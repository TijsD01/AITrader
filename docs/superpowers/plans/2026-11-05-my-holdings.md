# My Holdings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track Tijs's own Trading 212 holdings in a separate `user_holdings` table, keep the bot away from them, and let a Bot | Mine slider on the Home tab move a holding between bot and Tijs.

**Architecture:** The bot's `positions` table stays bot-only, so every existing sell, stop and sizing query ignores Mine holdings without a filter. A small module `src/trading/holdings.py` owns the new table's reads. `LiveBroker` routes plan-less holdings into it at sync, refuses Mine tickers on every buy path, and gets two flip methods that run under `_t212_lock`. The Home tab gets a "My holdings" list and the slider.

**Tech Stack:** Python 3.13, FastAPI, async SQLAlchemy + aiosqlite, pytest (`asyncio_mode = auto`), vanilla JS + Tailwind CDN + `style.css` tokens.

**Spec:** `docs/superpowers/specs/2026-11-05-my-holdings-design.md` (mockup: `docs/superpowers/specs/2026-11-05-my-holdings-mockup.png`). Read both.

## Global Constraints

- Feature is on only when `TRADING_MODE == "LIVE"` and `LIVE_TRADING_ENABLED` and `LIVE_BROKER == "trading212"` (`holdings_enabled()`, Task 1). Paper and Alpaca behaviour must not change. Paper is unchanged on purpose: it has no broker holdings.
- Never weaken an interlock (kill switch, daily loss limit, MAX_POSITIONS, MAX_POSITION_PCT, whole shares). Kill switch blocks buys only; flips are not buys and stay allowed.
- Ticker comparisons are on the upper-case Yahoo ticker (`Position.ticker` form, e.g. `ASML.AS`).
- Every T212 write follows the existing pattern: cancel, then `_t212_wait_gone`, and stop on failure before any DB change.
- Errors return `{"success": False, "message": ...}`, like other `LiveBroker` methods. Successes return `{"success": True, "message": ...}`.
- Exact user-facing copy (keep verbatim; `X` is the ticker):
  - Buy/trigger refusal: `X is marked as yours. Flip it to Bot on the Home tab first.`
  - Sync Telegram: `New holding marked as yours: {shares:g} X. The bot will not trade it. Flip it to Bot on the Home tab if the bot should.`
  - Release Telegram: `X is now yours. The bot cancelled its stop and will not trade it.`
  - Release errors: `No bot position in X.` / `The stop at Trading 212 could not be cancelled. Nothing changed. Try again.` / `A pending buy for X could not be cancelled. Nothing changed.` / `X has no unique Trading 212 instrument. Nothing changed.`
  - Take errors: `X is not in your holdings.` / `Trading 212 no longer holds X.`
  - Feature off: `Only for live Trading 212.`
  - Backfill Telegram: `Moved to My holdings: A, B. Flip any back to Bot on the Home tab.`
  - UI copy: as listed in spec section 8.
- Tests never call a live broker or LLM. Use `FakeT212` and fixtures from `tests/test_trading212_broker.py` (`fake`, `clean_snapshots`, `_held`, `_live`, `_cleanup`), imported the way `tests/test_t212_resting_orders.py` does.
- Commit style: `feat(holdings): ...`, `test(holdings): ...`. End each message with the session's attribution lines.

## Review Focus

1. **Ticker typed in another case** (`ko` in manual Buy) must still be refused. Test in Task 3.
2. **Tijs sells part of a Mine holding**: the row's share count drops; the row is not deleted. Test in Task 2.
3. **Flip while a sync runs**: the flip waits for `_t212_lock`; no half state. Test in Task 5.
4. **T212 client missing or unreachable during a flip**: error, nothing changes. Tests in Tasks 5 and 6.
5. **Double tap on the slider** (second request after the first finished): the second gets a 409 with `No bot position in X.` and the UI snaps back. Test in Task 8.

---

### Task 1: `UserHolding` model and `holdings.py` helpers

**Files:**
- Modify: `src/db/models.py` (add model after `Position`)
- Create: `src/trading/holdings.py`
- Test: `tests/test_user_holdings.py`

