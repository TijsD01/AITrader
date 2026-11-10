# Shadow learning, outcome memory, promote to main: design

Date: 2026-11-10. Status: awaiting review. Style: ultracave (fragments, each fact once).

## Goal

- Shadow tab: create, copy, edit, delete shadows. Params adjustable in UI.
- Per-shadow learning. Strength slider: off / low / medium / high.
- Learning reads memory. Proposals come from `agy`.
- Main bot can adopt a shadow's rules. Manual promote. Full param parity.

## Decisions (owner, 2026-11-10)

| Topic | Choice |
|---|---|
| Learn scope | Any shadow the owner toggles |
| Shadow management | Create, copy, delete in UI |
| Slider meaning | Learning strength = max step per param per learning run |
| Memory | Outcome memory on current store. No rewrite |
| Promote | Manual, diff + confirm, reversible |
| Promote scope | Full parity: all 16 params work on main |

## Now

- `ShadowStrategy` (16 params) in `src/trading/shadow.py`. Presets in code. Custom only via `SHADOW_CUSTOM_STRATEGIES` env JSON.
- `TradeLearner.calibrate_internal_parameters`: main only, last 20 closed main trades, no memory read.
- `AgentMemoryStore`: feeds committee cold-start. Learner neither reads nor writes.
- Main supports 7 of 16: score threshold, breakeven %, max positions, risk per trade, ATR trail, regime filter, take-profit R:R.
- Shadow-only 9: `pullback_scale`, `stop_scale`, `entry_mode`, `accept_hold`, `ignore_risk_veto`, `trailing_stop_pct`, `max_hold_days`, `breakeven_r`, `use_take_profit`.

## Non-goals

- Auto-promote. Never.
- Memory engine rewrite, embeddings, vector search.
- Backtest on past data. Shadows stay forward-only.
- Learning on `accept_hold`, `ignore_risk_veto`.

## Phase 1: shadows in DB, editable

### Schema: `src/trading/strategy_params.py` (new)

One table of param specs. Single source for UI, API validation, learner clamp, promote.

```python
@dataclass(frozen=True)
class ParamSpec:
    name: str            # ShadowStrategy field name
    label: str
    kind: str            # "int" | "float" | "bool" | "choice"
    min: float | None
    max: float | None
    step: float | None
    nullable: bool       # None = inherit main value frozen at shadow start
    learnable: bool      # False for accept_hold, ignore_risk_veto, entry_mode
    choices: tuple[str, ...] = ()
```

Functions:
- `validate(params: dict) -> dict`: unknown key, wrong type, out of range → `ValueError` naming the field.
- `clamp_step(name, old, proposed, strength) -> value`: bounds + step cap.
- `to_json_schema() -> list[dict]`: served to frontend.

Bounds:

| Param | Kind | Min | Max | Step |
|---|---|---|---|---|
| `score_offset` | int | -20 | 10 | 1 |
| `pullback_scale` | float | 0.0 | 1.5 | 0.05 |
| `stop_scale` | float | 0.5 | 2.0 | 0.05 |
| `reward_risk` | float, nullable | 1.0 | 5.0 | 0.1 |
| `breakeven_pct` | float, nullable | 0.005 | 10.0 | 0.005 |
| `breakeven_r` | float, nullable | 0.5 | 3.0 | 0.1 |
| `trailing_stop_pct` | float, nullable | 0.02 | 0.20 | 0.01 |
| `max_positions` | int, nullable | 1 | 20 | 1 |
| `max_hold_days` | int, nullable | 1 | 120 | 1 |
| `risk_per_trade_pct` | float, nullable | 0.0 | 3.0 | 0.1 |
| `trailing_atr_mult` | float, nullable | 0.0 | 6.0 | 0.25 |
| `entry_mode` | choice | `trigger`, `market` | | |
| `accept_hold`, `ignore_risk_veto`, `use_take_profit`, `regime_filter` | bool | | | |

`breakeven_pct` max 10.0 keeps `no_breakeven` preset valid. Learner range for it: 0.005 to 0.10.

### Table `shadow_strategies` (new, `src/db/models.py`)

| Column | Type | Note |
|---|---|---|
| `key` | String(40) PK | Same key as `shadow_accounts.strategy` |
| `label` | String(60) | |
| `description` | Text | |
| `params` | JSON | Validated by schema |
| `learning_strength` | Integer, default 0 | 0 off, 1 low, 2 medium, 3 high |
| `enabled` | Boolean, default True | |
| `copied_from` | String(40), nullable | |
| `is_preset` | Boolean, default False | |
| `closed_at_last_learn` | Integer, default 0 | Closed-trade count at last learning run |
| `created_at`, `updated_at` | DateTime | |

