---
name: live-money-safety
description: Use before changing anything on the real-money path of this trading bot - src/trading/live.py, broker.py, risk.py, fees.py, trigger_gate.py, cash_flows.py, order sizing, the kill switch, or TRADING_MODE / LIVE_TRADING_ENABLED settings.
---

# Live-money safety

This bot can place real orders. A bug here costs money, not just a failing test.

## Before you edit

1. Read how the active broker is picked: `get_active_broker` in `src/trading/broker.py`. Live orders need `TRADING_MODE=LIVE` **and** `LIVE_TRADING_ENABLED=True`.
2. Know the interlocks and keep them working:
   - `KILL_SWITCH_ACTIVE` blocks new buys (`validate_buy_order`, `LiveBroker.is_live_armed`) while stops and sells keep working.
   - `MAX_DAILY_LOSS_EUR` turns the kill switch on.
   - `WHOLE_SHARES_ONLY`, `MAX_POSITIONS`, `MAX_POSITION_PCT` cap order size.
3. Never weaken an interlock to make a test pass. If a test fails on one, the change is wrong or the test needs a deliberate, explained update.

## While you edit

- Change paper and live together. `PaperBroker` must mirror `LiveBroker` behaviour (fees, whole shares, sizing) or paper results lie.
- Orders must be idempotent: a retry after a timeout must not place a second order. Check for an existing open order or fill first.
- Treat broker responses as untrusted: check for partial fills, rejected orders and zero/None prices before you update positions or cash.
- Never log or print API keys, account IDs or `.env` values.
- Money is EUR. Keep currency conversion explicit (`src/data/currency.py`).

## Before you call it done

Run the tests that guard the money path:

```bash
.venv/bin/python -m pytest tests/test_live_trading.py tests/test_paper_trading.py tests/test_broker_abstraction.py \
  tests/test_broker_fees.py tests/test_triggers.py tests/test_trigger_gate.py tests/test_trigger_allocation.py \
  tests/test_entry_sizing.py tests/test_whole_shares.py tests/test_min_one_share.py tests/test_capital_usage_limit.py \
  tests/test_cash_flows.py tests/test_portfolio_sync.py -q
```

Then the full suite: `.venv/bin/python -m pytest tests/ -q`.

Add a test for every new branch in sizing, fees or order handling, including the "cannot afford one share" and "kill switch on" cases. Run the change in paper mode before anyone flips `LIVE_TRADING_ENABLED`. Say in the PR which interlocks the change touches.