**Interfaces:**
- Produces:
  - `UserHolding` model, table `user_holdings`, columns exactly as spec section 1 (`ticker` unique+index, `t212_ticker` unique, `shares`, `avg_price_native`, `current_price_native`, `current_price_eur`, `cost_basis_eur`, `name`, `currency`, `added_at`, `updated_at`).
  - `holdings_enabled() -> bool`
  - `async mine_tickers(session) -> set[str]`: upper-case tickers; empty set when `holdings_enabled()` is False.
  - `async is_mine(session, ticker: str) -> bool`: `ticker.upper()` in `mine_tickers`.
  - `async holdings_value_eur(session) -> float`: `sum(shares * current_price_eur)` over all rows, no gate (rows only exist from live T212).
  - `holding_dict(h: UserHolding) -> dict` with keys `ticker, name, currency, shares, avg_price_native, current_price_native, current_value_eur, cost_basis_eur, unrealized_pnl_eur, unrealized_pnl_pct` (rounded to 2 decimals).
  - `async list_holdings() -> list[dict]`: `[]` when disabled, else `holding_dict` per row sorted by value, largest first.
  - `async refresh_holding_prices(session) -> None`: per row, Yahoo price via `market_data_provider.get_stock_data` (in `asyncio.to_thread`), skip when the market is closed and a price exists (same rule as `refresh_positions_pnl`), set native and EUR price.

- [ ] **Step 1: Write failing tests** in `tests/test_user_holdings.py`:
  - `test_mine_tickers_empty_when_feature_off`: add a row `KOHOLD`, settings at test defaults (paper) → `mine_tickers` is `set()`, `list_holdings()` is `[]`.
  - `test_mine_tickers_and_value_when_live_t212`: `_live(monkeypatch)` + `LIVE_BROKER="trading212"`; rows `KOHOLD` (24 sh, €62.10 now, cost €1,390.00) and `VWHOLD.AS` → `mine_tickers == {"KOHOLD", "VWHOLD.AS"}`, `await is_mine(s, "kohold")` is True, `holdings_value_eur` equals the sum, `list_holdings()[0]["unrealized_pnl_eur"] == round(24*62.10 - 1390.00, 2)`.
  - Clean up rows in `finally`.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings.py -q` → FAIL (import error).
- [ ] **Step 3: Implement** the model and module. The table is created by `create_all`; no `_run_migrations` entry.
- [ ] **Step 4: Run** the same command → PASS.
- [ ] **Step 5: Commit** `feat(holdings): add user_holdings table and helpers`.

---

### Task 2: Sync routes plan-less holdings to My holdings

**Files:**
- Modify: `src/trading/live.py` (`_sync_t212_locked`, `refresh_positions_pnl`)
- Test: `tests/test_user_holdings_sync.py`

**Interfaces:**
- Consumes: `UserHolding`, `refresh_holding_prices` (Task 1).
- Produces: sync behaviour from spec section 2. No new public names.

Changes in `_sync_t212_locked`:
- Load all `UserHolding` rows keyed by `t212_ticker` before the holdings loop.
- In the loop, before the `local` check: a held ticker in that map → update `shares`, `avg_price_native`, `current_price_native` (T212 `currentPrice` when present), `current_price_eur`, `cost_basis_eur = to_eur(avg) * shares`; `continue`.
- For a new holding (the current `else` branch): call `_planned_levels(session, yahoo)`. Plan found → `_adopt_holding` as today. No plan → add a `UserHolding` and append to a new `mine_added: List[str]` (`f"{shares:g} {yahoo}"`).
- `gone_mine = [t for t in mine_rows if t not in held]`. Run the existing second positions call when `gone or gone_mine`, and filter both lists by `still_held`. Delete confirmed `gone_mine` rows and log `Trading 212 sync: {ticker} no longer held; removed from My holdings.`
- Snapshot `open_positions_count` = number of held tickers that are bot positions (local or newly adopted), not `len(held)`.
- After `_finish_sync`, send one Telegram per `mine_added` with the exact sync copy (same try/except pattern as `_finish_sync`).

Change in `refresh_positions_pnl`: call `await refresh_holding_prices(session)` and commit before the `if not positions: return` early exit, so holdings refresh even with no bot positions.

- [ ] **Step 1: Write failing tests** (use `fake`, `clean_snapshots`, `_held`; patch `refresh_positions_pnl`, `get_portfolio_summary` and Telegram as `test_sync_adopts_untracked_and_closes_only_when_confirmed` does):
  - `test_sync_planless_holding_becomes_mine`: held `SAPd_EQ` 3 sh avg 120, no trigger → one `UserHolding` `SAP.DE` with 3 shares; no `Position` for `SAP.DE`; no `("stop", ...)` in `fake.posts`; Telegram called with text containing `New holding marked as yours: 3 SAP.DE`.
  - `test_sync_holding_with_plan_still_adopted_as_bot`: TRIGGERED live trigger for `ASML.AS` → `Position` with stop 570.0, no `UserHolding`. (Today's behaviour.)
  - `test_sync_updates_mine_shares_on_partial_sale`: existing row `SAP.DE`/`SAPd_EQ` 3 sh, held answer 1 sh → row has 1 share, still exists, no `Trade`.
  - `test_sync_removes_mine_only_when_confirmed_gone`: first answer misses it, second has it → row kept; both miss it → row deleted, no `Trade` row for the ticker.
  - `test_snapshot_counts_bot_positions_only`: one Mine + one bot holding → latest live snapshot `open_positions_count == 1`.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_sync.py -q` → FAIL.
