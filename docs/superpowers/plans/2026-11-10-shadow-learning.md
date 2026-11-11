# Shadow Learning, Outcome Memory, Promote to Main: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shadows become DB-backed and editable in the UI, learn their own parameters from outcome memory through the LLM, and can be promoted by hand onto the main bot with full parameter parity.

**Architecture:** One parameter schema (`strategy_params.py`) feeds UI sliders, API validation, learner clamps and promote. Shadow strategies live in a `shadow_strategies` table behind an in-memory cache, so `get_strategies()` stays synchronous. Closed trades write outcome atoms into the existing memory store; `ShadowLearner` reads them, asks `LLMClient.generate_json` for a proposal and clamps it. `main_strategy.py` resolves the main bot's rule set from `SystemConfig`; promote writes that rule set.

**Tech Stack:** Python 3.13, FastAPI, async SQLAlchemy + aiosqlite, pytest (`asyncio_mode = auto`), vanilla JS in `src/web/static/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-10-shadow-learning-design.md`. Read it first.

## Execution Rules (owner, 2026-11-10)

**Model per task.** Each task has a `Model:` line. Dispatch the implementer subagent with that model.

| Model | Used for |
|---|---|
| `haiku` | Very simple, repetitive work: columns, settings, mechanical rewiring |
| `sonnet` | Medium difficulty |
| `opus` | High difficulty, and **every task that touches the money path** |

Money path = `src/trading/live.py`, `paper.py`, `broker.py`, `risk.py`, `fees.py`, `trigger_gate.py`, `cash_flows.py`, `main_strategy.py`, order sizing, kill switch, the buy path in `src/scheduler/jobs.py`. Any task that edits one of these runs on `opus`, whatever its size.

**Implementer skills.** Every implementer invokes the `ponytail` skill first (smallest change that fully solves the task). Money-path implementers also invoke `live-money-safety`. TDD: failing test first.

**UI tasks (5 and 17).** The implementer also invokes `frontend-design:frontend-design`, `unslop-ui-design` and `anthropic-skills:ui-animation-guidelines` before writing UI code, and keeps the app's existing visual language (`ui-*` classes, CSS variables).

**Baseline.** Before this work, `python -m pytest tests/ -q` gives 901 passed, 2 failed (`test_antigravity_agent.py::test_antigravity_llm_client`, `test_portfolio_sync.py::test_portfolio_summary_refreshes_unrealized_pnl`). "Suite passes" below means: no failures beyond these two.

**Review gate.** After each task, a **fresh** subagent (no shared context with the implementer) runs `/ponytail-review` on that task's commit. Reviewer model: `opus` for money-path tasks, `sonnet` otherwise. Findings are fixed by the implementer model before the next task starts. No task is done with open findings.

**Phase gate.** After the last task of a phase: `python -m pytest tests/ -q` must pass, then push. Each phase ships alone.

## Global Constraints

- Tests never call a live LLM. Mock `LLMClient.generate_json`.
- Tests override `settings` per test; conftest forces `TRADING_MODE="PAPER"`, `LIVE_TRADING_ENABLED=False`.
- Import `src.db.session` as a module where the engine must follow test rebinding.
- New table → `Base.metadata.create_all`. New column on an existing table → entry in `_run_migrations` in `src/db/session.py`.
- Paper and live change together. `PaperBroker` mirrors `LiveBroker`.
- Never weaken an interlock to pass a test.
- With default rules, main-bot behaviour is unchanged: the full existing suite passes untouched through every phase.
- Learner never changes `accept_hold`, `ignore_risk_veto`, `entry_mode`.
- Strength step caps: low 5%, medium 10%, high 20% of a param's range. Max 3 params per learning run.
- `SHADOW_LEARN_MIN_TRADES = 10`. `SHADOW_MAX_STRATEGIES = 12`. Manual learn needs >= 5 closed trades.
- LIVE promote with `ignore_risk_veto`, `accept_hold` or `entry_mode=market` needs `confirm_text == "PROMOTE LIVE"`.
- Code, comments, commits, docs: normal prose (AGENTS.md boundary). No model names in commits or code.

## Plan refinement of the spec

Spec says `get_strategies()` becomes an async DB read. Plan keeps it **synchronous** over a cache that `load_strategies()` fills at startup and after every write. Same behaviour, no caller changes. Everything else follows the spec.

## Review Focus

1. **Edit while a position is open.** Changing a shadow's params must not move stops or targets of its open positions. Test in Task 3.
2. **Learner reply is hostile or sloppy.** Strings for numbers, `NaN`, extra keys, a list instead of an object: no change, no crash. Test in Task 9.
3. **Delete during a monitor tick.** A shadow deleted while `check_exits` holds its row must not raise or resurrect the account. Test in Task 3.
4. **Promote, then the old learner runs.** `calibrate_internal_parameters(automatic=True)` after a promote must not overwrite promoted values. Test in Task 16.
5. **`ignore_risk_veto` meets a hard block.** Risk Officer veto skipped, but kill switch on / `validate_buy_order` fail / no stop level: no order, paper and live. Test in Tasks 13 and 14.

---

# Phase 1: shadows in DB, editable

### Task 1: Parameter schema

**Model:** sonnet

**Files:**
- Create: `src/trading/strategy_params.py`
- Test: `tests/test_strategy_params.py`

**Interfaces:**
- Produces:
  - `ParamSpec` frozen dataclass: `name, label, kind, min, max, step, nullable, learnable, choices=()`, plus `learn_min: float | None = None`, `learn_max: float | None = None` (learner range when narrower than the edit range).
  - `SPECS: dict[str, ParamSpec]`, one per `ShadowStrategy` field except `key`, `label`, `description`. Bounds: copy the table in the spec, section "Phase 1 / Schema". `breakeven_pct`: `learn_min=0.005, learn_max=0.10`.
  - `validate(params: dict) -> dict`: returns a cleaned copy; raises `ValueError(f"{name}: ...")` on unknown key, wrong type, out of range, `None` on a non-nullable param, NaN/inf.
  - `STRENGTH_SHARE = {1: 0.05, 2: 0.10, 3: 0.20}`
  - `clamp_step(name: str, old, proposed, strength: int) -> value`: returns `old` when the param is not learnable, strength is 0, or `proposed` is not a finite number; else limits the move to `STRENGTH_SHARE[strength] * (hi - lo)`, clamps to `[lo, hi]` (learn range when set), rounds to `step`, casts ints. `old is None` → treat as no move allowed (returns `None`).
  - `to_json_schema() -> list[dict]`: `dataclasses.asdict` per spec, in `SPECS` order.

- [ ] **Step 1: Write failing tests**

