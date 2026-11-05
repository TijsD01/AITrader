# My holdings (hands-off positions) — design

Date: 2026-11-05. Requested by Tijs. UI mockup: `2026-11-05-my-holdings-mockup.png` (Home tab, phone width).

## Goal

Track the stocks Tijs bought himself in the same Trading 212 account the bot trades, and keep the bot away from them. A per-position "Bot | Mine" slider on the Home tab moves a holding between the bot and Tijs.

Success:
- A holding marked Mine is never sold, stopped, bought more of, armed as a trigger, or analysed by the bot.
- The bot's sizing and caps ignore Mine holdings.
- Tijs sees his holdings, their value and P&L next to the bot's positions.
- Flipping a bot position to Mine removes the bot's stop at Trading 212 before anything else changes. If that fails, nothing changes.

Out of scope: Alpaca (keeps today's behaviour), paper mode, holdings at other brokers or typed in by hand, splitting one ticker between bot and Mine, Telegram commands for holdings.

## Current behaviour

`LiveBroker._sync_t212_locked` (`src/trading/live.py`) adopts every T212 holding the bot never booked through `_adopt_holding`. With a live buy plan for the ticker (`_planned_levels`: latest PENDING/TRIGGERED live `OrderTrigger`) it books a `Position` with that stop and target and places a broker stop. Without a plan it books a `Position` with no stop and no target ("tracked only, the bot never sells it"). Either way the holding sits in `positions`, so it counts in sizing, MAX_POSITIONS and `invested_eur`, and the scanner treats the ticker as held.

## Decisions

| Question | Decision |
|---|---|
| Where are Tijs's stocks? | Same Trading 212 account as the bot. Found by the sync. |
| What does Mine block? | Selling (stops, targets, monitor exits, manual Sell button), buying more (auto, trigger, manual Buy), and scanning/analysis. |
| Money math | Mine holdings are left out of sizing, MAX_POSITIONS, MAX_POSITION_PCT and the deployable-capital cap. |
| Default for a new untracked holding | Mine, unless a live buy plan exists for the ticker; then Bot (likely a lost bot fill), as today. |
| Shared cash | No new cash bookkeeping. `MAX_DEPLOYABLE_CAPITAL_EUR` is the bot budget; Mine holdings no longer count against it. |
| Storage | Separate table `user_holdings`. The `positions` table stays bot-only, so the ~20 existing `positions` queries need no owner filter. |

## Design

Everything below is live-only and Trading 212-only (`TRADING_MODE == "LIVE"`, `LIVE_TRADING_ENABLED`, `LIVE_BROKER == "trading212"`). In paper mode and with Alpaca the feature is off and behaviour is unchanged.

### 1. Data

New model `UserHolding` in `src/db/models.py`, table `user_holdings` (created by `create_all`; no `_run_migrations` entry, since it is a new table):

| Column | Type | Note |
|---|---|---|
| `id` | Integer PK | |
| `ticker` | String(20), unique, index | Yahoo ticker, e.g. `ASML.AS`, same form as `Position.ticker` |
| `t212_ticker` | String(40), unique | T212 instrument ticker; the sync matches on this |
| `name` | String(100) | |
| `currency` | String(10) | |
| `shares` | Float | From T212 `quantity` |
| `avg_price_native` | Float | From T212 `averagePricePaid` |
| `current_price_native` | Float | From T212 `currentPrice` when present, else refreshed with bot positions |
| `current_price_eur` | Float | |
| `cost_basis_eur` | Float | `avg_price_native` converted at sync time |
| `added_at`, `updated_at` | DateTime | |

Value and P&L are derived (`shares * current_price_eur - cost_basis_eur`), not stored.

New helper module `src/trading/holdings.py` with:
- `mine_tickers(session) -> set[str]`: upper-case Yahoo tickers of all rows. Empty outside live T212.
- `is_mine(session, ticker) -> bool`.
- `holdings_value_eur(session) -> float`.
- `list_holdings() -> list[dict]` for the API.

### 2. Sync (`_sync_t212_locked`)

For each T212 holding, in this order:
1. Its `t212_ticker` is in `user_holdings`: update shares, average price and current price on that row. Nothing else.
2. It matches an open bot `Position`: unchanged (`_update_tracked_holding`).
3. It has a resting entry order: unchanged (skipped, booked by `sync_resting_entries`).
4. New, and `_planned_levels` finds a live buy plan: unchanged (`_adopt_holding` as Bot, broker stop placed, Telegram alert).
5. New, no plan: insert a `UserHolding` row. Telegram: "New holding marked as yours: 24 KO. The bot will not trade it. Flip it to Bot on the Home tab if the bot should."

A `user_holdings` row whose `t212_ticker` is no longer held: confirm with the same second positions call the bot already uses for gone positions, then delete the row. No bot `Trade` is booked. Log it.

The snapshot's `open_positions_count` counts bot positions only.

### 3. Guards (the bot never touches a Mine ticker)

- Scanner (`src/scheduler/jobs.py`, `run_market_session` and `scan_market_opportunities`): drop Mine tickers from the candidate list before any agent runs. No LLM calls, no shadow follow-up, no report.
- `LiveBroker.execute_buy`: refuse a Mine ticker, including `is_manual=True`. Message: "KO is marked as yours. Flip it to Bot on the Home tab first."
- `LiveBroker.create_price_trigger`: refuse a Mine ticker with the same message.
- `trigger_gate` manual precheck (`precheck_manual_trigger`): same refusal, so the Advice tab shows it before the committee runs.
- `check_and_execute_triggers`: skip a pending trigger whose ticker is Mine and log a warning (defence in depth; creation refuses Mine tickers and the flip cancels their triggers).

Sell paths need no guard: they only read `positions`, and Mine holdings are not there.

### 4. Money math

- Sizing already uses `cash + bot positions` (`live.py` trigger planning) and `invested_eur = sum(bot cost basis)`. Both now exclude Mine holdings with no code change, because Mine holdings are not in `positions`.
- MAX_POSITIONS and `open_positions_count` count bot positions only.
- `get_portfolio_summary`:
  - `total_value_eur` stays the whole T212 account (broker total). The fallback when T212 is unreachable becomes `cash + bot equity + holdings_value_eur`.
  - New fields `user_holdings_eur` and `bot_equity_eur`.
  - `unrealized_pnl_eur` and the position count stay bot-only.
- **Exception, kept on purpose: the daily loss limit.** `get_daily_loss_eur` compares today's account value with the last snapshot before today, and snapshots hold the whole account. To keep that comparison consistent, its "current" value becomes `cash + bot positions + holdings_value_eur`. A fall in Mine holdings therefore counts toward the daily loss limit, as it does today. This is the safe direction: it can only stop new buys sooner. Leaving Mine holdings out of "current" while the snapshot base still holds them would report their full value as a loss and trip the kill switch at once.

### 5. Slider flip

New `LiveBroker` methods, both under `self._t212_lock` so a sync cannot run halfway through a flip.

**`release_to_user(ticker)`: Bot → Mine**
1. Load the open live `Position`. None → error "No bot position in X."
2. Cancel every pending live `OrderTrigger` for the ticker. `cancel_price_trigger` takes `_t212_lock` itself and `asyncio.Lock` is not re-entrant, so split its body into a lock-free `_cancel_trigger_locked(session, trigger)` that both `cancel_price_trigger` and the flip call. It also cancels a resting T212 entry order. If a resting entry filled before the cancel, or its order is still pending, stop with error "A pending buy for X could not be cancelled. Nothing changed." and change nothing else.
3. Cancel all T212 SELL orders for the ticker (`_t212_sells` + `cancel_order`), then `_t212_wait_gone`. If any order is still pending: stop, change nothing in the DB, return error "The stop at Trading 212 could not be cancelled. Nothing changed. Try again."
4. In one DB transaction: insert the `UserHolding` (shares, average price and current price from the position; `cost_basis_eur` from the position) and set the `Position` to `status="RELEASED"`. No SELL `Trade` is booked; `TradeLearner` scores from SELL trades, so a released position is not scored.
5. Telegram: "NVDA is now yours. The bot cancelled its stop and will not trade it."

**`take_from_user(ticker)`: Mine → Bot**
1. Load the `UserHolding`. None → error.
2. Read the T212 holding. Not held anymore → delete the row and return error "Trading 212 no longer holds X."
3. In one DB transaction: delete the row and call `_adopt_holding` with the T212 numbers (stop and target from a live buy plan if one exists, else tracked with no stop, as today's adoption).
4. Place the broker stop if one was planned (`_finish_sync`'s stop step), then Telegram as today's adoption alert.

### 6. One-time move of existing positions

On startup (after `create_all`), run once, guarded by a `SystemConfig` key `user_holdings_backfill_done`: every open live `Position` with no stop, no target, no `broker_order_id` and no live BUY `Trade` for that ticker at or after `opened_at - 10 min` was adopted without a plan. Move it to `user_holdings` and mark the `Position` `RELEASED`. Send one Telegram message listing the moved tickers: "Moved to My holdings: KO, VWRL.AS. Flip any back to Bot on the Home tab." Runs only in live T212 mode; otherwise the key stays unset so it runs on the first live start.

### 7. API (`src/web/routes.py`)

Protected like every other route by `install_web_security` (`src/web/security.py`).

- `GET /api/holdings` → list of holdings with value and P&L. `[]` when the feature is off.
- `POST /api/holdings/{ticker}/owner`, body `{"owner": "mine" | "bot"}` → calls `release_to_user` or `take_from_user`. Returns `{"ok": true, "message": ...}` or HTTP 409 with the error message. Returns 409 "Only for live Trading 212." when the feature is off.

### 8. UI (`src/web/static/`)

Matches the existing Home tab: same tokens, IBM Plex fonts, `.ui-list` rows and `confirm()` + `showToast` patterns. The mockup is the reference.

- "Positions" heading becomes "Bot positions".
- New section "My holdings" with a count, below Bot positions, using the same `.pos-row` markup. Its monogram is outlined grey (`.pos-mono.mine`), not a bot allocation colour. The open row shows Avg price, Now, P&L and the slider. It has no Sell button. Note under the slider: "The bot never buys or sells KO. Sell it in Trading 212."
- Bot position open row: the slider sits above the Sell button. Note: "The bot trades NVDA: stop, target and sell."
- **Slider `.own-switch`**: two-segment radio group (Bot | Mine) with a sliding thumb. Bot side uses `--accent`, Mine side uses `--text`. It sits inside `.pos-detail`, never inside the `.pos-head` button. Keyboard: arrow keys move between the two radios; visible focus ring; `prefers-reduced-motion` turns the slide off.
- Flip to Mine asks: "NVDA becomes yours. The bot cancels its stop at Trading 212 and never trades NVDA again. Continue?" Flip to Bot asks: "Let the bot trade KO? It may set a stop and sell it."
- While a flip runs: slider dimmed with a spinner, `aria-busy`, note "Cancelling the stop at Trading 212…". On success: toast with the server message, reload portfolio. On error: toast with the server message, slider returns to its old side.
- Hero: Portfolio value stays the account total. Allocation bar adds a hatched segment for Mine. The legend line reads Bot / Yours / Cash. Yours shows only when there are holdings.
- Empty "My holdings": the section is hidden.
- Loading: the section waits on `/api/holdings` without blocking the bot list. Error: one line "Could not load your holdings. Pull to refresh." in the section.
- Paper mode or Alpaca: no slider and no My holdings section.

## Error handling

- A T212 call fails during Bot → Mine: nothing in the DB changes, error shown, bot keeps the position and its stop.
- A T212 call fails during Mine → Bot: nothing in the DB changes, holding stays Mine.
- Crash or restart between the stop cancel and the DB step: the DB transaction is atomic, so the position is still Bot, but it has no broker stop. `check_stops_and_targets` still enforces its stop and target every 10 minutes from its stored levels, so it is not unprotected, only slower. The flip is safe to repeat: Tijs flips again and it completes.
- Sync cannot read positions: no `user_holdings` change, same as bot positions today.

## Testing (`tests/`, no live broker, T212 client mocked)

Sync:
- A new holding with no plan becomes a `UserHolding`; no `Position` and no broker stop.
- A new holding with a plan becomes a `Position` with a stop (today's behaviour).
- A `UserHolding` share change updates the row; a gone holding (confirmed twice) deletes it with no `Trade`.

Guards:
- `execute_buy` (auto and manual), `create_price_trigger` and the manual precheck refuse a Mine ticker.
- Scanner drops Mine tickers before evaluation.

Money math:
- Allocation, MAX_POSITIONS and the capital cap ignore Mine holdings.
- `get_daily_loss_eur` with Mine holdings present and flat prices reports 0, not their value.

Flip:
- Bot → Mine: stop cancelled, triggers cancelled, row moved, `Position` RELEASED, no SELL `Trade`.
- Bot → Mine with a stop cancel that stays pending: no DB change, error returned.
- Bot → Mine with a resting entry that cannot be cancelled: no DB change, error returned, no deadlock on `_t212_lock`.
- `cancel_price_trigger` behaves as before after the split.
- Mine → Bot: row deleted, `Position` adopted, stop placed when a plan exists.
- Both return 409 in paper mode.

Backfill:
- Moves only plan-less adopted positions, runs once.

UI: manual check at phone and desktop width with a screenshot of Home, both slider states, the busy state and an error toast. The app has one dark theme, so there is no light mode to check.

Run: `python -m pytest tests/ -q`. The `live-money-safety` skill applies to sections 3–6 during implementation.