- [ ] **Step 3: Implement** the changes above.
- [ ] **Step 4: Run** the new file plus `tests/test_trading212_broker.py tests/test_t212_resting_orders.py tests/test_portfolio_sync.py tests/test_live_trading.py -q` → all PASS. If an existing test expected a plan-less holding to become a `Position` through the sync, update it deliberately and say why in the commit message. (`_adopt_holding` itself is unchanged, so direct callers keep passing.)
- [ ] **Step 5: Commit** `feat(holdings): sync puts plan-less holdings in My holdings`.

---

### Task 3: Guards: no buy, trigger or scan on a Mine ticker

**Files:**
- Modify: `src/trading/live.py` (`_plan_live_buy`, `create_price_trigger`, `check_and_execute_triggers`)
- Modify: `src/trading/trigger_gate.py` (`_ticker_in_use`)
- Modify: `src/scheduler/jobs.py` (`run_market_session`, `scan_market_opportunities`)
- Test: `tests/test_user_holdings_guards.py`

**Interfaces:**
- Consumes: `is_mine`, `mine_tickers` (Task 1).
- Produces: `TriggerGateResult(False, "MINE", "<buy refusal copy>")` from `_ticker_in_use`.

Placement:
- `_plan_live_buy`: right after the `is_live_armed` check, before the daily loss check. Covers auto, manual and trigger buys.
- `create_price_trigger`: next to the existing open-position check.
- `_ticker_in_use`: first check, only when the live condition already computed there is true and `holdings_enabled()`.
- `check_and_execute_triggers`: in the non-resting loop, skip a PENDING trigger whose ticker is Mine and log a warning `Trigger #{id} for X skipped: X is marked as yours.` No cancel: creation refuses Mine tickers and the flip cancels triggers, so this only guards against a stale row.
- `jobs.py`: right after `_tickers_in_use(db_session)` in both places, `mine = await mine_tickers(db_session)`; drop Mine tickers from `candidates` before `actionable`/`other` are built, so they are never evaluated or followed by shadows.