New table → `Base.metadata.create_all`. No `_run_migrations` entry.

### Seeding

- First start with empty table: insert every key in `SHADOW_STRATEGIES` (presets + `SHADOW_CUSTOM_STRATEGIES`). `is_preset=True` for preset keys.
- After seed, table is truth. Env vars ignored, log one info line.
- `get_strategies()` becomes async DB read, returns `ShadowStrategy` objects built from rows. `SHADOW_ENABLED=False` still returns `[]`.
- `PRESETS` dict stays in code: seed source + "restore preset" target.

### Semantics

- Edit params of running shadow: open positions keep stored stops/targets. New rules apply to new triggers and entries. `frozen_params` untouched.
- Copy: new row, same params, `copied_from` set, fresh `ShadowAccount`, strength 0. No positions copied.
- Delete: removes strategy row, account, positions, triggers, snapshots of that key. Confirm dialog. Presets deletable; restorable from `PRESETS`.
- Key: slug of label, lowercase, `[a-z0-9_]`, max 40, unique, not `main`.
- Max 12 shadows. Each costs quotes per monitor tick.

### API (`src/web/routes.py`)

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/shadow/schema` | | param specs |
| POST | `/api/shadow` | `label`, `params`, optional `base` | create |
| POST | `/api/shadow/{key}/copy` | `label` | copy |
| PATCH | `/api/shadow/{key}` | any of `label`, `description`, `params`, `learning_strength`, `enabled` | edit |
| DELETE | `/api/shadow/{key}` | | delete |
| GET | `/api/shadow/{key}/history` | | param change rows |
| POST | `/api/shadow/{key}/history/{id}/undo` | | restore `old` values |

Existing `GET /api/shadow`, `POST /api/shadow/{key}/reset` unchanged. Comparison payload gains `learning_strength`, `copied_from`, `is_preset`. Validation fail → 422 with field name. Same auth as current shadow routes (`src/web/security.py`).

### Table `shadow_param_changes` (new)

`id`, `strategy`, `source` (`user` | `learner` | `promote` | `undo`), `changes` JSON (`{param: {old, new}}`), `reason` Text, `stats` JSON, `created_at`.

Every param write goes through one function `apply_param_change(key, new_params, source, reason)` in `shadow.py`. It validates, saves, writes history row.

### UI (`src/web/static/app.js`, shadow tab)

- Header: "New shadow" button.
- Per card: Edit, Copy, Delete, existing Reset. Learning strength slider (4 stops, labelled). Badge "Learning" when > 0.
- Edit sheet: built from `/api/shadow/schema`. Slider per numeric, toggle per bool, select per choice. Nullable param: "Use main bot value" toggle.
- History list: date, source, param old → new, reason, Undo.
- Note on learning cards: "Under 30 closed trades the result is noise. A learning shadow is no longer a clean comparison."
- Follow existing `data-action` dispatch and `ui-*` classes. Touch targets 44px.

## Phase 2: outcome memory

### Write

New `src/memory/outcomes.py`:

```python
async def record_outcome(*, source: str, strategy: str, ticker: str, pnl_pct: float,
                         r_multiple: float | None, hold_days: float, exit_reason: str,
                         score: int | None, entry_type: str, params: dict) -> None
