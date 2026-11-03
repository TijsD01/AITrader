# Resting Trading 212 Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Live Trading 212 entries become DAY orders resting at the broker, and take-profit uses T212's live price with a limit sell, behind a flag.

**Architecture:** `LiveBroker` gets a 1-minute `sync_resting_entries()` that places, follows, books and cancels entry orders per `OrderTrigger` (id in the existing `broker_order_id` column). `execute_buy` is split so sizing and booking can be reused by the sync. Exits gain an optional limit price. A scheduler job runs the sync.

**Tech Stack:** Python 3.13, FastAPI app, APScheduler, async SQLAlchemy, httpx, pytest (asyncio_mode=auto).

**Spec:** `docs/superpowers/specs/2026-11-03-t212-resting-orders-design.md`

## Global Constraints

- Everything new is a no-op unless `settings.T212_RESTING_ORDERS` is `True` **and** `settings.LIVE_BROKER.lower() == "trading212"`. Flag default `False`. With the flag off, every existing test passes unchanged.
- `BREAKOUT_LIMIT_BUFFER_PCT` default `0.5`.
- Entry orders use `timeValidity="DAY"`. Stops stay `GOOD_TILL_CANCEL`.
- No placement until the ticker's market has been open 20 minutes: `check_market_hours(t, now)` and `check_market_hours(t, now - 20 min)` both open.
- Never retry an order POST; route placements through `LiveBroker._t212_post` (it looks the order up after an ambiguous error).
- T212 rate limits: limit/stop/stop-limit POST 1 per 2 s, `GET /equity/orders` 1 per 5 s. At most 3 placements per sync run, `await asyncio.sleep(2.1)` between them.
- Kill switch / daily loss / `is_live_armed()` block placement but never block booking fills, stops or sells.
- Follow `.claude/skills/live-money-safety/SKILL.md` and `.claude/skills/ponytail/SKILL.md`. Smallest diff, comments only for the why.
- Tests: mocked broker only, reuse `FakeT212` and helpers from `tests/test_trading212_broker.py`. Known baseline failures: `test_antigravity_llm_client`, `test_portfolio_summary_refreshes_unrealized_pnl`. `pip install greenlet` if SQLAlchemy asyncio errors.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01ChtSRoc7Fe7UCY7QgX2Meo
  ```

## Review Focus

1. A resting buy that fills while the account sync runs must be booked once, against its trigger, never adopted as "untracked" (Task 3 test `test_account_sync_skips_ticker_with_resting_entry`).
2. Cash blocked by our own resting order must not defund its trigger (cancel/re-place loop every minute) (Task 3 test `test_resting_order_cash_does_not_defund_its_trigger`).
3. Crash between POST and DB write: next sync must take over the pending BUY, not place a second one (Task 3 test `test_sync_takes_over_existing_pending_buy`).
4. Kill switch flipped on: resting buys cancelled at once, fills already made still booked with a stop (Task 4 tests).
5. Take-profit limit sell that does not fill: stop restored, position stays open (Task 5 test `test_tp_limit_sell_unfilled_restores_stop`).

---

### Task 1: Settings, stop-limit client call, flag helper  _(Sonnet)_

**Files:**
- Modify: `config/settings.py` (next to `TRADING212_DEMO`), `.env.example` (next to `TRADING212_DEMO=true`)
- Modify: `src/trading/trading212.py` (after `place_stop`)
- Modify: `src/trading/live.py` (`LiveBroker`, near `is_live_armed`)
- Test: `tests/test_t212_resting_orders.py` (new)

**Interfaces:**
- Produces: `settings.T212_RESTING_ORDERS: bool = False`, `settings.BREAKOUT_LIMIT_BUFFER_PCT: float = 0.5`
- Produces: `Trading212Client.place_stop_limit(self, ticker: str, quantity: float, stop_price: float, limit_price: float, validity: str = "DAY") -> Dict[str, Any]` → `POST /equity/orders/stop_limit` with JSON `{"ticker", "quantity", "stopPrice", "limitPrice", "timeValidity"}`
- Produces: `LiveBroker.resting_orders_on() -> bool` (staticmethod): flag and `LIVE_BROKER == "trading212"`

- [ ] **Step 1: Failing tests** in `tests/test_t212_resting_orders.py`:
  - `test_place_stop_limit_posts_expected_body`: client with `httpx.MockTransport` (pattern: `test_instruments_fetched_once_and_stale_cache_served`) records the request; assert path ends `/equity/orders/stop_limit` and JSON == `{"ticker": "AAPL_US_EQ", "quantity": 0.5, "stopPrice": 100.0, "limitPrice": 100.5, "timeValidity": "DAY"}`.
  - `test_resting_orders_flag`: default → `LiveBroker.resting_orders_on() is False`; flag True + `LIVE_BROKER="alpaca"` → False; flag True + `"trading212"` → True (use `monkeypatch.setattr(settings, ...)`).
- [ ] **Step 2:** `python -m pytest tests/test_t212_resting_orders.py -q` → FAIL.
- [ ] **Step 3:** Implement the three interfaces. `.env.example`: commented `# T212_RESTING_ORDERS=false` and `# BREAKOUT_LIMIT_BUFFER_PCT=0.5` with a one-line explanation each.
- [ ] **Step 4:** Tests pass; `python -m pytest tests/test_trading212_broker.py tests/test_settings_persistence.py -q` green.
- [ ] **Step 5:** Commit `feat(t212): settings and stop-limit call for resting orders`.