- [ ] **Step 1: Write failing tests** (`_live(monkeypatch)`, `LIVE_BROKER="trading212"`, a `UserHolding` row `KOHOLD`):
  - `test_execute_buy_refuses_mine_ticker_manual_and_auto`: `execute_buy("kohold", ..., is_manual=True)` and `is_manual=False` both return `success False` and message `KOHOLD is marked as yours. Flip it to Bot on the Home tab first.`; `fake.posts == []`.
  - `test_create_trigger_refuses_mine_ticker`: same message, no `OrderTrigger` row.
  - `test_manual_trigger_precheck_refuses_mine`: `precheck_manual_trigger("KOHOLD", {"quote_type": "EQUITY", ...})` returns `code == "MINE"`.
  - `test_pending_trigger_on_mine_ticker_is_skipped`: PENDING trigger (no broker order) at a price already reached → after `check_and_execute_triggers`, trigger still `PENDING`, no buy posted.
  - `test_scanner_drops_mine_candidates`: patch `market_scanner.scan_universe` to return `KOHOLD` and `AAPL`; patch the committee's `evaluate_ticker`; run `run_market_session` → `evaluate_ticker` never called with `KOHOLD`. (Follow the patching pattern in `tests/test_committee_jobs.py`.)
  - `test_guards_off_in_paper_mode`: with default paper settings and the same row, `precheck_manual_trigger("KOHOLD", ...)` does not return `MINE`.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_guards.py -q` → FAIL.
- [ ] **Step 3: Implement** the guards.
- [ ] **Step 4: Run** the new file plus `tests/test_triggers.py tests/test_trigger_gate.py tests/test_committee_jobs.py tests/test_live_trading.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(holdings): bot never buys, arms or scans a Mine ticker`.

---

### Task 4: Money math: summary fields and daily loss

**Files:**
- Modify: `src/trading/live.py` (`get_daily_loss_eur`, `get_portfolio_summary`)
- Test: `tests/test_user_holdings_money.py`

**Interfaces:**
- Consumes: `holdings_value_eur` (Task 1).
- Produces: summary keys `user_holdings_eur: float` and `bot_equity_eur: float` (both rounded to 2). The frontend (Task 9) reads them.

Changes:
- `get_daily_loss_eur`: `current = latest.cash_eur + bot positions + await holdings_value_eur(session)`. Add a comment that snapshots hold the whole account, so Mine holdings must be on both sides.
- `get_portfolio_summary`: compute `mine_eur = await holdings_value_eur(session)` and `bot_equity = sum(bot positions)`. The local fallback `total_val` becomes `cash + bot_equity + mine_eur`. The broker-total path is unchanged. Add both new keys. `unrealized_pnl_eur`, `open_positions_count` and sizing inputs stay bot-only.

- [ ] **Step 1: Write failing tests:**
  - `test_daily_loss_ignores_flat_mine_holdings`: live snapshot before today with total €2,000 (cash €500, a Mine row worth €1,500 at the same price), latest snapshot cash €500, no bot positions → `get_daily_loss_eur() == 0.0`.
  - `test_daily_loss_counts_fall_in_mine_holdings`: same, Mine row now worth €1,400 → `100.0`.
  - `test_summary_reports_bot_and_mine_split`: one bot position €300, one Mine row €1,500 → `bot_equity_eur == 300.0`, `user_holdings_eur == 1500.0`, `open_positions_count == 1`.
  - `test_sizing_ignores_mine_holdings`: with `MAX_DEPLOYABLE_CAPITAL_EUR = 1000`, one Mine row with cost basis €5,000 → `usable_capital_eur` is the same as with no row.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_money.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new file plus `tests/test_capital_usage_limit.py tests/test_entry_sizing.py tests/test_portfolio_sync.py tests/test_cash_flows.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(holdings): keep Mine holdings out of sizing, in the daily loss check`.

---

### Task 5: Trigger-cancel split and `release_to_user` (Bot → Mine)

**Files:**
- Modify: `src/trading/live.py` (`cancel_price_trigger`, new `_cancel_trigger_locked`, new `release_to_user`)
- Test: `tests/test_user_holdings_flip.py`

**Interfaces:**
- Consumes: `UserHolding`, `holdings_enabled` (Task 1).
- Produces:
  - `async LiveBroker._cancel_trigger_locked(self, session, trigger: OrderTrigger) -> Dict[str, Any]`: the body of today's `cancel_price_trigger` from the `status != "PENDING"` check on; same return dicts; the caller holds `_t212_lock`.
  - `async LiveBroker.release_to_user(self, ticker: str) -> Dict[str, Any]`.

`release_to_user` steps, in order, all under `self._t212_lock`:
1. `holdings_enabled()` else `Only for live Trading 212.` Load the open live `Position` by `ticker.upper()`, else `No bot position in X.`
2. `client = trading212.get_client()`; `t212_ticker = await client.resolve_ticker(pos.ticker, pos.currency)`. No client or no ticker → `X has no unique Trading 212 instrument. Nothing changed.`
3. For each PENDING live trigger for the ticker: `_cancel_trigger_locked`. Any `success False` → `A pending buy for X could not be cancelled. Nothing changed.` and return.
4. `sells = self._t212_sells(await client.pending_orders(), t212_ticker)`; cancel each; `if not await self._t212_wait_gone(client, ids)` → stop error, return.
5. One transaction: add `UserHolding` (from the position: shares, `entry_price_native` as avg, current prices, `cost_basis_eur`, name, currency, `t212_ticker`); set `pos.status = "RELEASED"`; commit.
6. Telegram release copy (try/except). Return `{"success": True, "message": "X is now yours."}`.

Any exception from a T212 call in steps 2–4 → log it, return the step's error message, change nothing in the DB.

- [ ] **Step 1: Write failing tests** (`fake`, `_live`, `LIVE_BROKER="trading212"`, bot position `ASML.AS` 1 sh, `fake.pending = [{"id": 88, "type": "STOP", "ticker": "ASMLa_EQ", "quantity": -1.0, "stopPrice": 570.0}]` in the shape `_t212_sells` reads):
  - `test_cancel_price_trigger_unchanged_after_split`: PENDING trigger without broker order → cancel returns success and status `CANCELLED`; non-PENDING → same failure message as before.
  - `test_release_cancels_stop_and_moves_position`: `88 in fake.cancelled`; `UserHolding` `ASML.AS` exists with 1 share; `Position.status == "RELEASED"`; no SELL `Trade`; pending trigger for the ticker is `CANCELLED`.
  - `test_release_aborts_when_stop_stays_pending`: patch `_t212_wait_gone` to return False → stop-error message; `Position` still OPEN; no `UserHolding`.
  - `test_release_aborts_when_resting_buy_cannot_be_cancelled`: patch `_cancel_trigger_locked` to return `{"success": False, ...}` → pending-buy message; no stop cancelled (`fake.cancelled == []`); no DB change.
  - `test_release_without_client_changes_nothing`: `trading212.get_client` → None → instrument message; no DB change.
  - `test_release_waits_for_sync_lock`: hold `broker._t212_lock`, start `release_to_user` as a task, `await asyncio.sleep(0)` a few times → task not done; release the lock → task finishes with success.
  - `test_release_refused_in_paper_mode`: default settings → `Only for live Trading 212.`
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_flip.py -q` → FAIL.
- [ ] **Step 3: Implement** the split and `release_to_user`.
- [ ] **Step 4: Run** the new file plus `tests/test_triggers.py tests/test_t212_resting_orders.py tests/test_trading212_broker.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(holdings): flip a bot position to Mine after cancelling its broker stop`.

