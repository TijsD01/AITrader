# Shadow exit tests, trigger cancel reasons, numeric track record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test later/no breakeven and a lower trigger threshold without real money, record why each trigger is cancelled, and replace the committee's noisy memory prompt with a small numeric track record linked to the report that opened each trade.

**Architecture:** Three serial tasks. Task 1 adds shadow presets (one new strategy field, one new shadow column). Task 2 adds `order_triggers.cancel_reason` and sets it wherever a trigger leaves PENDING without filling. Task 3 links trigger→report and position→trigger, adds `src/trading/trade_stats.py`, and swaps the committee prompt's cold-start memory and lesson stories for that numeric block.

**Tech Stack:** Python 3.13, async SQLAlchemy + aiosqlite, pytest (`asyncio_mode = auto`).

**Spec:** No spec file. Source: the findings in `reports/Trading bot memory gains.md` plus the production DB analysis from this session (summarized below). User approved these 3 items on 2026-11-09.

Evidence (production DB, Sep 6 – Nov 9, verified by query):
- 23 closed trades, 39% win. Wins without the one +20% outlier ≈ +1%, losses ≈ −4%. Most wins are stops at entry +1% after the 2% breakeven ratchet (`risk.py:487-512`).
- BUY verdicts mostly score 74–80; `TRIGGER_MIN_SCORE` is 82.
- 14 of 18 live triggers CANCELLED; no column records why.
- Memory atoms injected into the prompt are stale or wrong ("cash of €55.15 violates…", "Trigger armed … Ref #latest" with no trigger).

## Global Constraints

- Real-money path: `src/trading/live.py` changes in Tasks 2 and 3. Read `.claude/skills/live-money-safety/SKILL.md` first. Those changes only write bookkeeping columns; they must not change any order, sizing, stop, or cancel decision.
- Change paper and live together (`paper.py` mirrors `live.py`).
- New columns on existing tables need an entry in `_run_migrations` in `src/db/session.py` (no Alembic).
- Tests never hit a live LLM or broker. Settings are reset by `tests/conftest.py`; override per test with `monkeypatch.setattr(settings, ...)`.
- Code, comments, and commits are written in normal English (caveman style is chat-only).
- Minimum sample for any stat shown to the LLM: `MIN_N = 30` closed trades.
- Do not touch memory writes (`agent_memory_client.record_*`, `log_interaction`). Only stop reading memory into the prompt.

## Review Focus

1. Existing DB without the new columns: the migration must add them, and old rows (NULL `trigger_id` / `report_id` / `initial_stop_native`) must be skipped, not crash. Tested in Tasks 1 and 3.
2. Shadow positions opened before deploy have no `initial_stop_native`: `breakeven_r` must fall back to the effective `breakeven_pct`. Tested in Task 1.
3. A trade where stop ≥ entry or stop is NULL: R is skipped, never divided by zero. Tested in Task 3.
4. Stats query fails (DB error): the committee still runs with an empty block. Existing try/except stays. Tested in Task 3.
5. Live cancel that the broker does not confirm (`_cancel_trigger_locked` returns early, trigger stays PENDING): no `cancel_reason` is written. Tested in Task 2.

---

### Task 1: Shadow presets for exit and threshold tests

**Files:**
- Modify: `src/trading/shadow.py` (`ShadowStrategy` ~line 61, `PRESETS` ~line 90, position open ~line 529, monitor loop ~line 593)
- Modify: `src/db/models.py` (`ShadowPosition` ~line 243)
- Modify: `src/db/session.py` (`_run_migrations` list)
- Modify: `config/settings.py:90` (`SHADOW_STRATEGIES` default)
- Test: `tests/test_shadow.py`

**Interfaces:**
- Produces: `ShadowStrategy.breakeven_r: Optional[float] = None`; `ShadowPosition.initial_stop_native` (Float, nullable); presets `breakeven_1r`, `no_breakeven`, `score_76`; helper `effective_breakeven_pct(strategy, eff_pct: float, entry: float, initial_stop: Optional[float]) -> float` in `shadow.py`.