```python
import math, pytest
from src.trading.strategy_params import SPECS, validate, clamp_step, to_json_schema
from src.trading.shadow import PRESETS

def test_every_preset_is_valid():
    for p in PRESETS.values():
        assert validate(p.to_params()) == p.to_params()

def test_specs_cover_every_strategy_param():
    assert set(SPECS) == set(PRESETS["baseline"].to_params())

@pytest.mark.parametrize("bad", [{"stop_scale": 9}, {"nope": 1}, {"stop_scale": "x"},
                                 {"stop_scale": None}, {"stop_scale": math.nan}, {"entry_mode": "limit"}])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        validate({**PRESETS["baseline"].to_params(), **bad})

def test_clamp_step_caps_move_by_strength():
    # stop_scale range 0.5..2.0 = 1.5 wide; low strength = 5% = 0.075 → rounds to step 0.05
    assert clamp_step("stop_scale", 1.0, 2.0, 1) == pytest.approx(1.05)
    assert clamp_step("stop_scale", 1.0, 2.0, 3) == pytest.approx(1.30)
    assert clamp_step("stop_scale", 1.0, 0.0, 3) == pytest.approx(0.70)

def test_clamp_step_ignores_non_learnable_and_garbage():
    assert clamp_step("ignore_risk_veto", False, True, 3) is False
    assert clamp_step("accept_hold", False, True, 3) is False
    assert clamp_step("entry_mode", "trigger", "market", 3) == "trigger"
    assert clamp_step("stop_scale", 1.0, "big", 3) == 1.0
    assert clamp_step("stop_scale", 1.0, math.inf, 3) == 1.0
    assert clamp_step("stop_scale", 1.0, 2.0, 0) == 1.0

def test_clamp_step_uses_learn_range():
    assert clamp_step("breakeven_pct", 0.09, 5.0, 3) <= 0.10

def test_json_schema_shape():
    row = next(r for r in to_json_schema() if r["name"] == "stop_scale")
    assert row["min"] == 0.5 and row["max"] == 2.0 and row["step"] == 0.05
```

- [ ] **Step 2:** Run `python -m pytest tests/test_strategy_params.py -q`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement the module per Interfaces.
- [ ] **Step 4:** Run again. Expected: PASS.
- [ ] **Step 5:** Commit: `Add shared strategy parameter schema`.

### Task 2: Tables and settings

**Model:** haiku

**Files:**
- Modify: `src/db/models.py` (after `ShadowSnapshot`), `config/settings.py` (after `SHADOW_CUSTOM_STRATEGIES`)
- Test: `tests/test_shadow_store.py` (new file, first test)

**Interfaces:**
- Produces:
  - `ShadowStrategyRow` (`__tablename__ = "shadow_strategies"`): columns exactly as the spec table "Table `shadow_strategies`".
  - `ShadowParamChange` (`__tablename__ = "shadow_param_changes"`): `id` PK autoincrement, `strategy` String(40) indexed, `source` String(20), `changes` JSON, `reason` Text nullable, `stats` JSON nullable, `created_at` DateTime default utcnow indexed.
  - `settings.SHADOW_LEARN_MIN_TRADES: int = 10`, `settings.SHADOW_MAX_STRATEGIES: int = 12`.

- [ ] **Step 1: Failing test**

```python
from config.settings import settings
from src.db.models import ShadowStrategyRow, ShadowParamChange

def test_tables_and_settings_exist():
    assert ShadowStrategyRow.__tablename__ == "shadow_strategies"
    assert {"key", "params", "learning_strength", "enabled", "copied_from", "is_preset",
            "closed_at_last_learn"} <= set(ShadowStrategyRow.__table__.columns.keys())
    assert ShadowParamChange.__tablename__ == "shadow_param_changes"
    assert settings.SHADOW_LEARN_MIN_TRADES == 10 and settings.SHADOW_MAX_STRATEGIES == 12
```

- [ ] **Step 2:** Run, expect FAIL (ImportError).
- [ ] **Step 3:** Add the two models and two settings.
- [ ] **Step 4:** Run `python -m pytest tests/test_shadow_store.py tests/test_shadow.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add shadow strategy and parameter history tables`.

### Task 3: Strategy store (seed, cache, CRUD, history)

**Model:** sonnet

**Files:**
- Modify: `src/trading/shadow.py` (`_custom_strategies`, `get_strategies` at lines ~156-204; `ShadowPortfolios.reset` area), `src/web/app.py` (`lifespan`, after DB init)
- Test: `tests/test_shadow_store.py`, `tests/conftest.py` (reset cache per test if the existing shadow fixtures need it)

**Interfaces:**
- Consumes: Task 1 `validate`; Task 2 models.
- Produces (module-level in `src/trading/shadow.py`):
  - `async load_strategies() -> None`: seeds the table when empty from `settings.SHADOW_STRATEGIES` (presets + `_custom_strategies()`; `is_preset=True` for keys in `PRESETS`), then fills `_CACHE: list[tuple[ShadowStrategy, int]]` (strategy, learning_strength) from enabled rows ordered by `created_at`.
  - `get_strategies() -> list[ShadowStrategy]`: sync. `[]` when `SHADOW_ENABLED` is False. Returns cache; when the cache was never loaded, falls back to today's env-based result (keeps existing tests and cold imports working).
  - `learning_strengths() -> dict[str, int]`
  - `slug(label: str) -> str`: lowercase, non `[a-z0-9]` → `_`, trimmed, max 40.
  - `async create_strategy(label: str, params: dict, description: str = "", copied_from: str | None = None) -> str`: raises `ValueError` on empty/duplicate/`main` key, invalid params, or `SHADOW_MAX_STRATEGIES` reached. Returns key.
  - `async copy_strategy(key: str, label: str) -> str`: same params, `copied_from=key`, `learning_strength=0`. `LookupError` when `key` is unknown.
  - `async apply_param_change(key: str, new_params: dict, source: str, reason: str = "", stats: dict | None = None) -> dict`: validates the merged params, saves, writes one `ShadowParamChange` with `changes = {name: {"old": .., "new": ..}}` for changed names only, reloads the cache. Returns `changes`. No row when nothing changed.
  - `async update_strategy(key: str, *, label=None, description=None, learning_strength=None, enabled=None) -> None`: `learning_strength` must be 0..3.
  - `async delete_strategy(key: str) -> bool`: deletes the strategy row plus `ShadowAccount`, `ShadowPosition`, `ShadowTrigger`, `ShadowSnapshot` rows of that key, under `shadow_portfolios._lock`. Reloads the cache.
  - `async undo_param_change(change_id: int) -> dict`: applies the `old` values of that row with `source="undo"`.
- All writers call `load_strategies()` before returning.

- [ ] **Step 1: Failing tests** (each test starts by deleting all `ShadowStrategyRow` rows and calling `load_strategies()`)