---

### Task 6: `take_from_user` (Mine → Bot)

**Files:**
- Modify: `src/trading/live.py` (new `take_from_user`)
- Test: `tests/test_user_holdings_flip.py` (extend)

**Interfaces:**
- Consumes: `UserHolding` (Task 1), `_adopt_holding`, `_finish_sync` (existing).
- Produces: `async LiveBroker.take_from_user(self, ticker: str) -> Dict[str, Any]`.

Steps under `self._t212_lock`: feature check; load row by `ticker.upper()` else `X is not in your holdings.`; `client.positions()` and find `row.t212_ticker` with quantity > 0. Missing → delete the row, commit, return `Trading 212 no longer holds X.` Found → in one transaction delete the row and `_adopt_holding(session, row.ticker, row.name, row.currency, shares, avg, 0.0, adopted, stops)`; commit; then `await self._finish_sync("Trading 212", [], stops, adopted)` outside the DB session. Return `{"success": True, "message": "The bot now trades X."}`. A T212 exception → error message, nothing changes.

- [ ] **Step 1: Write failing tests:**
  - `test_take_adopts_with_plan_and_places_stop`: row `ASML.AS`, held 1 sh avg 600, TRIGGERED trigger with stop 570 → row gone; `Position` OPEN with stop 570; `("stop", "ASMLa_EQ", -1.0, 570.0) in fake.posts`.
  - `test_take_without_plan_tracks_without_stop`: no trigger → `Position` with `stop_loss_native is None`; no stop post.
  - `test_take_when_broker_no_longer_holds`: held answer empty → message `Trading 212 no longer holds ASML.AS.`; row deleted; no `Position`.
  - `test_take_when_t212_errors_changes_nothing`: `fake.positions` raises → `success False`; row still exists.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_flip.py -q` → new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same file → PASS.
- [ ] **Step 5: Commit** `feat(holdings): hand a Mine holding to the bot`.

---

### Task 7: One-time backfill on startup

**Files:**
- Modify: `src/trading/holdings.py` (new `backfill_user_holdings`)
- Modify: `src/web/app.py` (`lifespan`, after `init_db()`)
- Test: `tests/test_user_holdings.py` (extend)

**Interfaces:**
- Produces: `async backfill_user_holdings() -> list[str]` (moved tickers). Key `SystemConfig("user_holdings_backfill_done", "true")`.

Rules: return `[]` without setting the key when `holdings_enabled()` is False or the key exists. Otherwise, for each open live `Position` with `stop_loss_native is None`, `take_profit_native is None`, `broker_order_id is None`, and no live BUY `Trade` for its ticker with `executed_at >= opened_at - 10 min`: resolve `t212_ticker` via `trading212.get_client().resolve_ticker`; skip (log a warning) when it does not resolve; else add a `UserHolding` and set the position `RELEASED`. Set the key and commit once. Send the backfill Telegram when the list is not empty. In `lifespan`, wrap the call in try/except and log, so a failure never blocks startup.

- [ ] **Step 1: Write failing tests:**
  - `test_backfill_moves_only_planless_adopted_positions`: three live positions: `KOHOLD` (no levels, no BUY trade) → moved; `BOTBUY` (no levels but a live BUY trade after `opened_at`) → stays; `BOTSTOP` (has a stop) → stays. Returns `["KOHOLD"]`.
  - `test_backfill_runs_once`: second call returns `[]` and moves nothing new.
  - `test_backfill_skipped_in_paper_mode`: default settings → `[]` and the key is not set.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings.py -q` → new tests FAIL.