- [ ] **Step 1: Write the failing tests** in `tests/test_shadow.py`

```python
def test_new_presets_exist():
    assert PRESETS["breakeven_1r"].breakeven_r == 1.0
    assert PRESETS["no_breakeven"].breakeven_pct == 10.0
    assert PRESETS["score_76"].score_offset == -6
    assert PRESETS["score_76"].threshold(82) == 76

def test_effective_breakeven_pct():
    from src.trading.shadow import effective_breakeven_pct
    s = PRESETS["breakeven_1r"]
    # entry 100, initial stop 92 -> 1R = 8% gain
    assert effective_breakeven_pct(s, 0.02, 100.0, 92.0) == pytest.approx(0.08)
    # no initial stop (position opened before deploy) -> main's pct
    assert effective_breakeven_pct(s, 0.02, 100.0, None) == 0.02
    # stop at/above entry -> main's pct
    assert effective_breakeven_pct(s, 0.02, 100.0, 100.0) == 0.02
    # strategy without breakeven_r -> eff pct unchanged
    assert effective_breakeven_pct(PRESETS["baseline"], 0.02, 100.0, 92.0) == 0.02
```

Also extend the existing shadow open-position test (the one that calls the code at `shadow.py:529`) to assert `pos.initial_stop_native == t.stop_loss`.

- [ ] **Step 2: Run** `python -m pytest tests/test_shadow.py -q` — expect FAIL (KeyError / ImportError).

- [ ] **Step 3: Implement**
  - `breakeven_r` field with comment: gain in multiples of the initial stop distance at which the stop moves to entry +1%; overrides `breakeven_pct`.
  - `effective_breakeven_pct`: if `strategy.breakeven_r` and `initial_stop` and `entry > initial_stop`: return `breakeven_r * (entry - initial_stop) / entry`; else `eff_pct`.
  - Presets (descriptions in plain English; each says it is the baseline rules plus one change):
    - `breakeven_1r`: `breakeven_r=1.0`
    - `no_breakeven`: `breakeven_pct=10.0` (a 1000% gain, never reached: holds to stop or target; the ATR trail also never starts)
    - `score_76`: `score_offset=-6` (description: "6 points below the main threshold frozen at start, 76 when the main bot is at 82")
  - `ShadowPosition.initial_stop_native = Column(Float, nullable=True)`; migration `("shadow_positions", "initial_stop_native", "FLOAT")`; set `initial_stop_native=t.stop_loss` at `shadow.py:529`.
  - Monitor loop (`shadow.py:593`): pass `effective_breakeven_pct(s, eff["breakeven_pct"], p.entry_price_native, p.initial_stop_native)` instead of `eff["breakeven_pct"]`.
  - `SHADOW_STRATEGIES` default: `"aggressive,baseline,breakeven_1r,no_breakeven,score_76"`. `baseline` is the control the others are compared against.

- [ ] **Step 4: Run** `python -m pytest tests/test_shadow.py -q` — expect PASS. Check that the existing test at line ~55 (iterates `list(PRESETS)`) still passes with the new presets.

- [ ] **Step 5: Commit** — `git commit -m "Add breakeven and threshold shadow presets"`

---

### Task 2: Record why a trigger was cancelled or expired

**Files:**
- Modify: `src/db/models.py` (`OrderTrigger` ~line 167), `src/db/session.py`
- Modify: `src/trading/live.py` lines 1383-1396, 1430, 1680-1684, 1708-1712, 1738-1744, 1836-1846, 1870-1875
- Modify: `src/trading/paper.py` lines 780, 896-900, 922-926, 960-964
- Modify: `scripts/memory_stats.py` (one `show(...)` line)
- Test: `tests/test_triggers.py` (paper) and `tests/test_live_trading.py` (live; reuse its existing T212 mock setup)