```python
async def test_seed_from_env_then_db_is_truth():
    settings.SHADOW_STRATEGIES = "baseline,aggressive"
    await load_strategies()
    assert [s.key for s in get_strategies()] == ["baseline", "aggressive"]
    settings.SHADOW_STRATEGIES = "momentum"
    await load_strategies()
    assert [s.key for s in get_strategies()] == ["baseline", "aggressive"]

async def test_create_copy_delete():
    key = await create_strategy("My Test", {**PRESETS["baseline"].to_params(), "stop_scale": 1.5})
    assert key == "my_test"
    copy = await copy_strategy(key, "My Copy")
    assert next(s for s in get_strategies() if s.key == copy).stop_scale == 1.5
    assert learning_strengths()[copy] == 0
    assert await delete_strategy(key) is True
    assert key not in [s.key for s in get_strategies()]

async def test_create_rejects_duplicate_main_invalid_and_cap():
    with pytest.raises(ValueError): await create_strategy("main", PRESETS["baseline"].to_params())
    with pytest.raises(ValueError): await create_strategy("Bad", {"stop_scale": 99})
    await create_strategy("Dup", PRESETS["baseline"].to_params())
    with pytest.raises(ValueError): await create_strategy("Dup", PRESETS["baseline"].to_params())
    settings.SHADOW_MAX_STRATEGIES = len(get_strategies())
    with pytest.raises(ValueError): await create_strategy("Over", PRESETS["baseline"].to_params())

async def test_apply_param_change_writes_history_and_undo_restores():
    changes = await apply_param_change("baseline", {"stop_scale": 1.2}, source="user", reason="try")
    assert changes == {"stop_scale": {"old": 1.0, "new": 1.2}}
    row = (await _rows(ShadowParamChange))[-1]
    assert row.source == "user" and row.reason == "try"
    await undo_param_change(row.id)
    assert next(s for s in get_strategies() if s.key == "baseline").stop_scale == 1.0
    assert await apply_param_change("baseline", {"stop_scale": 1.0}, source="user") == {}

# Review Focus 1
async def test_edit_does_not_move_open_position_levels():
    # open a baseline shadow position via follow_report + check_triggers (helpers as in tests/test_shadow.py),
    # record stop_loss_native / take_profit_native, apply_param_change(stop_scale=2.0, reward_risk=4.0),
    # reload the position: both levels unchanged.

# Review Focus 3
async def test_delete_removes_all_rows_and_exit_check_survives():
    # open a position on a custom strategy, delete_strategy(key), then await shadow_portfolios.check_exits():
    # no exception; no ShadowAccount / ShadowPosition / ShadowTrigger / ShadowSnapshot rows with that key.
```

- [ ] **Step 2:** Run `python -m pytest tests/test_shadow_store.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Call `await load_strategies()` in `lifespan` after table creation.
- [ ] **Step 4:** Run `python -m pytest tests/test_shadow_store.py tests/test_shadow.py tests/test_ticker_detail.py -q`. Expected: PASS, existing shadow tests unchanged.
- [ ] **Step 5:** Commit: `Store shadow strategies in the database with change history`.

### Task 4: Shadow management API

**Model:** sonnet

**Files:**
- Modify: `src/web/routes.py` (shadow block at ~841-860), `src/trading/shadow.py` (`get_comparison`: add `learning_strength`, `copied_from`, `is_preset` per strategy)
- Test: `tests/test_shadow_api.py`

**Interfaces:**
- Consumes: Task 1 `to_json_schema`; Task 3 functions.
- Produces: routes from the spec table "Phase 1 / API", except `/learn` (Task 10) and `/promote` (Task 16). Error mapping: `ValueError` → 422 with the message as `detail`; `LookupError` → 404. `PATCH` with `params` calls `apply_param_change(key, params, source="user")`. `GET /api/shadow/{key}/history` returns newest first, max 50: `[{id, source, changes, reason, created_at}]`.
- Auth: same dependency the existing `POST /api/shadow/{strategy_key}/reset` route gets (see `src/web/security.py`).

- [ ] **Step 1: Failing tests** using the client fixture pattern from `tests/test_api_and_screener.py`:

```python
async def test_schema_lists_all_params(client):
    names = {r["name"] for r in (await client.get("/api/shadow/schema")).json()}
    assert "stop_scale" in names and "ignore_risk_veto" in names

async def test_create_edit_copy_delete_roundtrip(client):
    r = await client.post("/api/shadow", json={"label": "Api One", "base": "aggressive", "params": {"stop_scale": 1.5}})
    assert r.status_code == 200 and r.json()["key"] == "api_one"
    assert (await client.patch("/api/shadow/api_one", json={"params": {"stop_scale": 1.1}, "learning_strength": 2})).status_code == 200
    hist = (await client.get("/api/shadow/api_one/history")).json()
    assert hist[0]["changes"]["stop_scale"] == {"old": 1.5, "new": 1.1}
    assert (await client.post(f"/api/shadow/api_one/history/{hist[0]['id']}/undo")).status_code == 200
    assert (await client.post("/api/shadow/api_one/copy", json={"label": "Api Two"})).json()["key"] == "api_two"
    assert (await client.delete("/api/shadow/api_one")).status_code == 200
    assert (await client.delete("/api/shadow/api_one")).status_code == 404

async def test_invalid_input_is_422_with_field_name(client):
    r = await client.patch("/api/shadow/baseline", json={"params": {"stop_scale": 99}})
    assert r.status_code == 422 and "stop_scale" in r.json()["detail"]
    assert (await client.patch("/api/shadow/baseline", json={"learning_strength": 7})).status_code == 422

async def test_comparison_exposes_learning_fields(client):
    s = (await client.get("/api/shadow")).json()["strategies"][0]
    assert {"learning_strength", "copied_from", "is_preset"} <= set(s)
