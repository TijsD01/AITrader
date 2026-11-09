# Audit plan — aitrader

4 read-only explorers, merged. Rank = money risk first, then measured cost, then bloat.
V = read in code. G = guess, not run. Nothing here is timed unless it says so.

## Shape of the system (decides what "fast" means)

- Poll-driven swing bot: triggers every 3 min, stops every 10 min. No tick feed, no WebSocket, no backtester.
- Hot-path cost is Yahoo/T212 network calls and locks. CPU work is microseconds to low milliseconds.
- So: `orjson`, Polars, DuckDB, VectorBT buy nothing here. JSON payloads are < 1 KB. Pandas has zero row loops (V).
- `uvloop` already ships via `uvicorn[standard]` and is auto-picked on Linux (G from uvicorn defaults).

## Do now (this branch)

| # | Sev | Where | Problem | Fix |
|---|-----|-------|---------|-----|
| 1 | HIGH | risk.py:339 `evaluate_trigger_condition` | NaN quote fires a buy trigger (NaN compares False). Zero quote cancels a healthy pullback trigger. | Non-finite or <= 0 price: `(False, None)`. |
| 2 | HIGH | risk.py:248 `validate_buy_order` | NaN cash/allocation/price returns "Approved". | Reject non-finite numbers and price <= 0. |
| 3 | HIGH | risk.py:409, 532 `plan_buy` chain | NaN price gives NaN budget, then `floor(nan)` raises and aborts the whole trigger loop. Fractional mode would send NaN quantity. | Bad input: 0 shares. |
| 4 | HIGH | risk.py:314 `validate_trigger_levels` | Accepts NaN/inf levels. | Require finite levels. |
| 5 | HIGH | risk.py:501 `compute_trailing_stop` | NaN high moves the stop to entry x 1.01. | Bad number: stop unchanged. |
| 6 | HIGH | live.py:1927, paper.py:548, shadow.py:636 | NaN price: stop never fires, silently. | Guard at the quote source: `get_stock_data` returns None for a non-finite price. |
| 7 | HIGH | currency.py:36 `to_eur` | Sync Yahoo call on the event loop every 15 min, on the stop path. Freezes API, Telegram, all jobs while it hangs. | Inside a running loop: refresh in a thread, serve the cached rate. |
| 8 | HIGH | paper.py:452 | Paper sell awaits the LLM post-mortem under `_monitor_lock`. Later stop-outs wait minutes. | `create_task`. |
| 9 | HIGH | jobs.py:40 | No `misfire_grace_time`: a > 1 s loop stall silently skips a stop check (G for effect). | `job_defaults` grace 60 s + coalesce. |
| 10 | MED | market.py:43 | Closed-market TTL constant defined, never used: full 1-year refetch every 60 s all night. Failed lookups never cached. | Use the closed TTL; cache failures 10 min. |
| 11 | MED | committee, scanner | No per-ticker trend/volatility gate. LLM runs on flat chop. | ADX14 + ATR% in technicals; scanner drops chop before the committee. |
| 12 | MED | jobs.py:740 | Opportunity scanner re-debates the same tickers every 30 min. | In-memory TTL cache of committee reports. |
| 13 | MED | market.py:87-134 | Indicators: 5 pandas `ewm` + 3 `rolling` over the full series for one last value. | NumPy on the tail; measure. |
| 14 | LOW | dead code | See purge list. | Delete. |

## Purge list (certain dead: grep shows definition only)

- `notify_trade` (bot.py:89), `get_wiki_page` (store.py:565), `get_db` (session.py:27), `run_opening_review` (jobs.py:374)
- Routes with no caller and no test: `POST /api/sync` (dup of `/api/portfolio/sync`), `GET /api/reviews/next-session`
- extractor.py:266-296 `use_llm` branch (default False, never passed)
- `ALPACA_BASE_URL` setting (never read)
- ~25 unused imports; 2 unused CSS classes
- Package re-exports in `src/trading/__init__.py`, `src/memory/__init__.py` (no package-level import anywhere)

## Not now — needs owner decision (deleting changes the product surface)

- Memory API: ~260 LOC routes + 10 models + store methods, no in-repo caller. External agents may call it. ~500 LOC.
- `src/memory/proxy.py` (207 LOC): `/v1/chat/completions` mounted on the main app, no in-repo client.
- Alpaca path (~90 LOC + dependency). Still a valid `LIVE_BROKER`. Also blocks the loop (sync SDK, live.py:98).
- Hand-written code graph (store.py:720-838, ~120 LOC). AGENTS.md tells agents to use it.
- Tested-but-unused routes: `POST /api/triggers`, `/api/watchlist` CRUD.
- paper.py vs live.py duplicates (~150 LOC, `get_trigger_plan` etc). Real-money path: own PR with the money-path test list.

## Not now — real, but bigger than this pass

- Lock design: stop check shares `_monitor_lock` with trigger buys and broker sync (jobs.py:565-609). `_t212_lock` held through Yahoo fetches and 2.1 s sleeps (live.py:1815). Split locks.
- Telegram polling awaits each handler inline: `/analyze` (up to 300 s) delays `/killswitch` (bot.py:465).
- `get_portfolio_summary` re-run per candidate (refreshes every position each time). Serial per-ticker fetches in 10 loops.
- Double `GET /equity/positions` per refresh: likely 429 + up to 10 s sleep (holdings.py:86, live.py:1972).
- Duplicate-buy race: duplicate check runs outside `_t212_lock`; manual buys skip `_monitor_lock` (G).
- No client order id on T212 or Alpaca orders (V). T212 lookup after an ambiguous error matches by ticker/qty/time.
- `consensus_score or 80` (live.py:1738), `or 75` (paper.py:951): a missing score bypasses the minimum.
- Daily-loss breaker is live-only; paper and shadows do not mirror it. No peak-drawdown breaker.
- One-share minimum ignores the risk cap (risk.py:553). Fail-open risk cap when the stop is missing (risk.py:377).
- No spread/slippage in paper or shadow fills: they assume 100 % fills at the Yahoo last price.
- Indicators include today's partial bar: ATR low, volume ratio low at the open (market.py:49).
- Missing indexes on `positions(status,is_live)`, `trades`, `portfolio_snapshots(is_live,timestamp)`. Cost unmeasured; single-user SQLite.
- Frontend: 30 s poll sends 7 requests and refetches 30-day history each tick. Job poll has no hidden-tab check.
- Bug: app.js:2966 posts a made-up BESI.AS trade into the memory store when the extract prompt is blank.

## Already there — do not rebuild

- ATR stop (1.75 x ATR, clamped 2.5-7 %) and stop-distance sizing cap (`risk_capped_allocation`).
- Daily drawdown breaker: `MAX_DAILY_LOSS_EUR` / `MAX_DAILY_LOSS_PCT` trips the kill switch (live.py:487).
- Index regime filter (200-day SMA), checked at arm time and fill time.
- No LLM call on trigger fire or live stop path (V). Trigger gate reads a stored report.