- [ ] **Step 3: Implement** and wire into `lifespan`.
- [ ] **Step 4: Run** the same file → PASS.
- [ ] **Step 5: Commit** `feat(holdings): move earlier hand-bought holdings to My holdings once`.

---

### Task 8: API routes

**Files:**
- Modify: `src/web/routes.py`
- Test: `tests/test_user_holdings_api.py`

**Interfaces:**
- Consumes: `list_holdings` (Task 1), `live_broker.release_to_user` / `take_from_user` (Tasks 5, 6).
- Produces:
  - `GET /api/holdings` → `list_holdings()`.
  - `POST /api/holdings/{ticker}/owner` with body model `OwnerRequest(owner: Literal["mine", "bot"])` → 200 `{"ok": True, "message": ...}`, or 409 `{"detail": message}` on `success False`. Invalid owner → FastAPI 422.

Use the app's test client the way `tests/test_api_and_screener.py` does; `install_web_security` already covers the routes.

- [ ] **Step 1: Write failing tests** (patch `live_broker.release_to_user`/`take_from_user` with `AsyncMock`):
  - `test_get_holdings_empty_in_paper_mode` → `[]`.
  - `test_owner_mine_calls_release` → 200, `release_to_user` awaited with `"NVDA"`.
  - `test_owner_bot_calls_take` → 200, `take_from_user` awaited.
  - `test_owner_second_tap_returns_409`: mock returns `{"success": False, "message": "No bot position in NVDA."}` → 409 with that detail.
  - `test_owner_rejects_bad_value` → 422.
- [ ] **Step 2: Run** `python -m pytest tests/test_user_holdings_api.py -q` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same file plus `tests/test_web_security.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(holdings): API to list holdings and flip owner`.

---

### Task 9: Home tab UI

