# Resting orders at Trading 212 — design

Date: 2026-11-03. Requested by Tijs. Research: `/mnt/project-files/research/t212-orders-bij-broker.md`.

## Goal

Fastest execution at the best price for live Trading 212 trades. Entry and take-profit stop depending on Yahoo prices, which lag EU exchanges by about 15 minutes.

Success:
- Entry fills at the trigger level or better, on T212's live price, without the 3-minute poll or Yahoo delay.
- Take-profit sells at or above target within about 1 minute of T212's price reaching it.
- Every existing interlock still blocks new buys (kill switch, daily loss limit, regime filter, MAX_POSITIONS, MAX_POSITION_PCT, trigger expiry).

Out of scope: Alpaca/IBKR, paper-only changes beyond keeping paper in step, real-time market data purchases, OCO emulation at the broker.

## Current flow (live, trading212)

| Step | Code | Price source |
|---|---|---|
| Entry trigger | `LiveBroker.check_and_execute_triggers` (`live.py`), every 3 min | Yahoo |
| Buy | `_t212_buy`: DAY limit at the Yahoo price, wait `FILL_WAIT_SECONDS`, cancel rest | T212 |
| Stop-loss | `_t212_place_stop`: GTC sell stop right after the fill | T212 |
| Take-profit | `check_stops_and_targets`, every 10 min, market sell | Yahoo |

## Design

Everything below runs only when `TRADING_MODE == "LIVE"`, `LIVE_TRADING_ENABLED`, `LIVE_BROKER == "trading212"` **and** a new setting `T212_RESTING_ORDERS` is `True` (default `False`). With the flag off the current flow is unchanged. The flag lets Tijs run it on the demo account first.

### 1. Entry: place a resting order when a trigger is funded

When a live trigger is PENDING, funded (`allocation_eur > 0`), its market is open or opening, and all buy gates pass, the bot places a GTC order at T212 and stores its id in the existing `OrderTrigger.broker_order_id` column (no migration):

- `LIMIT_PULLBACK` → GTC **limit buy** at `target_price`.
- `BREAKOUT_BUY` → GTC **stop-limit buy**: stop = `target_price`, limit = `target_price * (1 + BREAKOUT_LIMIT_BUFFER_PCT / 100)`. New setting, default `0.5`. Caps slippage; a gap above the cap means no fill, which is the intended trade-off.
- Quantity: the same sizing `execute_buy` uses today (allocation / limit price, whole or fractional per `WHOLE_SHARES_ONLY`), validated by `validate_buy_order` at placement time.

"Buy gates" are the checks `check_and_execute_triggers` and `execute_buy` run today: kill switch / `is_live_armed`, daily loss limit, regime filter, trigger gate verdict, affordability, `MAX_SINGLE_ORDER_EUR`.

New client method: `Trading212Client.place_stop_limit(ticker, quantity, stop_price, limit_price, validity)`.

### 2. Order sync job (every 1 minute)

New scheduler job `t212_order_sync`, interval 1 minute, runs under the existing `_monitor_lock`. Per PENDING live trigger with a `broker_order_id`:

1. Read the order (`get_order`) and, when gone, its fills (`_t212_fills`).
2. **Filled (fully or partly)** → book the position with the real filled quantity and average price, the trigger's stop/target, mark trigger TRIGGERED, place the GTC stop (`_t212_place_stop`). A partial fill keeps the rest resting until the next sync; the booked position grows on each sync.
3. **Cancelled/rejected at the broker** → clear `broker_order_id`; the trigger becomes eligible for placement again (or cancelled, see 3).
4. **Still pending** → re-check the cancel rules below.

Then place orders for funded triggers that have none (step 1).

Order of jobs matters: `sync_account_state` adopts untracked holdings. The order sync must run first in the same locked block, so a fill is booked against its trigger and never adopted as a stray holding.

### 3. Cancel rules (checked every sync)

Cancel the resting order at T212 (and clear `broker_order_id`) when:

