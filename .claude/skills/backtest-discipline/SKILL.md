---
name: backtest-discipline
description: Use when building, running or judging a backtest, a shadow-portfolio experiment, or any claim that a strategy or parameter change "performs better" in this trading bot.
---

# Backtest discipline

A backtest result is only worth something if it would have been possible to trade it. Check every rule below before you report a number.

## Data

- **No look-ahead.** A decision at bar `t` may only use data available at the close of bar `t-1` (or the open of `t` for open-entry rules). Shift indicators with `.shift(1)` before you compare them with today's price. Never use `df["Close"]` of the same bar to decide and to fill.
- **Adjusted vs. raw prices.** yfinance `auto_adjust` changes historical prices after splits and dividends. Use adjusted prices for returns and raw prices for share-count and affordability checks (`src/trading/risk.py` sizing works on the real share price).
- **Survivorship bias.** `config/default_universe.py` is today's universe. A backtest on it over past years overstates returns. Say so in the result, or use a point-in-time universe.
- **Time zones and sessions.** US and EU tickers trade in different sessions. Use `src/data/market_hours.py`; do not assume UTC days line up.
- **Currency.** The account is in EUR. Convert USD fills with the rate of that day (`src/data/currency.py`), not today's rate.

## Costs and fills

- Charge fees with the real fee model: `calculate_broker_fee` / `calculate_round_trip_fee` in `src/trading/fees.py`. Small accounts lose most of their edge to the minimum fee; skip trades that `evaluate_trade_economic_viability` rejects.
- Respect `WHOLE_SHARES_ONLY` (IBKR rejects fractional quantities). A position that cannot afford one share is not opened. Fractional sizing is only valid for a broker that supports it (Alpaca).
- Add slippage (at least a few basis points per side) and fill stops at the next available price, not at the stop level, when the bar gaps through it.
- Respect `MAX_POSITIONS`, `MAX_POSITION_PCT` and available cash. No leverage unless the account has it.

## Evaluation

- Compare against buy-and-hold of a benchmark (e.g. the index ETF of the universe) over the same period, after costs.
- Report: total return, CAGR, max drawdown, Sharpe (or Sortino), number of trades, win rate, average win/loss, exposure %, and total fees paid.
- Split the period: tune on the first part, report on the untouched last part (walk-forward or a simple hold-out). A parameter tuned on the full period proves nothing.
- Too few trades (under ~30) means the result is noise. Say so.
- LLM committee verdicts cannot be replayed honestly on past data (the model has seen the future). Backtest only the rule-based parts (triggers, stops, sizing), or use the shadow portfolios (`src/trading/shadow.py`) for forward testing of committee-driven rules.

## Reporting

State the period, universe, costs, slippage, and which of the biases above are still present. A result without these is not a result.
