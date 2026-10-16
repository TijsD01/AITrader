# TradingBotNew Agent Instructions

> **Project Scope:** Multi-Agent Quantitative Investment & Trading Bot  
> **Root Directory:** `/home/tdapper/TradingBotNew`

---

## ⚡ Token-Efficient Navigation Rules

1. **Use Exact Line Ranges on Large Files:** Never read entire large files (`src/trading/live.py`, `src/trading/paper.py`, `src/telegram/bot.py`, `src/web/routes.py`, `src/web/static/app.js`). Use the symbol line map below or `grep_search` first, then `view_file` with narrow `StartLine`/`EndLine`.
2. **Skip Binary & Generated Artifacts:** Never scan `data/*.db*`, `data/*.log`, `.venv/`, `__pycache__/`, `.pytest_cache/`, or `src/web/static/app.js` (2,748 lines) unless specifically editing frontend UI.
3. **Query TencentDB CodeGraph:** Use `memory_store.search_codegraph("<symbol>")` or `GET /v3/codegraph/search?query=<symbol>` for symbol dependencies and callers.

---

## 🗺️ Subsystem & Symbol Line Map

- **Agent Committee (`src/agents/`):**
  - [`committee.py`](file:///home/tdapper/TradingBotNew/src/agents/committee.py) (`InvestmentCommittee:16-264`, `evaluate_ticker:53-264`)
  - [`risk_manager.py`](file:///home/tdapper/TradingBotNew/src/agents/risk_manager.py) (`RiskManagerAgent:9-368`, `evaluate_risk:13-302`, `_validate_llm_levels:305-368`)
  - [`technical.py`](file:///home/tdapper/TradingBotNew/src/agents/technical.py), [`fundamental.py`](file:///home/tdapper/TradingBotNew/src/agents/fundamental.py), [`sentiment.py`](file:///home/tdapper/TradingBotNew/src/agents/sentiment.py), [`base.py`](file:///home/tdapper/TradingBotNew/src/agents/base.py) (`LLMClient:55-254`, `merge_llm_report:12-52`)
- **TencentDB Agent Memory (`src/memory/`, SQLite: `data/memory.db`):**
  - [`store.py`](file:///home/tdapper/TradingBotNew/src/memory/store.py) (`AgentMemoryStore:76-994`, `search_l1_atoms:297-364`, `search_codegraph:616-639`, `prune_and_compact:641-747`, `refresh_codegraph:749-760`)
  - [`cold_start.py`](file:///home/tdapper/TradingBotNew/src/memory/cold_start.py) (`ColdStartEngine:13-275`, `generate_cold_start_loadout:26-165`)
  - [`extractor.py`](file:///home/tdapper/TradingBotNew/src/memory/extractor.py) (`AssetExtractor:33-349`), [`client.py`](file:///home/tdapper/TradingBotNew/src/memory/client.py), [`routes.py`](file:///home/tdapper/TradingBotNew/src/memory/routes.py), [`proxy.py`](file:///home/tdapper/TradingBotNew/src/memory/proxy.py)
- **Broker & Risk System (`src/trading/`):**
  - [`broker.py`](file:///home/tdapper/TradingBotNew/src/trading/broker.py) (`get_active_broker:10-20`)
  - [`live.py`](file:///home/tdapper/TradingBotNew/src/trading/live.py) (`LiveBroker:26-1666`, `get_portfolio_summary:346-440`, `execute_buy:472-734`, `execute_sell:736-878`, `create_price_trigger:880-1023`, `get_triggers:1114-1167`, `check_and_execute_triggers:1169-1246`, `sync_account_state:1415-1557`)
  - [`paper.py`](file:///home/tdapper/TradingBotNew/src/trading/paper.py) (`PaperBroker:19-949`, `get_portfolio_summary:47-97`, `execute_buy:126-331`, `execute_sell:333-479`, `create_price_trigger:554-720`, `get_triggers:813-861`, `check_and_execute_triggers:863-949`)
  - [`risk.py`](file:///home/tdapper/TradingBotNew/src/trading/risk.py) (`calculate_position_allocation:31-102`, `validate_buy_order:188-268`, `risk_capped_allocation:374-397`, `plan_pending_triggers:430-450`)
  - [`fees.py`](file:///home/tdapper/TradingBotNew/src/trading/fees.py) (`calculate_broker_fee:42-153`, `calculate_round_trip_fee:155-197`, `evaluate_trade_economic_viability:234-377`)
  - [`trigger_gate.py`](file:///home/tdapper/TradingBotNew/src/trading/trigger_gate.py) (`evaluate_trigger_verdict:87-122`), [`learner.py`](file:///home/tdapper/TradingBotNew/src/trading/learner.py) (`TradeLearner:13-614`), [`shadow.py`](file:///home/tdapper/TradingBotNew/src/trading/shadow.py) (`ShadowPortfolios:210-818`), [`cash_flows.py`](file:///home/tdapper/TradingBotNew/src/trading/cash_flows.py)
- **Data, DB, Scheduler, Web & Telegram:**
  - [`src/db/models.py`](file:///home/tdapper/TradingBotNew/src/db/models.py) & [`src/db/session.py`](file:///home/tdapper/TradingBotNew/src/db/session.py) (`data/trading_bot.db`)
  - [`src/data/market.py`](file:///home/tdapper/TradingBotNew/src/data/market.py), [`src/data/market_hours.py`](file:///home/tdapper/TradingBotNew/src/data/market_hours.py), [`src/data/currency.py`](file:///home/tdapper/TradingBotNew/src/data/currency.py), [`src/data/review_tracker.py`](file:///home/tdapper/TradingBotNew/src/data/review_tracker.py)
  - [`src/screener/scanner.py`](file:///home/tdapper/TradingBotNew/src/screener/scanner.py) (`MarketScanner:13-125`) & [`config/default_universe.py`](file:///home/tdapper/TradingBotNew/config/default_universe.py), [`config/settings.py`](file:///home/tdapper/TradingBotNew/config/settings.py)
  - [`src/scheduler/jobs.py`](file:///home/tdapper/TradingBotNew/src/scheduler/jobs.py) (`TradingScheduler:27-571`, `run_market_session:195-385`)
  - [`src/web/app.py`](file:///home/tdapper/TradingBotNew/src/web/app.py), [`src/web/routes.py`](file:///home/tdapper/TradingBotNew/src/web/routes.py), [`src/web/security.py`](file:///home/tdapper/TradingBotNew/src/web/security.py)
  - [`src/telegram/bot.py`](file:///home/tdapper/TradingBotNew/src/telegram/bot.py) (`TelegramAssistant:22-1349`)

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