### Task 2: Split `execute_buy` into plan and book  _(Opus)_

Pure refactor of `LiveBroker.execute_buy` (`src/trading/live.py`). Behaviour must not change.

**Files:**
- Modify: `src/trading/live.py` (`execute_buy`)
- Test: existing suites only, plus one new test in `tests/test_t212_resting_orders.py`

**Interfaces:**
- Produces: `async def _plan_live_buy(self, ticker: str, price_native: float, currency: str, stop_loss_native: Optional[float], take_profit_native: Optional[float], allocation_eur: float, consensus_score: int, is_manual: bool = False, trigger_id: Optional[int] = None) -> Dict[str, Any]` — today's steps 1–8 (armed, daily loss, market hours, auto-buy, cash, allocation, risk cap, whole-share fit, `validate_buy_order`, share count, fee/cash room). Returns the same failure dicts `execute_buy` returns today, or `{"success": True, "shares": float, "price_eur": float, "allocation_eur": float}`.
- Produces: `async def _book_live_fill(self, *, ticker: str, name: str, currency: str, shares: float, price_native: float, stop_loss_native: Optional[float], take_profit_native: Optional[float], rationale: str, consensus_score: int, broker_order_id: str) -> Dict[str, Any]` — fee calc, `Position`, `Trade`, `PortfolioSnapshot` (cash from latest live snapshot minus cost and fee, open count + 1), commit, `self._last_pnl_refresh = 0.0`; returns the success dict `execute_buy` returns today. Does not call `adjust_triggers_dynamically` and does not take `_t212_lock` (callers hold it).
- `execute_buy` becomes: `_plan_live_buy` → under `_t212_lock` (trading212 only): `_dispatch_real_order` → unfilled/failed handling as today → `_book_live_fill` → after lock: `adjust_triggers_dynamically()`, memory event, return.

- [ ] **Step 1:** Run `python -m pytest tests/test_live_trading.py tests/test_trading212_broker.py tests/test_entry_sizing.py tests/test_whole_shares.py tests/test_min_one_share.py tests/test_capital_usage_limit.py tests/test_trigger_allocation.py -q`; record the pass count.
- [ ] **Step 2: New failing test** `test_book_live_fill_records_position_trade_and_snapshot`: seed a live snapshot (cash 1000), call `_book_live_fill(ticker="ASML.AS", name="ASML", currency="EUR", shares=0.5, price_native=600.0, stop_loss_native=570.0, take_profit_native=660.0, rationale="r", consensus_score=85, broker_order_id="t212_9")`; assert one OPEN live `Position` with shares 0.5 / stop 570 / target 660, one BUY `Trade` with `broker_order_id="t212_9"`, newest snapshot cash == 1000 − 300 − fee.
- [ ] **Step 3:** Extract both methods; keep every comment and check in place.
- [ ] **Step 4:** Step 1 command shows the same pass count; new test passes.
- [ ] **Step 5:** Commit `refactor(live): split execute_buy into _plan_live_buy and _book_live_fill`.