**Files:**
- Modify: `src/web/static/index.html` (Positions heading; new My holdings section after the positions `<section>`)
- Modify: `src/web/static/app.js` (`loadPortfolio`, action switch at the `data-action` handler)
- Modify: `src/web/static/style.css` (`.own-switch`, `.pos-mono.mine`, `.alloc-mine`, `.own-note`, copied from the mockup's `<style>` block)
- Modify: `tests/test_web_home_contract.py`

**Interfaces:**
- Consumes: `GET /api/holdings`, `POST /api/holdings/{ticker}/owner`, summary keys `user_holdings_eur`, `bot_equity_eur`, `trading_mode`, `broker` (Tasks 4, 8).
- Produces: element ids `holdingsSection`, `holdingsTitle`, `holdingsCount`, `holdingsContainer`; actions `set-owner` (`data-ticker`, `data-owner`), `toggle-holding`.

Behaviour (spec section 8 is the source of copy and layout):
- Heading text `Positions` → `Bot positions` (keep id `positionsTitle`).
- `loadPortfolio`: fetch `/api/holdings` in a separate promise so a failure only fills `holdingsContainer` with `Could not load your holdings. Pull to refresh.`. Hide `holdingsSection` when the list is empty or the mode is not live T212 (`summary.trading_mode === 'LIVE' && summary.broker === 'TRADING212'`).
- Hero `totalVal = cash + bot equity + (summary.user_holdings_eur || 0)`. Without this, the all-time return would drop by the holdings' value.
- Allocation bar: add a hatched `alloc-mine` span sized by `user_holdings_eur`. Legend line `Bot / Yours / Cash`; `Yours` only when > 0.
- Bot rows (live T212 only): slider block above the Sell button. Holding rows: same `.pos-row` markup, `pos-mono mine`, kv `Avg price`, `Now`, `P&L`, slider, note, no Sell button. Use `openPositions` for open state (same as bot rows).
- `set-owner` handler: ignore when the target side is already active; `confirm()` with the spec's text (cancel → nothing); set the switch `busy`, `aria-busy="true"` and the note to `Cancelling the stop at Trading 212…` (Bot → Mine) or `Handing KO to the bot…` (Mine → Bot); POST; then `showToast(message)` and `loadPortfolio()` on 200, or `showToast(detail, true)` and restore the old side on error.
- Slider is a `role="radiogroup"` with two `role="radio"` buttons. Left/Right arrow keys trigger the other side. Focus ring uses the existing `:focus-visible` style. Reduced motion turns the slide off.

- [ ] **Step 1: Extend the contract test**: add the four ids to the id list; add `test_app_js_owner_actions` asserting `"set-owner"` and `"/api/holdings"` appear in `app.js`. Run `python -m pytest tests/test_web_home_contract.py -q` → FAIL.
- [ ] **Step 2: Implement** HTML, CSS and JS.
- [ ] **Step 3: Run** `python -m pytest tests/test_web_home_contract.py tests/test_web_static_caching.py -q` → PASS.
- [ ] **Step 4: Visual check.** Start the app (`python run.py`), then use a Playwright script in the scratchpad that intercepts `/api/portfolio` (live T212 summary with `user_holdings_eur`), `/api/positions`, `/api/holdings` and `/api/holdings/*/owner` with `page.route`. Screenshot at 390×844 and 1280×900, `deviceScaleFactor: 2`: both slider states, the busy state, and an error toast (owner route returns 409). Also screenshot paper mode: no slider, no My holdings. Compare against the mockup. Check that no page scrolls sideways at 390 px.
- [ ] **Step 5: Commit** `feat(holdings): Home tab My holdings list and Bot | Mine slider`.

---

### Task 10: Money-path verification and wrap-up

**Files:** none new.

- [ ] **Step 1: Run the live-money-safety suite:**
  `python -m pytest tests/test_live_trading.py tests/test_paper_trading.py tests/test_broker_abstraction.py tests/test_broker_fees.py tests/test_triggers.py tests/test_trigger_gate.py tests/test_trigger_allocation.py tests/test_entry_sizing.py tests/test_whole_shares.py tests/test_min_one_share.py tests/test_capital_usage_limit.py tests/test_cash_flows.py tests/test_portfolio_sync.py -q` → all PASS.
- [ ] **Step 2: Run the full suite:** `python -m pytest tests/ -q` → all PASS.
- [ ] **Step 3: Paper smoke:** `python run.py` with default (paper) settings, load Home → no slider, no My holdings, positions as before.
- [ ] **Step 4: Push** the branch. In the PR description, list the interlocks touched: daily loss limit (now adds Mine holdings to "current"), buy path (`_plan_live_buy` refusal), trigger creation, trigger firing (Mine triggers cancelled), broker stop cancel on release. Recommend trying the flip on the T212 demo account before the real one.
