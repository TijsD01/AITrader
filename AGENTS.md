# TradingBotNew Agent Instructions

> **Project Scope:** Multi-Agent Quantitative Investment & Trading Bot  
> **Root Directory:** `/home/tdapper/TradingBotNew`

---

## ⚡ Token-Efficient Navigation Rules

1. **Use Exact Line Ranges on Large Files:** Never read entire large files (`src/trading/live.py`, `src/trading/paper.py`, `src/telegram/bot.py`, `src/web/routes.py`, `src/web/static/app.js`). `grep -n` the symbol first, then `view_file` with narrow `StartLine`/`EndLine`.
2. **Skip Binary & Generated Artifacts:** Never scan `data/*.db*`, `data/*.log`, `.venv/`, `__pycache__/`, `.pytest_cache/`, or `src/web/static/app.js` (2,748 lines) unless specifically editing frontend UI.
3. **Query TencentDB CodeGraph:** Use `memory_store.search_codegraph("<symbol>")` or `GET /v3/codegraph/search?query=<symbol>` for symbol dependencies and callers.

---

## 🗺️ Subsystem & Symbol Map

- **Agent Committee (`src/agents/`):**
  - [`committee.py`](file:///home/tdapper/TradingBotNew/src/agents/committee.py) (`InvestmentCommittee`, `evaluate_ticker`)
  - [`risk_manager.py`](file:///home/tdapper/TradingBotNew/src/agents/risk_manager.py) (`RiskManagerAgent`, `evaluate_risk`, `_validate_llm_levels`)
  - [`technical.py`](file:///home/tdapper/TradingBotNew/src/agents/technical.py), [`fundamental.py`](file:///home/tdapper/TradingBotNew/src/agents/fundamental.py), [`sentiment.py`](file:///home/tdapper/TradingBotNew/src/agents/sentiment.py), [`base.py`](file:///home/tdapper/TradingBotNew/src/agents/base.py) (`LLMClient`, `merge_llm_report`)
- **TencentDB Agent Memory (`src/memory/`, SQLite: `data/memory.db`):**
  - [`store.py`](file:///home/tdapper/TradingBotNew/src/memory/store.py) (`AgentMemoryStore`, `search_l1_atoms`, `search_codegraph`, `prune_and_compact`, `refresh_codegraph`)
  - [`cold_start.py`](file:///home/tdapper/TradingBotNew/src/memory/cold_start.py) (`ColdStartEngine`, `generate_cold_start_loadout`)
  - [`extractor.py`](file:///home/tdapper/TradingBotNew/src/memory/extractor.py) (`AssetExtractor`), [`client.py`](file:///home/tdapper/TradingBotNew/src/memory/client.py), [`routes.py`](file:///home/tdapper/TradingBotNew/src/memory/routes.py), [`proxy.py`](file:///home/tdapper/TradingBotNew/src/memory/proxy.py)
- **Broker & Risk System (`src/trading/`):**
  - [`broker.py`](file:///home/tdapper/TradingBotNew/src/trading/broker.py) (`get_active_broker`)
  - [`live.py`](file:///home/tdapper/TradingBotNew/src/trading/live.py) (`LiveBroker`, `get_portfolio_summary`, `execute_buy`, `execute_sell`, `create_price_trigger`, `get_triggers`, `check_and_execute_triggers`, `sync_account_state`)
  - [`paper.py`](file:///home/tdapper/TradingBotNew/src/trading/paper.py) (`PaperBroker`, `get_portfolio_summary`, `execute_buy`, `execute_sell`, `create_price_trigger`, `get_triggers`, `check_and_execute_triggers`)
  - [`risk.py`](file:///home/tdapper/TradingBotNew/src/trading/risk.py) (`calculate_position_allocation`, `validate_buy_order`, `risk_capped_allocation`, `plan_pending_triggers`)
  - [`fees.py`](file:///home/tdapper/TradingBotNew/src/trading/fees.py) (`calculate_broker_fee`, `calculate_round_trip_fee`, `evaluate_trade_economic_viability`)
  - [`trigger_gate.py`](file:///home/tdapper/TradingBotNew/src/trading/trigger_gate.py) (`evaluate_trigger_verdict`), [`learner.py`](file:///home/tdapper/TradingBotNew/src/trading/learner.py) (`TradeLearner`), [`shadow.py`](file:///home/tdapper/TradingBotNew/src/trading/shadow.py) (`ShadowPortfolios`), [`cash_flows.py`](file:///home/tdapper/TradingBotNew/src/trading/cash_flows.py)
- **Data, DB, Scheduler, Web & Telegram:**
  - [`src/db/models.py`](file:///home/tdapper/TradingBotNew/src/db/models.py) & [`src/db/session.py`](file:///home/tdapper/TradingBotNew/src/db/session.py) (`data/trading_bot.db`)
  - [`src/data/market.py`](file:///home/tdapper/TradingBotNew/src/data/market.py), [`src/data/market_hours.py`](file:///home/tdapper/TradingBotNew/src/data/market_hours.py), [`src/data/currency.py`](file:///home/tdapper/TradingBotNew/src/data/currency.py), [`src/data/review_tracker.py`](file:///home/tdapper/TradingBotNew/src/data/review_tracker.py)
  - [`src/screener/scanner.py`](file:///home/tdapper/TradingBotNew/src/screener/scanner.py) (`MarketScanner`) & [`config/default_universe.py`](file:///home/tdapper/TradingBotNew/config/default_universe.py), [`config/settings.py`](file:///home/tdapper/TradingBotNew/config/settings.py)
  - [`src/scheduler/jobs.py`](file:///home/tdapper/TradingBotNew/src/scheduler/jobs.py) (`TradingScheduler`, `run_market_session`)
  - [`src/web/app.py`](file:///home/tdapper/TradingBotNew/src/web/app.py), [`src/web/routes.py`](file:///home/tdapper/TradingBotNew/src/web/routes.py), [`src/web/security.py`](file:///home/tdapper/TradingBotNew/src/web/security.py)
  - [`src/telegram/bot.py`](file:///home/tdapper/TradingBotNew/src/telegram/bot.py) (`TelegramAssistant`)

---

## 🛠️ Verification & Run Commands

```bash
# Run targeted test module first (saves time & tokens)
.venv/bin/python -m pytest tests/test_agent_memory.py -q
.venv/bin/python -m pytest tests/test_agents.py -q
.venv/bin/python -m pytest tests/test_triggers.py -q

# Run full test suite
.venv/bin/python -m pytest tests/ -q

# Prune & compact TencentDB Agent Memory
.venv/bin/python -c "import asyncio; from src.memory.store import memory_store; print(asyncio.run(memory_store.prune_and_compact()))"
```