### Task 3: Resting entry sync  _(Opus)_

**Files:**
- Modify: `src/trading/live.py`
- Test: `tests/test_t212_resting_orders.py`

**Interfaces:**
- Consumes: Task 1 (`place_stop_limit`, `resting_orders_on`), Task 2 (`_plan_live_buy`, `_book_live_fill`), existing `_t212_post`, `_t212_cancel`, `_t212_fills`, `_t212_place_stop`, `evaluate_trigger_condition`, `is_trigger_expired`, `check_market_regime`, `enforce_daily_loss_limit`, `is_live_armed`.
- Produces: `async def sync_resting_entries(self) -> List[Dict[str, Any]]` — booked fills, each the `_book_live_fill` dict plus `"trigger_id"`. Returns `[]` when `resting_orders_on()` is False.
- Produces: `async def _t212_cancel_entry(self, client: Any, trigger: OrderTrigger) -> List[Dict[str, Any]]` — cancels `trigger.broker_order_id` (`_t212_cancel`), books any fill (`_t212_fills` → `_book_live_fill` + `_t212_place_stop`, trigger → TRIGGERED), else clears `broker_order_id`. Returns booked fills. Caller commits the trigger and holds `_t212_lock`.

Behaviour of `sync_resting_entries` (all under `self._t212_lock`, after `adjust_triggers_dynamically()`), per PENDING live trigger:
1. **Has `broker_order_id`** (`"t212_<id>"`): `client.get_order(id)`.
   - `None` → `_t212_fills`; filled > 0 → book with trigger's stop/target, rationale `f"Resting order #{t.id} filled ({t.trigger_type} @ {t.target_price})"`, place stop (`stop_loss or fill*0.95`), status TRIGGERED, `triggered_at`; filled 0 → clear id (DAY expiry or broker cancel).
   - Pending with `float(order.get("filledQuantity") or 0) > 0` → `_t212_cancel_entry`.
   - Pending, cancel rules → `_t212_cancel_entry`, then status: expired → EXPIRED; `evaluate_trigger_condition` invalid on the Yahoo price → CANCELLED; not armed / daily loss / regime negative / `allocation_eur <= 0` / order value (qty × limit, to EUR) differs from `allocation_eur` by more than 10 % → stays PENDING.
2. **No `broker_order_id`**, funded, armed, no daily loss, market open ≥ 20 min, regime ok, condition not invalid, Yahoo price present:
   - Pending BUY (`quantity > 0`) for the T212 ticker in `client.pending_orders()` (read once per run) → store its id, place nothing.
   - Else `limit = target_price` (pullback) or `target_price * (1 + BREAKOUT_LIMIT_BUFFER_PCT/100)` (breakout); `_plan_live_buy(price_native=limit, allocation_eur=t.allocation_eur, trigger_id=t.id, ...)`; failure with `unaffordable` → CANCELLED (as `check_and_execute_triggers` does); other failure → skip. Place via `_t212_post`: pullback `place_limit(t212, shares, _t212_price(limit), "DAY")`, breakout `place_stop_limit(t212, shares, _t212_price(target), _t212_price(limit), "DAY")`. Store `f"t212_{order['id']}"`.
   - Max 3 placements, `asyncio.sleep(2.1)` between.
3. Commit per trigger; after the loop, `adjust_triggers_dynamically()` again when anything was booked.

Also in this task:
- `_fetch_t212_account_values` stores `self._reserved_for_orders_eur`; `_spendable_cash` caps at `available + reserved` when `resting_orders_on()`.
- `check_and_execute_triggers`: return `[]` right after `adjust_triggers_dynamically()` when `resting_orders_on()`.
- `_sync_t212_locked`: skip adopt/update for a held T212 ticker that maps to a PENDING live trigger with a `broker_order_id` (log one info line).

