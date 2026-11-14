# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Project instructions shared with the other coding agents live in AGENTS.md (subsystem map, test commands, response style):

@AGENTS.md

Claude-specific notes on AGENTS.md:
- Its `file:///home/tdapper/TradingBotNew/...` links point at the owner's machine. Resolve them relative to this repo root.
- "`view_file` with `StartLine`/`EndLine`" means Read with `offset`/`limit`.
- The `.venv/bin/python` commands assume a local venv. Without one, use `python -m pytest ...` after `pip install -r requirements.txt`.

## Commands

```bash
pip install -r requirements.txt            # Python 3.13; no lint/format tooling is configured
python run.py --reload                     # FastAPI app + scheduler + Telegram polling (HOST/PORT from settings)
scripts/start-memory-server.sh             # standalone agent-memory hub on 127.0.0.1:8420 (src.memory.server)

python -m pytest tests/ -q                 # full suite
python -m pytest tests/test_triggers.py -q # one module
python -m pytest tests/test_triggers.py::test_name -q   # one test
python -m pytest tests/ -q -k trading212   # by keyword
```

`pytest.ini` sets `pythonpath = .` and `asyncio_mode = auto`, so async tests need no marker.

## Test isolation (tests/conftest.py)

- conftest resets every `settings` field to its code default before importing app code. The real `.env` never affects tests. Override settings per test instead of relying on env vars.
- Tests force `TRADING_MODE="PAPER"`, `LIVE_TRADING_ENABLED=False`, `REGIME_FILTER_ENABLED=False`.
- The SQLAlchemy engine and `memory_store.db_path` are rebound to `data/test_trading_bot.db` and `data/test_memory.db`. Both are rebuilt per session.
- Code that captures `engine`/`AsyncSessionLocal` at import time bypasses this rebinding. Import `src.db.session` as a module and read its attributes at call time.

## Architecture

One process. `run.py` starts uvicorn on `src.web.app:app`. Its `lifespan` starts `TradingScheduler` (APScheduler), replays missed sessions via `check_and_run_startup_sessions()`, and starts Telegram long-polling. Web UI, REST API, memory routes and memory proxy share that one app.

**Decision pipeline** (`TradingScheduler.run_market_session` in `src/scheduler/jobs.py`):
1. `MarketScanner` loads watchlist + universe (`config/default_universe.py`) and scans with yfinance data (`src/data/market.py`).
2. `InvestmentCommittee.evaluate_ticker` pulls a cold-start memory loadout, runs technical, fundamental and sentiment agents in parallel (`asyncio.gather`), then `RiskManagerAgent` validates levels. The risk step reuses the debate report; it makes no extra LLM call.
3. A report scoring >= `TRIGGER_MIN_SCORE` arms a price trigger (pullback/breakout) instead of buying at once. Whole-share fit and affordability checks run first.
4. Interval jobs: `price_trigger_monitor` (3 min) fires triggers through `trigger_gate.evaluate_trigger_verdict`. `position_monitor` (10 min) checks stops/targets. `market_opportunity_scanner` (30 min) runs during market hours. Session crons run at EU and US open/close.
5. Every verdict also feeds `ShadowPortfolios.follow_report`. Shadows are simulation-only parameter variants for comparison, never orders. The owner can copy a shadow's rules onto the main bot by hand (see **Shadow learning and promote**).

**Broker selection** (`src/trading/broker.py`): code calls `active_broker`, a proxy that resolves on every attribute access. `LiveBroker` is used only when `TRADING_MODE == "LIVE"` **and** `LIVE_TRADING_ENABLED`. Otherwise `PaperBroker`. Both implement `BaseBroker` (`src/trading/base.py`). `LiveBroker` branches on `LIVE_BROKER` (`trading212` via `src/trading/trading212.py`, or `alpaca`; any other value fails at startup). The kill switch blocks new buys only; it does not swap brokers, so open live positions stay monitored and closable.

**Sizing and money rules** live in `risk.py` (allocation, `validate_buy_order`, caps) and `fees.py` (per-broker fees, round-trip viability). Base currency is EUR; conversion is in `src/data/currency.py`. `WHOLE_SHARES_ONLY` defaults to `True`, but a validator in `config/settings.py` flips it to `False` for `trading212` (fractional shares) unless set explicitly.