**Interfaces:**
- Produces: `OrderTrigger.cancel_reason = Column(String(255), nullable=True)`; `LiveBroker._cancel_trigger_locked(session, trigger, reason: str = "Cancelled by user")`.

Reasons (exact text, set on the same line as each `status` change and nowhere else):

| Site | Reason |
|---|---|
| manual cancel (`paper.py:780`, `live.py:1394` default) | `"Cancelled by user"` |
| `release_to_user` (`live.py:1430`) | `"Ticker released to user"` |
| expiry (`live.py:1682`, `paper.py:898`, and the expired branch of `live.py:1838/1844`) | `f"Expired after {settings.TRIGGER_EXPIRY_DAYS} days"` |
| setup invalid (`live.py:1710`, `paper.py:924`, invalid branch of `live.py:1838/1844`) | the `invalid_reason` / `invalid` string, truncated to 255 |
| unaffordable (`live.py:1742`, `live.py:1873`, `paper.py:963`) | `"Unaffordable: " + message`, truncated to 255 (`live.py:1873` uses `plan.get("message", "")`) |

- [ ] **Step 1: Write failing tests**: paper trigger that expires → `cancel_reason.startswith("Expired after")`; paper trigger whose price is below its stop → `cancel_reason` contains `"stop-loss"`; paper manual cancel → `"Cancelled by user"`; live `_cancel_trigger_locked` with a mocked T212 cancel that is NOT confirmed (`broker_order_id` stays set) → status `PENDING` and `cancel_reason is None`.
- [ ] **Step 2: Run** them — expect FAIL.
- [ ] **Step 3: Implement** the column, the migration `("order_triggers", "cancel_reason", "VARCHAR(255)")`, and the table above. Add to `scripts/memory_stats.py`: `show("triggers: cancel reasons", q(tb, "SELECT status, cancel_reason, COUNT(*) FROM order_triggers WHERE status IN ('CANCELLED','EXPIRED') GROUP BY 1,2"))`.
- [ ] **Step 4: Run** the live-money test list from `.claude/skills/live-money-safety/SKILL.md` — expect PASS.
- [ ] **Step 5: Commit** — `git commit -m "Record why price triggers are cancelled or expire"`

---

### Task 3: Link trades to reports and feed the committee a numeric track record

**Files:**
- Modify: `src/db/models.py` (`OrderTrigger.report_id`, `Position.trigger_id`), `src/db/session.py`
- Modify: `src/trading/base.py:48`, `src/trading/paper.py:559` and `:255`, `src/trading/live.py:1182`, `:726`, `:902-960`, `:1781`
- Modify: `src/scheduler/jobs.py:355` (pass `report_id=rep_entry.id`; `rep_entry` is committed at line 317 and `expire_on_commit=False`, so `.id` is set)
- Create: `src/trading/trade_stats.py`
- Modify: `src/agents/committee.py:58-73`, `src/trading/learner.py:603-641`
- Test: `tests/test_trade_stats.py` (new), `tests/test_learner.py:124`