- [ ] **Step 1: Failing tests** (FakeT212 extended in the test file with `place_stop_limit` recording `("stop_limit", ticker, qty, (stop, limit))`, and `get_order` returning `order_state` entries). Each sets the flag, `_live(monkeypatch)`, a funded PENDING live trigger and a Yahoo price via `patch.object(live_module.market_data_provider, "get_stock_data", ...)`:
  - `test_pullback_trigger_places_day_limit_at_target`: one `("limit", "ASMLa_EQ", qty, 600.0)`, trigger `broker_order_id == "t212_1"`, no Position.
  - `test_breakout_trigger_places_stop_limit_with_buffer`: target 100 → `("stop_limit", ..., (100.0, 100.5))`.
  - `test_no_placement_in_first_20_minutes`: `check_market_hours` patched open now, closed at now−20 min → no posts.
  - `test_filled_order_is_booked_with_stop`: order gone, history fill 0.5 @ 598 → Position 0.5 @ 598, Trade, trigger TRIGGERED, one `("stop", ..., -0.5, 570.0)`.
  - `test_partial_fill_cancels_rest_and_books`: pending order with `filledQuantity` 0.2 → cancelled, Position 0.2.
  - `test_expired_trigger_cancels_order`, `test_price_through_stop_cancels_order` (status CANCELLED), `test_kill_switch_cancels_but_keeps_trigger_pending`.
  - `test_sync_takes_over_existing_pending_buy`: `pending=[{"id": 5, "ticker": "ASMLa_EQ", "quantity": 0.5, ...}]` → no post, `broker_order_id == "t212_5"`.
  - `test_resting_order_cash_does_not_defund_its_trigger`: summary with `availableToTrade` 0, `reservedForOrders` 300, trigger allocation 300 with an order → after two sync runs no cancel, no new post.
  - `test_account_sync_skips_ticker_with_resting_entry`: broker holds 0.2 ASMLa_EQ, trigger has `broker_order_id` → `_sync_t212_locked` adds no Position.
  - `test_flag_off_keeps_bot_trigger_path`: flag False → `sync_resting_entries() == []`, `check_and_execute_triggers` behaves as before (existing tests cover it).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** New tests pass; live-money test set from the skill green.
- [ ] **Step 5:** Commit `feat(t212): resting DAY entry orders synced every minute`.

### Task 4: Cancel resting buys at once  _(Sonnet)_

**Files:**
- Modify: `src/trading/live.py` (`cancel_price_trigger`, new `cancel_resting_entries`)
- Modify: `src/db/session.py` (`set_kill_switch`)
- Test: `tests/test_t212_resting_orders.py`

**Interfaces:**
- Consumes: Task 3 `_t212_cancel_entry(client, trigger)`.
- Produces: `async def cancel_resting_entries(self, reason: str) -> int` — under `_t212_lock`, `_t212_cancel_entry` for every PENDING live trigger with a `broker_order_id`; triggers stay PENDING; returns the count; logs `reason`. No-op (0) when `resting_orders_on()` is False or no client.
- `cancel_price_trigger`: when the trigger has a `broker_order_id` and `resting_orders_on()`, call `_t212_cancel_entry` first; if the order is still pending afterwards return `{"success": False, "message": ...}` and leave it PENDING.
- `set_kill_switch(True)`: after the commit, best-effort `await get_active_broker().cancel_resting_entries("kill switch on")` when the broker has that method; exceptions logged, never raised. Lazy import inside the function (avoid import cycle).

- [ ] **Step 1: Failing tests:** `test_kill_switch_on_cancels_resting_buys` (two triggers with orders → two cancels, both PENDING), `test_cancel_trigger_cancels_broker_order` (→ CANCELLED, order cancelled), `test_cancel_trigger_with_fill_books_position` (history fill on cancel → Position booked, trigger TRIGGERED, response success False with message saying it filled).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass + `tests/test_web_security.py tests/test_telegram.py -q` green.
- [ ] **Step 5:** Commit `feat(t212): cancel resting buys on kill switch and trigger cancel`.

### Task 5: Take-profit on live T212 price with limit sell  _(Opus)_