```

- Writes one `L1Atom`: `type="episodic"`, `agent_id="outcomes"`, `subject=ticker`, `predicate="closed_trade"`, `object=f"{source}:{strategy}"`, `content` = one-line human summary + JSON tail.
- `source`: `main_live` | `main_paper` | `shadow`.
- Callers: `ShadowPortfolios._close_position` path (after commit), `PaperBroker` and `LiveBroker` sell path next to `conduct_trade_post_mortem`.
- Failure never breaks a close: `try/except`, `logger.warning`.
- Idempotent: `atom_id = f"outcome:{source}:{position_or_trade_id}"`. Existing id → skip.

### Read

`AgentMemoryStore.search_outcomes(strategy=None, source=None, ticker=None, exit_reason=None, since=None, limit=50)`. Structured filter on atom table. JSON tail parsed into dicts.

### Separation

- `ColdStartEngine` committee loadout: includes `main_*` outcomes only. Shadow outcomes excluded, so simulated fills do not colour live verdicts.
- Learner reads all three sources, labelled.

### Pruning

`prune_and_compact`: keep newest 500 outcome atoms per strategy. Older ones roll into one summary atom per strategy per month (count, win rate, avg R, exit reason counts).

## Phase 3: shadow learner

New `src/trading/shadow_learner.py`, class `ShadowLearner`.

### Trigger

- After shadow exits in `check_exits`: for each strategy with `learning_strength > 0` and `closed_trades - closed_at_last_learn >= SHADOW_LEARN_MIN_TRADES` (new setting, default 10).
- Manual: `POST /api/shadow/{key}/learn`. Ignores trade-count gate, still needs >= 5 closed trades.
- One run at a time per strategy (asyncio lock). Runs outside the monitor's DB session.

### Run

1. Stats for the strategy since last learn and overall: trades, win rate, avg win, avg loss, avg R, expectancy, exit reason counts, avg hold days, fees.
2. Recall: `search_outcomes(strategy=key, limit=40)` + `search_outcomes(source="main_*", limit=20)`.
3. Prompt to `LLMClient` (`agy` when `LLM_PROVIDER=antigravity`): current params, schema bounds, stats, outcomes. Ask JSON only: `{"changes": {param: value}, "reason": str}`. Max 3 params per run.
4. Guard. For each proposed param:
   - not in schema or `learnable=False` → drop.
   - `clamp_step`: inside bounds; move <= strength share of range (low 5%, medium 10%, high 20%); round to step.
   - no change after clamp → drop.
5. Any left → `apply_param_change(..., source="learner", reason=...)`. Set `closed_at_last_learn`.
6. Write memory atom `predicate="param_change"` with changes + reason.

### Failure

- `agy` missing, timeout, non-JSON, empty → no change, `logger.warning`, history row not written, counter not advanced (retry at next exit).
- No deterministic fallback tuner. Wrong guess worse than no change.

### Limits said in UI and docs

- Learner tunes on its own results: overfit risk. Strength cap + min trades reduce, do not remove.
- Committee verdicts differ by day: two shadows see same verdicts, so comparison between shadows stays fair; comparison across time does not.

### Tests

Mock `LLMClient`. Cases: good proposal, out-of-bounds, over step cap, non-learnable param, 4+ params, bad JSON, exception, below trade gate, strength 0.

## Phase 4: promote + main parity

Real-money path. `live-money-safety` skill applies to every task here.

### Main rule set: `src/trading/main_strategy.py` (new)

- `async get_main_rules() -> MainRules`: absolute values for all 16 params. Reads `SystemConfig` rows, falls back to `settings`. Defaults reproduce today's behaviour exactly.
- New `SystemConfig` keys: `MAIN_PULLBACK_SCALE` (1.0), `MAIN_STOP_SCALE` (1.0), `MAIN_ENTRY_MODE` (`trigger`), `MAIN_ACCEPT_HOLD` (`false`), `MAIN_IGNORE_RISK_VETO` (`false`), `MAIN_TRAILING_STOP_PCT` (empty), `MAIN_MAX_HOLD_DAYS` (empty), `MAIN_BREAKEVEN_R` (empty), `MAIN_USE_TAKE_PROFIT` (`true`), `MAIN_MAX_POSITIONS`, `MAIN_RISK_PER_TRADE_PCT`, `MAIN_TRAILING_ATR_MULT`, `MAIN_REGIME_FILTER`, `MAIN_PROMOTED_FROM`, `MAIN_RULES_PREVIOUS` (JSON).
- Existing keys reused: `TRIGGER_MIN_SCORE`, `BREAKEVEN_TRIGGER_PCT`, `TAKE_PROFIT_RR`.

### Shared level maths

Move from `shadow.py` to pure functions both sides call: `plan_entry`, `effective_breakeven_pct`, stop scaling, % trail, max-hold check. Shadow and main cannot drift.

### Behaviour by param

| Param | Where main changes |
|---|---|
| `pullback_scale`, `stop_scale`, `reward_risk`, `use_take_profit` | trigger arming: `jobs.py` session path, `create_price_trigger` in `paper.py` / `live.py` |
| `entry_mode=market` | session path buys through `execute_buy` instead of arming a trigger |
| `accept_hold` | session path: HOLD with score >= threshold treated as entry candidate |
| `ignore_risk_veto` | session path + `trigger_gate.evaluate_trigger_verdict`: Risk Officer rejection does not block |
| `breakeven_r`, `trailing_stop_pct`, `max_hold_days` | `position_monitor` in both brokers; T212 resting stop orders updated on same path as today's ratchet |
| `max_positions`, `risk_per_trade_pct`, `trailing_atr_mult`, `regime_filter` | read from `get_main_rules()` instead of `settings` |

### Interlocks: never overridable

Promote and `ignore_risk_veto` skip the committee's Risk Officer verdict only. These still run on every main buy, any rule set:

- `KILL_SWITCH_ACTIVE`, `MAX_DAILY_LOSS_EUR`
- `validate_buy_order` in full: `MAX_POSITION_PCT`, cash, capital usage limit, NaN / non-positive rejection
- `WHOLE_SHARES_ONLY`, min one share, affordability
- `evaluate_trade_economic_viability` (fees)
- Order idempotency checks
- `MAX_POSITIONS` hard ceiling from `settings` when lower than promoted value in LIVE mode

Stop loss always present: `ignore_risk_veto` buy without a valid stop level → rejected.

### Promote flow

- `GET /api/shadow/{key}/promote/preview` → per param: main now, after promote, changed flag. Shadow relative values resolved to absolutes with **current** main values (not shadow's `frozen_params`), e.g. `score_offset -6` on main 82 → 76.
- `POST /api/shadow/{key}/promote` body `{"confirm": true, "confirm_text": "..."}`:
  - saves current rules to `MAIN_RULES_PREVIOUS`
  - writes new rules, `MAIN_PROMOTED_FROM=key`
  - sets `SCORE_AUTO_TUNE`-equivalent pause: new key `MAIN_LEARNER_PAUSED=true`. `calibrate_internal_parameters(automatic=True)` and `decay_idle_thresholds` skip while set
  - history row `source="promote"`, memory atom, Telegram notice
- `POST /api/main/rules/revert` → restores `MAIN_RULES_PREVIOUS`, clears pause.
- LIVE mode (`TRADING_MODE=LIVE` and `LIVE_TRADING_ENABLED`) and new rules enable `ignore_risk_veto`, `accept_hold`, or `entry_mode=market`: `confirm_text` must equal `PROMOTE LIVE`. Else 400.
- Open positions and pending triggers keep stored levels. New rules: new entries, plus exit rules (`breakeven_r`, trail, max hold) on positions opened after promote. Needs `opened_under_rules` marker: new nullable column `positions.rules_version` → entry in `_run_migrations`.
- One-way copy. Later edits or learning on the shadow do not reach main.

### UI

- "Use for main bot" button per shadow card. Dialog: diff table, list of risky flags in red, typed confirm in LIVE.
- Banner on dashboard while promoted: "Main bot uses rules from <label>. Auto-calibration paused." Buttons: Revert, Resume calibration.

### Tests

- Defaults: full existing suite passes unchanged (parity = no behaviour change until promote).
- Each new param: paper and live, both. Include kill switch on, cannot afford one share, fee-unviable trade, missing stop.
- `ignore_risk_veto` + `validate_buy_order` fail → no order.
- Promote preview maths, revert, learner pause, LIVE typed confirm.
- Money-path test set from `live-money-safety` skill, then full suite.
- Paper run before any LIVE promote.

## Settings added

| Name | Default | Meaning |
|---|---|---|
| `SHADOW_LEARN_MIN_TRADES` | 10 | Closed trades between learning runs per shadow |
| `SHADOW_MAX_STRATEGIES` | 12 | Cap on shadow count |

## Files

| File | Change |
|---|---|
| `src/trading/strategy_params.py` | new: schema, validate, clamp |
| `src/trading/shadow.py` | DB-backed strategies, CRUD, `apply_param_change`, outcome hook |
| `src/trading/shadow_learner.py` | new |
| `src/trading/main_strategy.py` | new |
| `src/memory/outcomes.py` | new |
| `src/memory/store.py`, `cold_start.py` | `search_outcomes`, prune rule, source filter |
| `src/db/models.py`, `session.py` | 2 tables, 1 column + migration |
| `src/trading/paper.py`, `live.py`, `risk.py`, `trigger_gate.py`, `learner.py` | phase 4 parity, learner pause |
| `src/scheduler/jobs.py` | session path reads main rules; learner hook |
| `src/web/routes.py`, `static/app.js` | API + shadow tab UI |
| `config/settings.py` | 2 settings |
| `tests/` | per phase |

## Order

1 → 2 → 3 → 4. Each phase ships alone. 3 needs 1 + 2. 4 needs 1.

## Risks

- Phase 4 widens what main may do with real money. Mitigation: defaults unchanged, interlocks fixed, typed confirm, revert, paper first.
- Learner overfit on small samples. Mitigation: trade gate, step cap, history + undo, UI note.
- `get_strategies()` sync → async touches every caller. Mitigation: phase 1 first task, full suite.
- `app.js` size grows. Mitigation: shadow editor in own function block; no framework change.