| Condition | Trigger after cancel |
|---|---|
| Trigger expired (`is_trigger_expired`) | EXPIRED |
| Price ran through the setup (`evaluate_trigger_condition` invalid, on the Yahoo price: T212 gives no price for stocks not held) | CANCELLED |
| Funding dropped to €0 (`adjust_triggers_dynamically`) or allocation changed by more than 10 % | PENDING, re-placed when funded |
| Kill switch on, daily loss limit hit, live disarmed | PENDING, no new orders until cleared |
| Regime filter negative | PENDING, re-placed when it turns positive |
| User cancels the trigger (`cancel_price_trigger`) | CANCELLED |
| Position slots full after another fill | PENDING |

Kill switch and daily loss also cancel resting buys immediately when they flip (not only at the next sync).

T212 can't modify orders, so a change is cancel + new order. Placement is rate limited to 1 per 2 s; the sync places at most a few orders per run and leaves the rest for the next minute.

### 4. Take-profit on the live T212 price

For live T212 positions, `refresh_positions_pnl` takes `currentPrice` from `/equity/positions` (one call for all positions) instead of Yahoo. The same 1-minute job runs `check_stops_and_targets` for T212.

When price ≥ target: cancel the resting stop, place a **DAY limit sell at the target price** for the held quantity. Price is already at or above target, so it fills at once at target or better. Not filled within `FILL_WAIT_SECONDS` → cancel it, restore the GTC stop (`_t212_restore_stop`), retry next minute. A sell never goes below target this way.

The stop-loss stays a GTC stop at T212, as today.

### 5. Paper mode

Paper already fills triggers at `target_price` (limit semantics). Breakouts also fill at `target_price` in paper; the buffer only affects live. Paper keeps its 3-minute Yahoo-based check. No other paper change: paper has no broker latency to remove.

## Error handling

- Never retry an order POST. On timeout or 5xx, look up pending orders for the ticker (`_t212_find_recent`) before deciding, as `_t212_post` does today.
- Treat every broker answer as untrusted: zero/None fill prices and unknown statuses leave the trigger untouched and log an error.
- A sync failure leaves resting orders in place. Stops already at T212 still protect open positions.
- Bot offline while a resting buy fills: the position has no stop until the bot comes back. On start, `check_and_run_startup_sessions` runs the order sync first, which books it and places the stop. This risk is accepted and stated in the PR.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `T212_RESTING_ORDERS` | `False` | Turn this design on |
| `BREAKOUT_LIMIT_BUFFER_PCT` | `0.5` | Stop-limit cap above the breakout level |

## Interlocks touched

Kill switch, daily loss limit, regime filter, MAX_POSITIONS, trigger expiry: checks move from fill time to placement time, plus active cancellation. None is weakened.

## Testing

Mocked T212 HTTP (existing `httpx.MockTransport` pattern in `tests/test_trading212_broker.py`):
- Funded pullback → one GTC limit at target; breakout → stop-limit with buffer.
- Fill / partial fill → position booked with real qty and price, stop placed, trigger TRIGGERED.
- Each cancel rule → order cancelled, trigger state as in the table.
- Kill switch on → no placement, existing resting buys cancelled.
- Order sync before account sync → fill never adopted as a stray holding.
- TP: T212 price ≥ target → stop cancelled, limit sell at target; unfilled → stop restored.
- Flag off → current flow unchanged (existing tests stay green).
- Timeout on POST → no second order.

Then the live-money test set from the `live-money-safety` skill and the full suite.

## Demo checks before live (Tijs, on `TRADING212_DEMO=true`)

1. GTC limit buy with a fractional quantity is accepted.
2. GTC stop-limit buy with a fractional quantity is accepted.
3. Resting stop plus DAY limit sell on the same shares: what T212 does (the design avoids it by cancelling the stop first).
4. Blocked cash for pending buys shows in `/equity/account/summary` (the bot's cash figure must not double-count it).

A failure on 1 or 2 means whole-share orders only for resting orders, or the flag stays off.