```

`POST /api/shadow` with `base`: start from `PRESETS[base].to_params()` (default `baseline`), overlay `params`. Check the real key of the strategies list in the `GET /api/shadow` payload and use it in the last test.

- [ ] **Step 2:** Run, expect FAIL (404/405).
- [ ] **Step 3:** Implement routes and the three payload fields.
- [ ] **Step 4:** Run `python -m pytest tests/test_shadow_api.py tests/test_web_security.py tests/test_shadow.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add shadow create, copy, edit, delete and history API`.

### Task 5: Shadow tab UI

**Model:** sonnet

**Files:**
- Modify: `src/web/static/app.js` (dispatch switch at ~160; shadow block 2986-3210: `renderShadowStrategy`, `loadShadow`), the HTML template holding the shadow tab header (find with `grep -rn "tab-shadow\|shadow" src/web/templates src/web/static/*.html`)
- Test: `tests/js/features.test.mjs` only when the new code adds a pure helper there; otherwise the manual check below.

**Interfaces:**
- Consumes: Task 4 routes.
- Produces: `data-action` values `new-shadow`, `edit-shadow`, `copy-shadow`, `delete-shadow`, `set-shadow-strength`, `undo-shadow-change`; functions `openShadowEditor(key|null)`, `saveShadowEditor()`, `loadShadowHistory(key)`.

Implementer also invokes the `unslop-ui-design` skill. Follow existing `ui-*` classes and the `data-action` dispatch; no new libraries.

- [ ] **Step 1:** Header button "New shadow". Per card: Edit, Copy, Delete next to Reset, all `min-height:44px`.
- [ ] **Step 2:** Editor sheet built from `GET /api/shadow/schema` (fetch once, cache): `<input type="range">` with live value for `int`/`float` (min, max, step from schema), toggle for `bool`, `<select>` for `choice`. Nullable param: "Use main bot value" toggle that sends `null`. Fields: label, description. Save → `POST /api/shadow` (new) or `PATCH`. Show the 422 `detail` inline.
- [ ] **Step 3:** Learning strength: 4-stop range slider per card labelled Off / Low / Medium / High → `PATCH {learning_strength}`. Badge "Learning" when > 0. Under the slider, when > 0: "Under 30 closed trades the result is noise. A learning shadow is no longer a clean comparison."
- [ ] **Step 4:** History list (collapsed by default): date, source, `param: old → new`, reason, Undo button.
- [ ] **Step 5:** Copy asks for a label (`prompt`-style dialog the app already uses). Delete confirms: "Delete shadow '<label>' with all its positions and history?"
- [ ] **Step 6: Verify.** `python run.py`, then with Playwright (pre-installed) at 390px and 1280px width: create a shadow, move `stop_scale`, save, set strength Medium, copy, undo the change, delete. Screenshot each state; no horizontal scroll; no console errors. Run `python -m pytest tests/test_web_modules_contract.py tests/test_web_home_contract.py tests/test_web_static_caching.py -q` and `node --test tests/js/`. Expected: PASS.
- [ ] **Step 7:** Commit: `Add shadow editor, learning strength slider and history to the shadow tab`.

**Phase 1 gate:** full suite, push.

---

# Phase 2: outcome memory

### Task 6: Outcome atoms (write + read)

**Model:** sonnet

**Files:**
- Create: `src/memory/outcomes.py`
- Modify: `src/memory/store.py` (add `search_outcomes` after `search_l1_atoms`)
- Test: `tests/test_outcome_memory.py`

**Interfaces:**
- Produces:
  - `async record_outcome(*, source: str, strategy: str, ref_id: int | str, ticker: str, pnl_pct: float, r_multiple: float | None, hold_days: float, exit_reason: str, score: int | None, entry_type: str, params: dict) -> bool`. `source` in `("main_live", "main_paper", "shadow")`. Writes an `L1Atom`: `type="episodic"`, `agent_id="outcomes"`, `atom_id=f"outcome:{source}:{ref_id}"`, `subject=ticker`, `predicate="closed_trade"`, `object=f"{source}:{strategy}"`, `content = f"{summary} ||| {json}"` where `json` is every keyword argument. Returns `False` and logs a warning on any exception or when the `atom_id` already exists. Never raises.
  - `AgentMemoryStore.search_outcomes(strategy: str | None = None, source: str | None = None, ticker: str | None = None, exit_reason: str | None = None, since: float | None = None, limit: int = 50) -> list[dict]`: newest first; each dict is the parsed JSON tail. `source="main"` matches both `main_live` and `main_paper`. `exit_reason` is a substring match.

- [ ] **Step 1: Failing tests**

```python
async def test_record_then_search_by_strategy_and_source():
    assert await record_outcome(source="shadow", strategy="aggressive", ref_id=1, ticker="AAA", pnl_pct=4.2,
        r_multiple=1.4, hold_days=3.0, exit_reason="Take-Profit hit", score=84, entry_type="LIMIT_PULLBACK",
        params={"stop_scale": 1.25})
    await record_outcome(source="main_paper", strategy="main", ref_id=7, ticker="BBB", pnl_pct=-2.0,
        r_multiple=-1.0, hold_days=1.0, exit_reason="Stop-Loss hit", score=81, entry_type="BREAKOUT_BUY", params={})
    got = await memory_store.search_outcomes(strategy="aggressive")
    assert len(got) == 1 and got[0]["ticker"] == "AAA" and got[0]["params"] == {"stop_scale": 1.25}
    assert [o["ticker"] for o in await memory_store.search_outcomes(source="main")] == ["BBB"]
    assert await memory_store.search_outcomes(exit_reason="Stop-Loss") == await memory_store.search_outcomes(source="main")

async def test_record_is_idempotent():
    kw = dict(source="shadow", strategy="s", ref_id=5, ticker="CCC", pnl_pct=1.0, r_multiple=None, hold_days=1.0,
              exit_reason="x", score=None, entry_type="MARKET", params={})
    assert await record_outcome(**kw) is True
    assert await record_outcome(**kw) is False
    assert len(await memory_store.search_outcomes(strategy="s")) == 1

async def test_record_never_raises(monkeypatch):
    async def boom(*a, **k): raise RuntimeError("db down")
    monkeypatch.setattr(memory_store, "add_l1_atom", boom)
    assert await record_outcome(source="shadow", strategy="s", ref_id=9, ticker="DDD", pnl_pct=0.0, r_multiple=None,
        hold_days=0.0, exit_reason="x", score=None, entry_type="MARKET", params={}) is False
```

- [ ] **Step 2:** Run `python -m pytest tests/test_outcome_memory.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Check how `add_l1_atom` treats a caller-supplied `atom_id`; add the existence check in `record_outcome` if the store does not dedupe.
- [ ] **Step 4:** Run with `tests/test_agent_memory.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add trade outcome atoms to agent memory`.

### Task 7: Shadow closes write outcomes

**Model:** sonnet

**Files:**
- Modify: `src/trading/shadow.py` (`_check_exits` ~598-646, `_close_position` ~648-672)
- Test: `tests/test_outcome_memory.py`

**Interfaces:**
- Consumes: Task 6 `record_outcome`.
- Produces: `_close_position` return dict gains `position_id, pnl_pct, r_multiple, hold_days, score, entry_type, params` (`params` = that strategy's `to_params()`; `entry_type` from the linked `ShadowTrigger.trigger_type`, else `"UNKNOWN"`; `r_multiple = (exit - entry) / (entry - initial_stop)` when `initial_stop_native` is set and below entry, else `None`). `check_exits` calls `record_outcome(source="shadow", ref_id=position_id, ...)` for each close **after** the session commit and outside `self._lock`.

- [ ] **Step 1: Failing test:** open a baseline shadow position (helpers from `tests/test_shadow.py`), patch the quote below the stop, `await shadow_portfolios.check_exits()`; assert `search_outcomes(strategy="baseline")` has one row with `exit_reason` starting `"Stop-Loss hit"`, `r_multiple` about `-1.0`, `source == "shadow"`. Second test: `record_outcome` patched to raise → `check_exits` still returns the close.
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest tests/test_outcome_memory.py tests/test_shadow.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Record shadow trade outcomes in memory`.

### Task 8: Main closes write outcomes; cold-start filter; pruning

**Model:** opus (edits `paper.py` and `live.py`)

**Files:**
- Modify: `src/trading/paper.py` (sell path at ~450, next to `conduct_trade_post_mortem`), `src/trading/live.py` (matching sell path: `grep -n "conduct_trade_post_mortem" src/trading/live.py`), `src/memory/cold_start.py` (atom recall in `generate_cold_start_loadout`), `src/memory/store.py` (`prune_and_compact`)
- Test: `tests/test_outcome_memory.py`

**Interfaces:**
- Consumes: Task 6.
- Produces: main sells call `record_outcome(source="main_live" | "main_paper", strategy="main", ref_id=trade.id, ...)` inside the existing learner `try/except`, so a memory failure can never fail or repeat a sell. Cold-start loadout includes outcome atoms whose `object` starts with `main_`, never `shadow:`. `prune_and_compact` keeps the newest 500 outcome atoms per `object`, replaces older ones by one summary atom per `object` per calendar month (`predicate="outcome_summary"`, content: count, win rate, avg R, exit reason counts), and returns an extra key `"outcomes_compacted"`.

- [ ] **Step 1: Failing tests**
  - paper sell (helpers from `tests/test_paper_trading.py`) → one `main_paper` outcome with the trade's ticker and pnl.
  - `record_outcome` patched to raise → sell result still `success`, position closed, cash unchanged vs. the non-raising run.
  - live sell with the mocked broker from `tests/test_live_trading.py` → one `main_live` outcome.
  - loadout text built after one `shadow` and one `main_paper` outcome contains the main ticker and not the shadow ticker.
  - 505 outcomes for one strategy across two months → after `prune_and_compact`, 500 remain and at least one `outcome_summary` atom exists.
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement. No change to order, cash, or position logic.
- [ ] **Step 4:** Run the money-path set from the `live-money-safety` skill plus `tests/test_agent_memory.py tests/test_outcome_memory.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `Record main trade outcomes and keep shadow outcomes out of committee prompts`.

**Phase 2 gate:** full suite, push.

---

# Phase 3: shadow learner

### Task 9: `ShadowLearner`

**Model:** opus (high difficulty: LLM guard logic)

**Files:**
- Create: `src/trading/shadow_learner.py`
- Test: `tests/test_shadow_learner.py`

**Interfaces:**
- Consumes: `clamp_step`, `SPECS` (Task 1); `apply_param_change`, `get_strategies`, `learning_strengths` (Task 3); `memory_store.search_outcomes` (Task 6); `LLMClient.generate_json(system_prompt, user_prompt) -> Optional[dict]` from `src/agents/base.py`; `memory_store.add_l1_atom`.
- Produces:
  - `class ShadowLearner` with `llm: LLMClient` attribute (constructed in `__init__`, replaceable in tests).
  - `async learn(key: str, manual: bool = False) -> dict`: returns `{"success": bool, "skipped": bool, "message": str, "changes": dict}`.
  - `async learn_due() -> list[dict]`: runs `learn(key)` for every strategy with strength > 0.
  - `shadow_learner = ShadowLearner()` module singleton.
  - `compute_stats(positions: list[ShadowPosition]) -> dict`: `trades, win_rate_pct, avg_win_pct, avg_loss_pct, avg_r, expectancy_pct, exit_reasons: dict[str, int], avg_hold_days, fees_eur`. Pure, no DB.

Rules (all from the spec, "Phase 3"):
- Gate: strength 0 → skipped. Not manual: `closed_total - closed_at_last_learn < SHADOW_LEARN_MIN_TRADES` → skipped. Manual: `closed_total < 5` → skipped.
- One run per key at a time: per-key `asyncio.Lock`; a second concurrent call returns skipped.
- Prompt carries: current params, each learnable spec's range, `compute_stats` for "since last learn" and "all", up to 40 own outcomes and 20 `source="main"` outcomes. Asks for JSON only: `{"changes": {param: value}, "reason": str}`, at most 3 params.
- Guard: reply not a dict, or `changes` not a dict → no change. Take at most the first 3 entries. Drop names not in `SPECS`. Each value through `clamp_step(name, old, value, strength)`; drop when equal to old.
- Something left → `apply_param_change(key, kept, source="learner", reason=str(reason)[:500], stats=stats)`, set `closed_at_last_learn = closed_total`, add a memory atom (`agent_id="outcomes"`, `predicate="param_change"`, `object=f"shadow:{key}"`).
- Valid reply with nothing left after the guard → `success=True`, empty `changes`, counter **advanced** (the LLM answered; do not re-ask on every exit).
- LLM returned `None`, raised, or reply malformed → `success=False`, no history row, counter **not** advanced.

- [ ] **Step 1: Failing tests** (fixture: custom strategy `learn_me` with strength 2, N closed `ShadowPosition` rows inserted directly; `shadow_learner.llm.generate_json` replaced by an `AsyncMock`)

```python
async def test_good_proposal_is_clamped_and_saved():      # reply {"changes": {"stop_scale": 2.0}, "reason": "stops too tight"}
    # medium = 10% of 1.5 = 0.15 → stop_scale 1.0 → 1.15; history row source "learner"; reason stored; counter advanced
async def test_non_learnable_and_unknown_params_dropped(): # {"ignore_risk_veto": True, "accept_hold": True, "entry_mode": "market", "bogus": 1}
    # success True, changes == {}, no ShadowParamChange row
async def test_at_most_three_params():                     # 5 valid params proposed → 3 changed
async def test_below_trade_gate_skips_without_llm_call():  # 9 closed trades → skipped, generate_json not awaited
async def test_manual_needs_five_trades():                 # 4 → skipped; 5 → LLM called
async def test_strength_zero_skips():
async def test_llm_none_or_exception_changes_nothing_and_retries_later():
    # generate_json returns None, then raises: success False, no row, closed_at_last_learn unchanged
# Review Focus 2
@pytest.mark.parametrize("reply", [[], "text", {"changes": []}, {"changes": {"stop_scale": "wide"}},
                                   {"changes": {"stop_scale": float("nan")}}, {"changes": {"stop_scale": None}}, {}])
async def test_hostile_replies_change_nothing(reply):
    # no exception, params unchanged
def test_compute_stats():                                  # 3 wins (+2,+4,+6), 1 loss (-3): trades 4, win_rate_pct 75.0, avg_win_pct 4.0, avg_loss_pct -3.0
async def test_concurrent_learn_runs_once():               # two gathered learn() calls → generate_json awaited once
```

- [ ] **Step 2:** Run `python -m pytest tests/test_shadow_learner.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit: `Add memory-based shadow learner with bounded LLM proposals`.

### Task 10: Wire learner (scheduler, API, UI button)

**Model:** sonnet (in `jobs.py` it adds one call beside the existing shadow calls; it does not touch the buy path)

**Files:**
- Modify: `src/scheduler/jobs.py` (after `shadow_portfolios.check_exits()` at ~572 and ~674), `src/web/routes.py`, `src/web/static/app.js`
- Test: `tests/test_shadow_api.py`, `tests/test_shadow_learner.py`

**Interfaces:**
- Consumes: Task 9 `shadow_learner`.
- Produces: `POST /api/shadow/{key}/learn` → `await shadow_learner.learn(key, manual=True)`; 404 for an unknown key. Scheduler: after `check_exits()` returns a non-empty list, `asyncio.create_task(shadow_learner.learn_due())` wrapped so an exception is logged, never raised into the monitor job. UI: "Learn now" button on cards with strength > 0, `data-action="learn-shadow"`; shows the returned `message`, then reloads the card.

- [ ] **Step 1: Failing tests:** API returns the learner's dict (learner mocked); unknown key → 404; `learn_due` raising does not break the position-monitor job (call the job method with `shadow_learner.learn_due` patched to raise).
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest tests/test_shadow_api.py tests/test_shadow_learner.py tests/test_committee_jobs.py -q`. Playwright check: button shows the skip message on a fresh shadow.
- [ ] **Step 5:** Commit: `Run shadow learning after exits and on demand`.

**Phase 3 gate:** full suite, push.

---

# Phase 4: promote + main parity (every task: opus, `live-money-safety`)

Before Task 11 the implementer reads: `get_active_broker` in `src/trading/broker.py`, `validate_buy_order` in `src/trading/risk.py:205`, `evaluate_trigger_verdict` in `src/trading/trigger_gate.py:88`, and the session path in `src/scheduler/jobs.py:380-520` and `700-760`.

### Task 11: Main rule set resolver

**Model:** opus

**Files:**
- Create: `src/trading/main_strategy.py`
- Modify: `src/db/models.py` (`Position.rules_version = Column(Integer, nullable=True)`), `src/db/session.py` (`("positions", "rules_version", "INTEGER")` in `_run_migrations`)
- Test: `tests/test_main_strategy.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class MainRules`: `trigger_min_score: int, pullback_scale: float, stop_scale: float, entry_mode: str, accept_hold: bool, ignore_risk_veto: bool, reward_risk: float | None, use_take_profit: bool, breakeven_pct: float, breakeven_r: float | None, trailing_stop_pct: float | None, max_positions: int, max_hold_days: int | None, risk_per_trade_pct: float, trailing_atr_mult: float, regime_filter: bool, version: int, promoted_from: str | None`. Method `as_strategy() -> ShadowStrategy` (key `"main"`, `score_offset=0`) so the shared `plan_entry` / `effective_breakeven_pct` work on main.
  - `async get_main_rules() -> MainRules`: one `SystemConfig` read. Keys and defaults as in the spec ("Main rule set"); `version` from `MAIN_RULES_VERSION` (default 0). A missing or unparsable row falls back to its default and logs a warning. `reward_risk` defaults to `None` (= committee's target, today's behaviour).
  - `is_default_shape(rules) -> bool`: True when every Phase 4 behaviour param is at its default.
  - `async write_main_rules(values: dict, promoted_from: str | None) -> int`: saves current values as JSON in `MAIN_RULES_PREVIOUS`, writes new rows, bumps `MAIN_RULES_VERSION`, returns the new version. Also sets `settings.MAX_POSITIONS`, `settings.RISK_PER_TRADE_PCT`, `settings.TRAILING_STOP_ATR_MULT`, `settings.REGIME_FILTER_ENABLED`, `settings.TRIGGER_MIN_SCORE` in memory, the way the settings route at `src/web/routes.py:572-604` does, so existing readers follow without edits.
  - `async revert_main_rules() -> bool`: restores `MAIN_RULES_PREVIOUS`, bumps the version, clears `MAIN_PROMOTED_FROM`; `False` when nothing is stored.
  - `effective_max_positions(rules, live: bool, env_cap: int) -> int`: `min(rules.max_positions, env_cap)` when live, else `rules.max_positions`. `env_cap` is the `MAX_POSITIONS` value from `.env` at process start, captured once in this module.

- [ ] **Step 1: Failing tests**

```python
async def test_defaults_reproduce_today():
    r = await get_main_rules()
    assert (r.pullback_scale, r.stop_scale, r.entry_mode) == (1.0, 1.0, "trigger")
    assert (r.accept_hold, r.ignore_risk_veto, r.use_take_profit) == (False, False, True)
    assert r.breakeven_r is None and r.trailing_stop_pct is None and r.max_hold_days is None and r.reward_risk is None
    assert r.trigger_min_score == settings.TRIGGER_MIN_SCORE and r.max_positions == settings.MAX_POSITIONS
    assert r.version == 0 and is_default_shape(r)

async def test_write_then_revert_roundtrip():
    v = await write_main_rules({"stop_scale": 1.25, "entry_mode": "market", "max_positions": 3}, promoted_from="aggressive")
    r = await get_main_rules()
    assert v == 1 and r.stop_scale == 1.25 and r.entry_mode == "market" and r.promoted_from == "aggressive"
    assert settings.MAX_POSITIONS == 3 and not is_default_shape(r)
    assert await revert_main_rules() is True
    r = await get_main_rules()
    assert r.stop_scale == 1.0 and r.entry_mode == "trigger" and r.version == 2 and r.promoted_from is None

async def test_garbage_row_falls_back():                 # MAIN_STOP_SCALE = "abc" → 1.0
def test_live_max_positions_never_exceeds_env_cap():     # effective_max_positions(rules(max_positions=9), True, 5) == 5; False → 9
async def test_rules_version_column_exists():            # Position(rules_version=1) persists
```

- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement. `write_main_rules` validates values through `strategy_params.validate` for the names that exist there.
- [ ] **Step 4:** Run with `tests/test_settings_persistence.py tests/test_portfolio_sync.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add main rule set resolver with versioning and revert`.

### Task 12: Shared level maths

**Model:** opus

**Files:**
- Create: `src/trading/levels.py`
- Modify: `src/trading/shadow.py` (re-export moved names; `_check_exits` uses the new helpers)
- Test: `tests/test_levels.py`

**Interfaces:**
- Produces in `levels.py`:
  - `plan_entry(strategy, trigger_type, trigger_price, stop_loss, take_profit, current_price) -> dict`: moved unchanged from `shadow.py`.
  - `effective_breakeven_pct(strategy, eff_pct, entry, initial_stop) -> float`: moved unchanged.
  - `pct_trail_stop(current_stop: float | None, highest: float, trailing_stop_pct: float | None) -> float | None`: the block at `shadow.py:626-629`.
  - `max_hold_exceeded(opened_at, now, max_hold_days: int | None) -> bool`.
- `shadow.py` keeps `from src.trading.levels import plan_entry, effective_breakeven_pct` so existing imports work. `levels.py` must not import `shadow.py` (take the strategy as a duck-typed object).

- [ ] **Step 1: Failing tests:** `pct_trail_stop(90.0, 120.0, 0.10) == 108.0`; `pct_trail_stop(110.0, 120.0, 0.10) == 110.0`; `pct_trail_stop(None, 100.0, 0.05) == 95.0`; `pct_trail_stop(90.0, 120.0, None) == 90.0`; `max_hold_exceeded` true at 21 days with limit 20, false at 19, false with `None`; `plan_entry` identity check against three presets copied from `tests/test_shadow.py:65-76`.
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Move and add.
- [ ] **Step 4:** Run `python -m pytest tests/test_levels.py tests/test_shadow.py -q`. Expected: PASS, shadow tests unchanged.
- [ ] **Step 5:** Commit: `Share entry and exit level maths between shadow and main`.

### Task 13: Main entry behaviour

**Model:** opus

**Files:**
- Modify: `src/scheduler/jobs.py` (session path ~394-510, opportunity scanner ~708-760), `src/trading/trigger_gate.py` (`evaluate_trigger_verdict` ~88-132, `get_trigger_min_score`), `src/web/routes.py:413` and `src/telegram/bot.py:475` (manual trigger level derivation)
- Test: `tests/test_main_parity_entry.py`

**Interfaces:**
- Consumes: `get_main_rules`, `MainRules.as_strategy`, `effective_max_positions` (Task 11); `plan_entry` (Task 12).
- Produces:
  - `evaluate_trigger_verdict(report, trigger_min_score, force=False, rules: MainRules | None = None)`: `rules=None` → exactly today's result. With rules: HOLD is acceptable when `rules.accept_hold`; an unapproved risk verdict is acceptable when `rules.ignore_risk_veto` **and** the report carries a stop level > 0 below the entry. `_hard_block` and every other check unchanged.
  - Levels: wherever main calls `derive_trigger_levels(report, price)` to arm a trigger, pass the result through `plan_entry(rules.as_strategy(), ...)` when `not is_default_shape(rules)`; skip that call entirely when default (byte-identical levels to today).
  - `entry_mode == "market"` (or `plan_entry` returned `MARKET`): the session path calls the active broker's `execute_buy` through the same code the trigger monitor uses when a trigger fires, with the planned stop and target. Never a second, parallel sizing path.
  - New positions get `rules_version = rules.version` (set in Task 14's broker edit; this task passes the value through).

Interlocks that must still decide every buy, whatever the rules: `KILL_SWITCH_ACTIVE`, `MAX_DAILY_LOSS_EUR`, full `validate_buy_order`, whole-share/affordability checks, `evaluate_trade_economic_viability`, order idempotency, regime filter when `rules.regime_filter`.

- [ ] **Step 1: Failing tests** (paper broker, mocked committee report as in `tests/test_committee_jobs.py`)

```python
async def test_default_rules_arm_identical_trigger():          # same OrderTrigger fields as before this task
async def test_pullback_and_stop_scale_shift_levels():         # pullback_scale 0.5, stop_scale 1.25, price 100, entry 95, sl 90 → target 97.5, stop 91.25
async def test_reward_risk_and_no_take_profit():               # reward_risk 3 → tp = entry + 3×stop distance; use_take_profit False → take_profit None
async def test_market_entry_buys_now_through_execute_buy():    # one Position, no PENDING OrderTrigger
async def test_accept_hold_arms_on_hold_above_threshold():     # and does not arm when accept_hold False
async def test_ignore_risk_veto_arms_when_stop_present():
# Review Focus 5
async def test_ignore_risk_veto_without_stop_is_rejected():
async def test_ignore_risk_veto_with_kill_switch_on_places_nothing():
async def test_ignore_risk_veto_still_fails_validate_buy_order():   # position over MAX_POSITION_PCT or no cash → no order
async def test_market_entry_cannot_afford_one_share_places_nothing():
async def test_market_entry_fee_unviable_places_nothing():
```

- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the money-path set from `live-money-safety` + `tests/test_committee_jobs.py tests/test_telegram_trigger_gate.py tests/test_main_parity_entry.py`. Expected: PASS with no edits to existing tests.
- [ ] **Step 5:** Commit: `Let the main bot apply promoted entry rules behind unchanged interlocks`. Commit body lists the interlocks touched: none weakened; `evaluate_trigger_verdict` gained an opt-in `rules` argument.

### Task 14: Main exit behaviour (paper and live together)

**Model:** opus

**Files:**
- Modify: `src/trading/paper.py` (position monitor ~500-560; buy path sets `rules_version`), `src/trading/live.py` (monitor ~1990-2030 and the T212 resting-stop update on that path; buy path sets `rules_version`)
- Test: `tests/test_main_parity_exit.py`

**Interfaces:**
- Consumes: `get_main_rules` (Task 11); `effective_breakeven_pct`, `pct_trail_stop`, `max_hold_exceeded` (Task 12).
- Produces: in both monitors, for a position with `rules_version == rules.version` and `rules.version > 0`: breakeven trigger via `effective_breakeven_pct(rules.as_strategy(), rules.breakeven_pct, entry, initial_stop)`; after `compute_trailing_stop`, apply `pct_trail_stop`; close with reason `f"Time exit after {n} days"` when `max_hold_exceeded`. Positions with another or `NULL` `rules_version` follow exactly today's logic. A stop only ever moves up. In live, a raised stop reaches the broker through the same call today's ratchet uses; if that call fails, the DB stop is not advanced (same rule as today).

`breakeven_r` needs the stop at entry. Use the existing initial-stop field on `Position` if there is one (`grep -n "initial_stop" src/db/models.py`); if not, add `positions.initial_stop_native FLOAT` + migration entry in this task and set it on buy.

- [ ] **Step 1: Failing tests,** each written once and parametrized over paper and live (mocked broker from `tests/test_live_trading.py`):
  - old-version position: stop path identical to before (snapshot test against current behaviour).
  - `trailing_stop_pct=0.10`, high 120 → stop 108.
  - `breakeven_r=1.0`, entry 100, initial stop 92: at 107 the stop has not moved; at 108 it is 101.
  - `max_hold_days=20`, position 21 days old, market open → sold, reason starts `"Time exit"`.
  - stop never decreases when the high falls back.
  - live: broker stop-update raises → DB stop unchanged, no sell.
  - kill switch on → time exit still sells (sells are never blocked).
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement in both brokers.
- [ ] **Step 4:** Run the money-path set + `tests/test_t212_resting_orders.py tests/test_trade_logic_fixes.py tests/test_main_parity_exit.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `Apply promoted exit rules to positions opened under them, in paper and live`.

### Task 15: Pause the main learner while promoted

**Model:** opus

**Files:**
- Modify: `src/trading/learner.py` (`calibrate_internal_parameters` ~206, `decay_idle_thresholds` ~516)
- Test: `tests/test_learner.py`

**Interfaces:**
- Produces: when `SystemConfig["MAIN_LEARNER_PAUSED"] == "true"`: `calibrate_internal_parameters(automatic=True)` and `decay_idle_thresholds()` return `{"success": False, "skipped": True, "message": "Auto-calibration is paused while promoted shadow rules are active.", "adjustments": []}` before any LLM call or write. Manual calibration (`automatic=False`) still runs. Post-mortems still run.

- [ ] **Step 1: Failing tests (Review Focus 4):** paused + enough closed trades → skipped, `TRIGGER_MIN_SCORE` / `BREAKEVEN_TRIGGER_PCT` / `TAKE_PROFIT_RR` rows unchanged, LLM mock not awaited; paused → `decay_idle_thresholds` changes nothing; paused + manual → runs.
- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest tests/test_learner.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Pause automatic calibration while promoted rules are active`.

### Task 16: Promote API

**Model:** opus

**Files:**
- Create: `src/trading/promote.py`
- Modify: `src/web/routes.py`
- Test: `tests/test_promote.py`

**Interfaces:**
- Consumes: `get_main_rules`, `write_main_rules`, `revert_main_rules` (Task 11); `get_strategies` (Task 3); `record` helpers of Task 3/6 for history and memory.
- Produces:
  - `resolve_for_main(strategy: ShadowStrategy, current: MainRules) -> dict`: absolute values for all 16 params. `trigger_min_score = strategy.threshold(current.trigger_min_score)`; a `None` shadow value keeps the current main value. Uses **current** main values, never the shadow's `frozen_params`.
  - `async preview(key: str) -> dict`: `{"key", "label", "live": bool, "risky_flags": [names newly enabled among ignore_risk_veto, accept_hold, entry_mode], "needs_confirm_text": bool, "params": [{"name", "label", "current", "after", "changed"}]}`.
  - `async promote(key: str, confirm: bool, confirm_text: str = "") -> dict`: `ValueError` when `confirm` is not True, or when live and risky flags are non-empty and `confirm_text != "PROMOTE LIVE"`. Then `write_main_rules(..., promoted_from=key)`, set `MAIN_LEARNER_PAUSED="true"`, add a `ShadowParamChange` (`strategy=key`, `source="promote"`), add a memory atom, send a Telegram notice in a `try/except`.
  - Routes: `GET /api/shadow/{key}/promote/preview`; `POST /api/shadow/{key}/promote`; `POST /api/main/rules/revert` (reverts and clears the pause); `POST /api/main/learner/resume` (clears the pause only); `GET /api/main/rules` → `{rules, promoted_from, learner_paused, can_revert}`. `ValueError` → 400.
  - `live` = `settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED`.

- [ ] **Step 1: Failing tests**

```python
async def test_preview_resolves_offset_against_current_main():   # main 82, shadow score_76 (offset -6) → after 76, changed True
async def test_preview_none_keeps_main_value():                  # baseline → no changed rows
async def test_promote_requires_confirm():                       # confirm False → 400, rules unchanged
async def test_promote_writes_rules_pauses_learner_logs_history():
async def test_revert_restores_and_resumes():
async def test_resume_keeps_promoted_rules():
async def test_live_risky_promote_needs_typed_text():            # settings forced LIVE: wrong text → 400; "PROMOTE LIVE" → 200
async def test_live_non_risky_promote_needs_no_text():           # shadow with only stop_scale changed
async def test_paper_risky_promote_needs_no_text():
async def test_later_shadow_edits_do_not_reach_main():           # promote, apply_param_change on the shadow, main unchanged
async def test_telegram_failure_does_not_fail_promote():
```

- [ ] **Step 2:** Run, expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest tests/test_promote.py tests/test_web_security.py tests/test_main_strategy.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `Add manual promote of a shadow's rules to the main bot`.

### Task 17: Promote UI

**Model:** opus (drives a real-money action)

**Files:**
- Modify: `src/web/static/app.js`, the dashboard template (banner slot)
- Test: Playwright check below

**Interfaces:**
- Consumes: Task 16 routes.
- Produces: `data-action` values `promote-shadow`, `confirm-promote`, `revert-main-rules`, `resume-main-learner`.

- [ ] **Step 1:** "Use for main bot" button per shadow card → dialog from `/promote/preview`: table `Parameter | Main now | After`, changed rows highlighted, risky flags listed in the loss colour with one plain sentence each (e.g. "Ignore risk veto: the bot will buy even when the Risk Officer rejected the trade.").
- [ ] **Step 2:** When `needs_confirm_text`: text input, Confirm disabled until the value is exactly `PROMOTE LIVE`. Otherwise a plain Confirm button. Show the 400 `detail` inline.
- [ ] **Step 3:** Banner on the dashboard while `promoted_from` is set: "Main bot uses rules from <label>. Auto-calibration paused." with Revert and Resume calibration. Revert asks for confirmation.
- [ ] **Step 4: Verify** in PAPER mode with Playwright at 390px and 1280px: promote `aggressive`, banner appears, `GET /api/main/rules` shows `stop_scale 1.25`; revert, banner gone. Screenshots; no console errors. Run the three `tests/test_web_*contract*` modules and `node --test tests/js/`.
- [ ] **Step 5:** Commit: `Add promote dialog and promoted-rules banner`.

### Task 18: Docs and final verification

**Model:** haiku for the docs edit; the verification run and the final review on opus.

**Files:**
- Modify: `CLAUDE.md` (Architecture: one paragraph on `strategy_params.py`, `main_strategy.py`, `shadow_learner.py`, outcome memory; fix the sentence "Shadows are simulation-only parameter variants ... never orders" to say promote copies rules to main by hand), `AGENTS.md` (symbol map entries for the new files)

- [ ] **Step 1:** Edit both files. Keep them normal prose.
- [ ] **Step 2:** Run the money-path set from `live-money-safety`, then `python -m pytest tests/ -q`. Expected: PASS.
- [ ] **Step 3:** Paper run: `python run.py`, promote `aggressive`, trigger one evaluation from the UI, confirm in the log that the armed trigger uses the scaled levels and that the kill switch still blocks a buy when turned on.
- [ ] **Step 4:** Fresh `opus` subagent runs `/ponytail-review` on the whole branch diff against `main`. Fix findings.
- [ ] **Step 5:** Commit: `Document shadow learning and promoted rules`. Push.

---

## Task / model summary

| # | Task | Model | Money path |
|---|---|---|---|
| 1 | Parameter schema | sonnet | no |
| 2 | Tables and settings | haiku | no |
| 3 | Strategy store | sonnet | no |
| 4 | Shadow API | sonnet | no |
| 5 | Shadow tab UI | sonnet | no |
| 6 | Outcome atoms | sonnet | no |
| 7 | Shadow outcomes | sonnet | no |
| 8 | Main outcomes, cold start, prune | opus | yes |
| 9 | ShadowLearner | opus | no (high difficulty) |
| 10 | Wire learner | sonnet | no |
| 11 | Main rule resolver | opus | yes |
| 12 | Shared level maths | opus | yes |
| 13 | Main entry behaviour | opus | yes |
| 14 | Main exit behaviour | opus | yes |
| 15 | Pause main learner | opus | yes |
| 16 | Promote API | opus | yes |
| 17 | Promote UI | opus | yes |
| 18 | Docs + final verification | haiku / opus | review |

Reviewer after every task: fresh subagent, `/ponytail-review`, `opus` on money-path rows, `sonnet` on the rest.