**Interfaces:**
- Produces:
  - `OrderTrigger.report_id = Column(Integer, nullable=True)`; `Position.trigger_id = Column(Integer, nullable=True)`; migrations `("order_triggers", "report_id", "INTEGER")`, `("positions", "trigger_id", "INTEGER")`.
  - `create_price_trigger(..., ai_rationale: str, report_id: Optional[int] = None)` on base, paper and live; stored on the trigger. Other callers (`routes.py`, `telegram/bot.py`, opportunity scanner `jobs.py:632`, which saves no report) stay unchanged and get NULL.
  - `_book_live_fill(..., trigger_id: Optional[int] = None)`: callers at `live.py:726` pass `trigger_id=trigger_id`, at `live.py:1781` pass `trigger_id=trigger.id`; sets `Position.trigger_id`. Paper `execute_buy` sets `Position(trigger_id=trigger_id)` at `paper.py:255`.
  - `trade_stats.py`:
    - `MIN_N = 30`
    - `def summarize_pct(pnl_pcts: list[float], min_n: int = MIN_N) -> str`
    - `def summarize_r(rows: list[tuple[str, float, float, float]], min_n: int = MIN_N) -> list[str]`: rows are `(trigger_type, entry, exit, initial_stop)`; one line per trigger_type.
    - `async def track_record_block() -> str`: loads SELL `Trade.realized_pnl_pct` (not NULL) and closed `Position` joined to `OrderTrigger` on `trigger_id` (entry = `entry_price_native`, exit = `current_price_native`, stop = `OrderTrigger.stop_loss`). Import `src.db.session` as a module and read `AsyncSessionLocal` at call time (see CLAUDE.md test isolation).

Block format (exact):

```text
=== TRACK RECORD (closed bot trades, numbers only) ===
All trades: n=40, win 45%, avg win +3.10%, avg loss -2.20%, expectancy +0.19% per trade
LIMIT_PULLBACK: n=31, win 42%, avg R +0.12
BREAKOUT_BUY: n=4, below 30, too few to judge
```

Below `MIN_N` the "All trades" line reads `All trades: n=23, below 30, too few to judge`. R = `(exit - entry) / (entry - stop)`; skip rows where stop is None or `stop >= entry`.

- [ ] **Step 1: Write failing tests** in `tests/test_trade_stats.py`:

```python
from src.trading.trade_stats import summarize_pct, summarize_r

def test_pct_below_min_n():
    assert summarize_pct([1.0] * 29) == "All trades: n=29, below 30, too few to judge"

def test_pct_stats():
    line = summarize_pct([2.0] * 12 + [-1.0] * 18)
    assert line == "All trades: n=30, win 40%, avg win +2.00%, avg loss -1.00%, expectancy +0.20% per trade"

def test_r_skips_bad_stops_and_groups():
    rows = [("LIMIT_PULLBACK", 100.0, 110.0, 90.0)] * 30 + [("LIMIT_PULLBACK", 100.0, 90.0, 100.0), ("BREAKOUT_BUY", 100.0, 95.0, None)]
    assert summarize_r(rows) == ["LIMIT_PULLBACK: n=30, win 100%, avg R +1.00", "BREAKOUT_BUY: n=0, below 30, too few to judge"]
```

Plus one async test: seed the test DB with a closed `Position` (with `trigger_id`), its `OrderTrigger` (`stop_loss` set) and a SELL `Trade`; assert `track_record_block()` starts with the header and contains `"All trades: n=1"`. Plus a paper test: `create_price_trigger(..., report_id=7)` → trigger row has `report_id == 7`; trigger fires → the new `Position.trigger_id` equals the trigger id.

- [ ] **Step 2: Run** `python -m pytest tests/test_trade_stats.py -q` — expect FAIL (module missing).
- [ ] **Step 3: Implement** the columns, the migrations, `report_id` / `trigger_id` threading, and `trade_stats.py`.
- [ ] **Step 4: Swap the prompt inputs.**
  - `committee.py:58-67`: replace the `get_cold_start_loadout` call with `memory_context = await trade_stats.track_record_block()` inside the existing try/except.
  - `learner.py:633-639`: delete the lessons loop and the "Trade learning engine is active…" line; keep the parameter lines. Remove the now-unused lessons query.
  - `tests/test_learner.py:124` assertions still hold; add `assert "Key Lessons" not in context`.
- [ ] **Step 5: Run** `python -m pytest tests/test_trade_stats.py tests/test_learner.py tests/test_agents.py tests/test_agent_memory.py -q`, then the live-money test list, then `python -m pytest tests/ -q` — expect PASS.
- [ ] **Step 6: Commit** — `git commit -m "Link triggers to reports and give the committee a numeric track record"`