**Files:**
- Modify: `src/trading/live.py` (`refresh_positions_pnl`, `check_stops_and_targets`, `execute_sell`, `_dispatch_real_close`, `_t212_close`, `_t212_exit`)
- Test: `tests/test_t212_resting_orders.py`

**Interfaces:**
- `execute_sell(self, ticker, reason="Manual Close", broker_flat=False, exit_price_native=None, limit_price_native: Optional[float] = None)` → `_dispatch_real_close(ticker, shares, currency, limit_price: Optional[float] = None)` → `_t212_close(ticker, shares, currency, limit_price=None)` → `_t212_exit(client, t212_ticker, ticker, shares, sells, limit_price=None)`. With `limit_price`, `_t212_exit` sends `client.place_limit(t212_ticker, -shares, self._t212_price(limit_price), "DAY")` instead of `place_market`; the rest (follow, cancel unfilled, fills, restore stop) is unchanged. Alpaca ignores it.
- `check_stops_and_targets`: when `resting_orders_on()` and the take-profit branch fires, call `execute_sell(ticker, reason=..., limit_price_native=p.take_profit_native)`. Stop-loss branch unchanged.
- `refresh_positions_pnl`: when `resting_orders_on()`, read `client.positions()` once; per position use `currentPrice` of the T212 instrument that `resolve_ticker(p.ticker, p.currency)` maps to; fall back to Yahoo when missing. Trailing-stop logic unchanged (ATR still from Yahoo).

- [ ] **Step 1: Failing tests:**
  - `test_refresh_uses_t212_current_price`: Yahoo 100, T212 `currentPrice` 110 → `current_price_native == 110`.
  - `test_take_profit_sends_limit_sell_at_target`: position target 660, T212 price 665 → posts include `("limit", "ASMLa_EQ", -0.5, 660.0)`, no `"market"` post, position CLOSED on fill.
  - `test_tp_limit_sell_unfilled_restores_stop`: limit stays pending → cancelled, stop re-placed, position OPEN.
  - `test_stop_loss_branch_still_market`: price below stop → `"market"` post.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass + live-money test set green.
- [ ] **Step 5:** Commit `feat(t212): take-profit on live T212 price with limit sell`.

### Task 6: Scheduler job  _(Sonnet)_

**Files:**
- Modify: `src/scheduler/jobs.py` (`TradingScheduler.start` job list, new method, `check_and_run_startup_sessions` start)
- Test: `tests/test_committee_jobs.py` or `tests/test_t212_resting_orders.py`

**Interfaces:**
- Consumes: `LiveBroker.resting_orders_on()`, `sync_resting_entries()`, `check_stops_and_targets()`, `telegram_bot.notify_trigger_executed`.
- Produces: `async def monitor_resting_orders(self) -> List[Dict[str, Any]]` — returns `[]` unless `get_active_broker()` has `resting_orders_on` and it is True. Under `self._monitor_lock`: `sync_cash_balance()` (errors logged), `sync_resting_entries()` (notify each booked fill), then `self._check_stops_and_targets()`. Returns booked fills.
- Job `id="t212_order_sync"`, `IntervalTrigger(minutes=1)`, `max_instances=1`, `coalesce=True`.
- `check_and_run_startup_sessions` calls `await self.monitor_resting_orders()` first (inside its try).

- [ ] **Step 1: Failing tests:** `test_monitor_resting_orders_noop_when_flag_off` (sync not called), `test_monitor_resting_orders_books_and_notifies` (patched `sync_resting_entries` returns one fill → `notify_trigger_executed` awaited once, `check_stops_and_targets` awaited), `test_scheduler_registers_t212_order_sync` (job id present after `start()` with scheduler patched as in existing job tests, or by inspecting `add_job` calls).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Pass; full suite `python -m pytest tests/ -q` shows only the two baseline failures.
- [ ] **Step 5:** Commit `feat(scheduler): one-minute Trading 212 order sync job`.

### Final: fresh-context ponytail review

A new agent with no context reads `.claude/skills/ponytail-review/SKILL.md`, the spec and the branch diff against `origin/feature/fees-memory-market-refresh`, and reports findings. Orchestrator fixes must-fix items, then opens the PR (interlocks touched, demo checks from the spec, flag default off).
