# TradingBotNew Agent Instructions

> **Project Scope:** Multi-Agent Quantitative Investment & Trading Bot  
> **Root Directory:** `/home/tdapper/TradingBotNew`

---

## ⚡ Instant Architecture Navigation

Consult [`graphify-out/wiki/index.md`](file:///home/tdapper/TradingBotNew/graphify-out/wiki/index.md) or run `graphify query "<question>"` before reading raw files.

### Subsystem Map
- **Agent Committee:** [`src/agents/committee.py`](file:///home/tdapper/TradingBotNew/src/agents/committee.py) coordinates [`fundamental.py`](file:///home/tdapper/TradingBotNew/src/agents/fundamental.py), [`technical.py`](file:///home/tdapper/TradingBotNew/src/agents/technical.py), [`sentiment.py`](file:///home/tdapper/TradingBotNew/src/agents/sentiment.py), and [`risk_manager.py`](file:///home/tdapper/TradingBotNew/src/agents/risk_manager.py).
- **LLM Engine:** [`src/agents/base.py`](file:///home/tdapper/TradingBotNew/src/agents/base.py) invokes the local Antigravity CLI (`agy`) without external API keys.
- **Broker System:** [`src/trading/broker.py`](file:///home/tdapper/TradingBotNew/src/trading/broker.py) switches between [`live.py`](file:///home/tdapper/TradingBotNew/src/trading/live.py) and [`paper.py`](file:///home/tdapper/TradingBotNew/src/trading/paper.py).
- **Risk Validation:** [`src/trading/risk.py`](file:///home/tdapper/TradingBotNew/src/trading/risk.py) executes `calculate_position_allocation` and `validate_buy_order`.
- **Database:** [`src/db/models.py`](file:///home/tdapper/TradingBotNew/src/db/models.py) and [`src/db/session.py`](file:///home/tdapper/TradingBotNew/src/db/session.py) (`init_db`). SQLite file: `data/trading_bot.db`.
- **Scheduler:** [`src/scheduler/jobs.py`](file:///home/tdapper/TradingBotNew/src/scheduler/jobs.py) handles market sessions and cron triggers.
- **Web App:** [`src/web/app.py`](file:///home/tdapper/TradingBotNew/src/web/app.py) & [`src/web/routes.py`](file:///home/tdapper/TradingBotNew/src/web/routes.py).
- **Telegram Bot:** [`src/telegram/bot.py`](file:///home/tdapper/TradingBotNew/src/telegram/bot.py).
- **Scanner:** [`src/screener/scanner.py`](file:///home/tdapper/TradingBotNew/src/screener/scanner.py) with [`config/default_universe.py`](file:///home/tdapper/TradingBotNew/config/default_universe.py).

---

## 🛠️ Verification & Run Commands

```bash
# Run the application
python run.py

# Run all tests
pytest tests/

# Focus on specific test modules
pytest tests/test_agents.py
pytest tests/test_antigravity_agent.py
pytest tests/test_triggers.py

# Keep graph updated after editing code
graphify update .
```