**LLM access**: `LLMClient` in `src/agents/base.py`. `LLM_PROVIDER` is `antigravity` (default; shells out to the `agy` CLI), `gemini` or `openai`. Agents merge LLM output over deterministic reports (`merge_llm_report`), so a failed LLM call degrades rather than breaks evaluation. Tests must not depend on a live LLM.

**Agent memory** (`src/memory/`, SQLite `data/memory.db`): `AgentMemoryStore` holds memory atoms and a code graph. `ColdStartEngine` builds the prompt context the committee injects. It runs in-app via `memory_router`, or standalone via `scripts/start-memory-server.sh`. Every closed trade, shadow and main, is also written as an outcome atom by `src/memory/outcomes.py`; main-bot writes run off the sell's lock, and shadow outcomes stay out of cold-start loadouts.

**Persistence**: async SQLAlchemy + aiosqlite (`src/db/models.py`, `data/trading_bot.db`). Tables come from `Base.metadata.create_all`. There is no Alembic: a new column on an existing table also needs an entry in the `_run_migrations` list in `src/db/session.py` (idempotent `ALTER TABLE ... ADD COLUMN`).

**Learning loop**: `TradeLearner` (`src/trading/learner.py`) scores closed trades and calibrates main-bot settings. Its post-mortem only uses the position and analysis report that existed at entry. Calibration judges the window by expectancy and profit factor (net of fees) as well as win rate; an LLM-proposed value moves at most 10% of its allowed range per calibration (agent weights are capped before they are normalised to sum to 1, so a weight can end slightly past that), and the rule-based fallback never lowers score thresholds on a losing streak and only loosens them on positive expectancy, one point at a time and never below the code default. `review_tracker.py` tracks advice outcomes.

**Shadow learning and promote**: shadows live in the `shadow_strategies` table and are created, copied, edited and deleted from the Shadow tab. `SHADOW_STRATEGIES` and `SHADOW_CUSTOM_STRATEGIES` only seed that table on first start; the `SHADOW_SEEDED` SystemConfig row marks it done. After that the table is the source of truth, and `get_strategies()` reads an in-memory cache that every store write reloads. `src/trading/strategy_params.py` is the single schema of shadow parameter bounds, used by the UI sliders, API validation and the learner's clamps. `ShadowLearner` (`src/trading/shadow_learner.py`) tunes a shadow whose learning strength is above zero: it reads outcomes from memory, asks `LLMClient.generate_json` for a proposal, and clamps it to at most 3 parameters per run, with a step cap of 5%, 10% or 20% of a parameter's range for low, medium or high strength; it never changes `accept_hold`, `ignore_risk_veto` or `entry_mode`, and a failed LLM call changes nothing. It may not move a parameter back toward the value before its own last change of it until at least `SHADOW_LEARN_MIN_TRADES` positions opened after that change have closed with a lower expectancy than the trades that motivated it; a later human edit lifts that lock. Main rules resolve from `SystemConfig` in `src/trading/main_strategy.py`, bounds-checked on read with a fallback to the default, and `src/trading/levels.py` holds the entry and exit maths shared by shadows and the main bot. `src/trading/promote.py` copies a shadow's rules onto the main bot by hand (diff preview, confirmation, one-level revert) and never touches open positions, pending triggers or the broker; a promote sets `MAIN_LEARNER_PAUSED` so automatic calibration stops, and in LIVE mode risky changes need the typed text `PROMOTE LIVE`. Promoted rules shape only automated committee entries, and exit rules (breakeven at R, percentage trail, maximum hold) apply only to positions opened under the active rules version (`positions.rules_version`). No rule set bypasses the kill switch, daily loss limit, `validate_buy_order` (including the minimum consensus score), whole-share and affordability checks, fee viability or market hours, and an entry whose shaped stop is missing or not below the entry price is not taken.

## Skills environment

Plugins and project skills for this repo are set up in `.claude/`. See `docs/claude-skills.md` for what is installed and how to turn things on or off.

- Touching the real-money path (`src/trading/live.py`, `broker.py`, `risk.py`, `fees.py`, `main_strategy.py`, `levels.py`, `promote.py`, sizing, kill switch)? Use the `live-money-safety` skill.
- Building or judging a backtest or shadow experiment? Use the `backtest-discipline` skill.

The caveman response style in AGENTS.md is mirrored in `.cursor/rules/`, `.clinerules/`, `.windsurf/rules/`, `.opencode/AGENTS.md` and `.github/copilot-instructions.md`. Change it in all of them together.
