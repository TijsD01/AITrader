# Internal Code Audit: Agent-Memory Subsystem and Its Effect on Trading P&L

Scope: repo at /home/user/AITrader, as of 2026-11-09. Read-only audit. Citations are `file:line` in this repo. "Fact" means read directly in code or observed in a run. "Inference" means a conclusion drawn from facts. `data/` held no live `memory.db` or `trading_bot.db`, so no production memory contents could be inspected (see Gaps).

Verification runs:
- `python -m pytest tests/test_agent_memory.py tests/test_learner.py -q` gave 31 passed.
- A scratch simulation (scratchpad `sim.py`, separate SQLite file, portfolio and recap stubbed) fed committee and trade-fill texts through `AssetExtractor` and built a cold-start loadout. Results are cited below as "[sim]".

---

## Q1. What gets written to memory, which fields, and are trade outcomes linked back to the decision that caused them?

### Takeaway
Memory gets three kinds of fire-and-forget text events: committee verdicts, trade fills and trigger arms. Regex extraction turns them into a few L1 atoms, and those atoms are overwritten per ticker. No outcome (P&L, R-multiple, holding time, exit reason) is linked to the verdict or report that caused the trade, either in memory or in the SQL schema.

### Cited Findings
**Writers (all `asyncio.create_task`, no reference kept, no done-callback):**
- Committee verdict: `src/agents/committee.py:234-245` calls `log_interaction`. The content is `"Deliberation on {ticker} ({action}, Score N/100):\n{transcript}\n\nVerdict: {summary}"`. The metadata is `{ticker, action, consensus_score, price}`. It runs on every evaluation, LLM or rules path, including HOLD/SELL.
- Trade fills: `src/trading/paper.py:310-325` (BUY), `src/trading/paper.py:430-446` (SELL), `src/trading/live.py:733-748` (BUY) and `src/trading/live.py:1141-1157` (SELL) call `record_trade_event`.
- Trigger armed: `src/trading/paper.py:703-717` and `src/trading/live.py:1305-1319` call `record_trigger_event`.
- No memory event is written when a trigger fills, cancels or expires. No event is written for a stop or target hit beyond the generic SELL. No event is written for a vetoed verdict beyond the deliberation text. Evidence: grep for `record_*`/`log_interaction` finds only the call sites above.

**Fields written:**
- `record_trade_event` (`src/memory/client.py:76-110`) writes free text "Executed {BUY|SELL} on {shares}x {ticker} at {price} {currency} with fee… realizing PnL of €… Rationale: {rationale}" plus metadata `{event:"trade_execution", ticker, shares, price, pnl_eur, fee_eur}`. It has no `pnl_pct`, R-multiple, holding time, entry price, initial stop, report id or trigger id. `session_id = trade_{ticker}_{epoch}` (`client.py:96`).
- `record_trigger_event` (`src/memory/client.py:112-141`) writes "Armed order trigger #{id} for {ticker} ({type}) at target price €{target_price:.2f}…". `target_price` is in native currency (`src/db/models.py:175`), but the text always says € (`client.py:125`).
- L0 storage: `memory_l0_conversations(session_id, team_id, agent_id, user_id, task_id, role, content, metadata_json, created_at)` (`src/memory/store.py:104-117`, insert at `store.py:232-248`).

**Extraction to L1/L2/L3 (`src/memory/extractor.py`):**
- Verdict atom: `atom_id = atom_verdict_{ticker}` (`extractor.py:208-220`) with `INSERT OR REPLACE` (`store.py:288-293`). Only the latest verdict per ticker survives. [sim] confirms that a BUY 82 then HOLD 60 left one atom, "HOLD (Score: 60)".
- Trade atom: `atom_id = atom_trade_{ticker}` (`extractor.py:230-246`), also overwritten. [sim] confirms that a BUY then SELL kept only the SELL atom. The SELL atom's `content` is the first 160 chars of the fill text (`extractor.py:243`), and this is the only place realized P&L reaches L1.
- Trigger atom: `atom_trig_{ticker}_{id or "latest"}` (`extractor.py:93-126`). The committee transcript has no trigger id, so a BUY deliberation creates `…_latest` with "Trigger armed for X at €P" even when no trigger was armed. The rules transcript always says "AI Trigger armed: …" (`committee.py:221`), and arming happens later and may be skipped for score, regime, cash or share price (`src/scheduler/jobs.py:316-367`). [sim] produced `atom_trig_nvda_latest` "€182.5" for a USD stock. That atom survived a later HOLD verdict.
- L2 scenario per ticker, `evaluation-{ticker}`. The summary is "Deliberated {verdict} on {ticker} (Score n/100)" (`extractor.py:222-229`, upsert `store.py:399-415`).
- L3 "wisdom" can only gain one fixed sentence, triggered by the word "veto" or "fee drag" (`extractor.py:262-264`).
- A "Dynamically Extracted Pullback Entry" skill is upserted whenever text contains "pullback" plus "limit"/"rsi" (`extractor.py:248-260`). Its content is generic.
- The `atom_cash_balance` singleton is overwritten from any "with €N" phrase (`extractor.py:157-178`).

**Outcome linkage in the SQL schema (outside memory):**
- `Trade` has `consensus_score` and `rationale` but no FK to `AnalysisReport`, `OrderTrigger` or the opening BUY (`src/db/models.py:72-94`).
- The SELL `Trade` row is written without `consensus_score` (`src/trading/paper.py:382-396`).
- `OrderTrigger.executed_trade_id` links a trigger to its BUY trade (`models.py:187`). `AnalysisReport` has no trigger or trade link (`models.py:125-145`). It is persisted separately in `jobs.py:297-313`, and `create_price_trigger` receives no report id (`jobs.py:347-359`).
- `Position` has no trigger or report id (`models.py:25-53`). `stop_loss_native` is mutated by breakeven and trailing logic (`paper.py:490-500`), so the initial risk (1R) is not preserved anywhere.
- `TradeLesson` holds `outcome, realized_pnl_eur/pct, exit_reason, holding_period_hours, what_went_well/wrong, lesson` (`models.py:189-204`). It is the only outcome record, it is keyed by `trade_id` (the SELL) and it has no `is_live` flag.

### Inferences
- Memory is a "last state per ticker" cache, not an episodic outcome log. It cannot answer "how did past BUYs at score ≥80 on this setup turn out?" because the verdict→outcome pair is never stored together, and older verdicts and trades are overwritten.
- No column holds R-multiple, MAE/MFE or initial stop. Any outcome-weighted learning would first need schema changes: `report_id` on trigger, position and trade, plus initial stop and entry snapshot.

### Gaps
- No production `memory.db` was available. The actual atom distribution and pollution level are unknown.

---

## Q2. How does `store.py` rank, search and prune atoms? Ticker-keyed or setup/regime-similar? Outcome-weighted?

### Takeaway
Retrieval is SQL `LIKE` keyword matching, ranked by match tier and then recency. There are no embeddings, no setup or regime features, and the stored `confidence` plays no part in ranking. No outcome weighting exists. Pruning is pattern-based test cleanup plus an L0 row cap, and it runs only when called manually.

### Cited Findings
- `search_l1_atoms` (`src/memory/store.py:297-364`) tokenizes the query (`_query_tokens`, `store.py:39-52`).
  - Tokens of 1-2 characters match `subject` exactly or as a prefix. Longer tokens use `LIKE` on content, subject or object (`store.py:314-327`).
  - All `type='instruction'` atoms are always included (`store.py:331-332`).
  - The ORDER BY tiers are: exact subject, then subject LIKE, then any match, then instruction, then `updated_at DESC` (`store.py:334-340`).
  - Results dedupe on `(type, subject, predicate, object)` (`store.py:354-363`).
- The `confidence` column is written (`store.py:134`, extractor sets 0.88-0.98), but `search_l1_atoms` never reads it (`store.py:297-364`).
- The cold-start query is just the ticker (`src/agents/committee.py:62-64`, `query=ticker`), so retrieval is ticker-keyed. No regime, sector, signal-vector or setup-type key exists anywhere in the atom schema (`store.py:123-139`; `models.py:39-51`).
- `search_skills` uses LIKE on name and description, ordered by `created_at DESC` (`store.py:521-550`). A bare ticker never matches a skill, so the fallback returns the 2 newest skills (`src/memory/cold_start.py:48-50`).
- `prune_and_compact` (`store.py:641-718`):
  1. Deletes L1 rows whose subjects are stop-words or test subjects, and lowercase-subject trigger/verdict atoms (`store.py:651-671`).
  2. Deletes test L2 scenarios (`store.py:675-683`).
  3. Deletes test L0 sessions, then caps L0 at `max_l0_rows=150` (`store.py:687-706`).
  4. Refreshes the codegraph, then checkpoints WAL and VACUUMs (`store.py:710-717`).
  It never decays or expires stale L1 atoms, such as filled or cancelled triggers or old verdicts.
- The only caller of `prune_and_compact` is the HTTP route `POST /v3/memory/compact` (`src/memory/routes.py:277-279`). No scheduler job calls it; the jobs file only registers market, monitor, sync and `learner_idle_decay` jobs (`src/scheduler/jobs.py:84-155`). The AGENTS.md command is also manual.

### Inferences
- Atoms are overwritten per ticker, so L1 stays bounded by roughly the universe (52 EU + 51 US tickers per `config/default_universe.py`) times 3 atom kinds. L0 grows without bound until someone calls compact manually (one row per evaluation and per fill).
- Ranking cannot surface "similar past setups that won or lost" by construction.

### Gaps
- The L0 growth rate in production is unmeasured (no DB available).

---

## Q3. What does `ColdStartEngine` produce, how does `evaluate_ticker` use it, and does memory matter when the LLM is down?

### Takeaway
The loadout is a text block of up to 2,400 characters, injected only into the committee LLM debate prompt. Its builder runs before the LLM-availability check but its output is unused when no LLM is active. When the LLM is unavailable, times out or returns malformed JSON, memory has zero effect on the action, score, entry, stop or size.

### Cited Findings
**Loadout construction (`src/memory/cold_start.py:27-168`):**
- Parallel fetch of: portfolio state, L3 profile, all L2 scenarios, 6 L1 atoms, 3 skills and all wiki pages (`cold_start.py:39-46`).
- Sections in order: header, portfolio status, "what had happened" recap, L3 persona plus last 3 wisdom lines plus risk rules, matched L2 scenario, up to 5 L1 facts, skills (`cold_start.py:136-148`).
- L1 facts with subject `Broker_Fee`/`RiskManager` are filtered out (`cold_start.py:107-111`).
- Hard truncation at `char_budget` from the end (`cold_start.py:151-152`). This cuts the ticker-specific L1 and skills sections first, while the static persona text survives.
- `wiki_highlights` is computed (`cold_start.py:120-123`) but not put into `full_prompt_context`. It is returned only in the model (`cold_start.py:164`).
- The recap uses all trades in the last 30 days for portfolio win rate and P&L, plus up to 8 rows for this ticker (`cold_start.py:256-293`). Lessons are added only if `include_lessons` (`cold_start.py:294-305`).
- With no ticker match, `matched_sc` stays None and a generic default scenario text is used (`cold_start.py:71-103`).

**Use in the committee:**
- `committee.py:59-67` calls `get_cold_start_loadout(query=ticker, char_budget=2400, include_lessons=False)`. Lessons are excluded here because `trade_learner.get_learning_context_for_agents()` supplies them instead (`committee.py:69-73`; `learner.py:603-641`).
- `memory_context` appears only in `user_prompt` (`committee.py:111-148`, at line 114). That prompt is sent only when `llm_client.has_active_key()` (`committee.py:150-152`).
- Technical, fundamental and sentiment agents never receive memory. Their LLM calls are at `technical.py:112-113`, `fundamental.py:110-111` and `sentiment.py:116-117`; a grep for memory or lessons finds nothing in `src/agents/` except the committee.
- `RiskManagerAgent` computes entry, stop, target and size deterministically, with no LLM and no memory (`src/agents/risk_manager.py:36-38` comment). Its only learned inputs are `ATR_STOP_MULTIPLIER` and `TAKE_PROFIT_RR` from `SystemConfig` (`risk_manager.py:20-34`).

**When the LLM path fails:**
- `_parse_llm_verdict` returns None, and the rules formula sets the score from tech, fund, sent and risk with SystemConfig weights (`committee.py:173-185`). `memory_context` and `learning_context` are unused on this path.
- Failure is common for the default provider. `LLM_PROVIDER=antigravity` shells out with a 90 s timeout (`src/agents/base.py:120-133`) and returns None on failure (`base.py:186-191`). The risk-manager comment records that agy "hit the 90s timeout in ~98% of runs" for level generation (`risk_manager.py:38`).
- The report records `score_source` as `"llm"` or `"rules"` (`committee.py:172, 232, 254`), so the share of memory-influenced decisions can be measured from `AnalysisReport.raw_signals`.

**Even on the LLM path, memory is bounded:**
- The LLM picks the action and score.
- A risk veto or sentiment veto caps the score at 55 (`committee.py:159-169`).
- Arming then requires a score ≥ `TRIGGER_MIN_SCORE` and ≥ `MIN_CONSENSUS_SCORE`, a BUY action and risk approval (`src/trading/trigger_gate.py:87-122`; `jobs.py:44-51`).

**Cost of building the loadout:**
- It runs on every evaluation, even when no LLM is active (`committee.py:59-67`, before `committee.py:150`).
- `_fetch_portfolio_status` calls the broker's `get_portfolio_summary`, `get_triggers` and `get_positions` (`cold_start.py:180-186`). The code's docstring says that on paper these calls "refresh P&L and re-adjust triggers (DB writes)" (`cold_start.py:174-175`). The result is cached for 30 s (`cold_start.py:12, 177-179`).

### Inferences
- When `score_source=="rules"`, memory's P&L effect is exactly zero. On the LLM path it can only nudge the LLM's 0-100 score. Most of the content it adds is not ticker-specific edge: about 900 of roughly 2,000 chars in [sim] were static persona and risk-rule boilerplate. The ticker-specific part is mostly that ticker's own previous verdict and score, which invites anchoring and self-confirmation rather than learning from outcomes.
- Building the loadout on the rules path wastes broker and DB calls. In live mode those reach the broker API (rate-limit exposure; not verified).

### Gaps
- The real fraction of `score_source=="llm"` versus `"rules"` in production is unknown without a DB.
- Loadout latency is only logged (`cold_start.py:154-155`) and was not measured on production data.

---

## Q4. How does `AssetExtractor` work and where is it called?

### Takeaway
It is a regex/heuristic extractor that runs in the background after every committee verdict, trade fill and trigger arm. The proxy and memory routes also call it. Its LLM branch is dead code, and it produces low-information, sometimes wrong atoms.

### Cited Findings
- Callers:
  - `AgentMemoryClient.log_interaction`, as a fire-and-forget task (`client.py:63-74`), reached from the committee, paper broker and live broker.
  - Memory proxy endpoints, also fire-and-forget (`src/memory/proxy.py:84-92` and around `proxy.py:159`).
  - Memory routes `/v3/conversation/add`, `/v3/memory/extract` and `/capture` (`routes.py:108, 322, 344`).
- `use_llm` defaults to False (`extractor.py:51`). No caller passes `use_llm=True` (grep across `src/`). The LLM extraction block (`extractor.py:266-296`) never runs in production.
- Ticker detection uses an uppercase-token regex filtered by a stop-word set and the universe (`extractor.py:14-30, 69-78`).
- Data-quality defects:
  - Trigger prices are always labelled € (`extractor.py:121-122`).
  - Phantom "latest" trigger atoms come from committee prose ([sim]; `extractor.py:101`).
  - The default score fallback is "75" when no score is found (`extractor.py:203-204`).
  - The cash-balance atom is taken from any "with €N" phrase (`extractor.py:158-178`).
- Proxy traffic defaults to `team_id="trading-team"`, `agent_id="committee"` (`proxy.py:46-47` and the Anthropic route). Coding-agent chats sent through the proxy therefore write into the same namespace the trading committee reads (fact). Whether the proxy is used in practice is unknown.
- Dead or unused code: `AgentMemoryClient.recall`/`capture` (`client.py:145-159`) have no callers; routes use the store and extractor directly (`routes.py:333-349`). `wiki_highlights` never reaches the prompt (Q3).

### Inferences
- The extractor adds no information beyond what is already in SQL tables (`Trade`, `OrderTrigger`, `AnalysisReport`). It re-encodes that data lossily, sometimes incorrectly, and keeps only the latest row per ticker.

### Gaps
- None material.

---

## Q5. How do TradeLearner, review_tracker and ShadowPortfolios overlap with memory, and which of them change numeric decisions?

### Takeaway
Only `TradeLearner` changes numbers: score thresholds, ATR stop multiplier, take-profit R:R, breakeven %, and committee weights. The weights matter only on the rules path. Calibration and post-mortems are wired only into the paper broker's sell, so live trading never auto-learns. `review_tracker` and `ShadowPortfolios` hold the richest verdict→outcome data in the system, but both are display-only and feed neither memory nor calibration.

### Cited Findings
**TradeLearner (`src/trading/learner.py`):**
- Post-mortem (`learner.py:21-204`):
  - Finds the "original" report as the newest `AnalysisReport` for the ticker (`learner.py:66-69`), not the report that armed the trade. A held ticker can be re-evaluated and persisted while open (`jobs.py:283-313` selects held "other" candidates when there are fewer than 4 actionable ones). The post-mortem score can therefore come from a report written after entry (misattribution / lookahead).
  - Finds the position as the newest `Position` by ticker, with no `is_live` or status filter (`learner.py:48-51`).
  - Lessons are rule templates (`learner.py:87-151`), optionally rewritten by the LLM (`learner.py:153-176`).
- Calibration (`learner.py:206-514`):
  - Gated by `LEARNER_MIN_TRADES=20` closed trades since the last run (`learner.py:219-245`; `config/settings.py:126`).
  - Takes the last 20 SELLs of the active mode (`learner.py:216-238`).
  - The LLM proposes values clamped to ranges (`learner.py:357-401`). Otherwise rules apply (`learner.py:405-459`): +1 score on a losing streak or win rate < 40%; −2 on a ≥70% win rate; +0.15 ATR multiplier after ≥2 stop-outs; weights shifted to risk after 3 losses.
  - The rules path always resets `BREAKEVEN_TRIGGER_PCT` to 0.025 (`learner.py:431-432`).
  - Writes go to `SystemConfig` and in-memory `settings` (`learner.py:469-502`).
  - The input signal is win rate and streaks only. There is no expectancy, no R and no per-setup split.
- Idle decay lowers thresholds by 1 point a day after 7 idle days (`learner.py:516-601`), via the daily job (`jobs.py:150-155, 495-499`).
- Where the tuned values are consumed:
  - `MIN_CONSENSUS_SCORE` in `trigger_gate.py:107-113`, in the rules buy threshold (`committee.py:184-185`) and in broker validation.
  - `TRIGGER_MIN_SCORE` in `jobs.py:259-273`, `jobs.py:585-590` and `trigger_gate.py:67-80`.
  - `ATR_STOP_MULTIPLIER` and `TAKE_PROFIT_RR` in `risk_manager.py:31-33`.
  - `BREAKEVEN_TRIGGER_PCT` in `paper.py:493-498` and `live.py:1943`.
  - `WEIGHT_*` only in `committee.py:84-88` → `committee.py:181` (rules path).
- Wiring gap: `conduct_trade_post_mortem` and `calibrate_internal_parameters(automatic=True)` are called only in `PaperBroker.execute_sell` (`paper.py:450-457`). `src/trading/live.py` contains no reference to `trade_learner` (grep). Live exits produce no `TradeLesson` and no automatic calibration; only manual calibration via `routes.py:818` and `telegram/bot.py:1342` runs.
- `get_learning_context_for_agents` injects the 4 newest lessons across all tickers and both modes, plus current params, into the committee prompt (`learner.py:603-641`; `committee.py:116`). It overlaps with the cold-start lessons, hence `include_lessons=False` at `committee.py:63`.

**review_tracker (`src/data/review_tracker.py`):**
- It archives session digests (`review_tracker.py:152-178`). It compares each past-week pick's recommended price to the live price, with TARGET/STOP flags (`review_tracker.py:205-281`). It computes a "win rate" of picks that are up, ignoring direction for SELL or HOLD picks (`review_tracker.py:259-286`).
- Consumers are Telegram text and the web route only (`jobs.py:392`; `routes.py:792`; `telegram/bot.py:322, 373, 1383`). It never feeds memory, the learner or thresholds.

**ShadowPortfolios (`src/trading/shadow.py`):**
- `follow_report` gets every verdict (`jobs.py:380-382, 650`) and simulates the presets aggressive, momentum and baseline (`shadow.py:90-123`) with per-trigger `consensus_score`, `followed_main` and `exit_reason` (`models.py:223-270`).
- It only reads `SystemConfig` (`shadow.py:234-241, 322-326`) and never writes it. `get_comparison` is display-only (`shadow.py:719`).

### Inferences
- There are three parallel "learning" stores: memory atoms, `TradeLesson` and SystemConfig, and shadow/review data. Only SystemConfig changes numbers. The most decision-relevant outcome data sits in shadow and review: score→forward return over all verdicts, including untaken ones, which avoids selection bias. That data is unused for calibration.
- Calibrating on win rate with 20-trade windows and LLM-proposed jumps is noisy and can overfit. The `backtest-discipline` concerns apply.

### Gaps
- No production data to judge how often calibration fired or what values it set.

---

## Q6. Does the code graph contribute to trading decisions? Token and latency cost per evaluation?

### Takeaway
The code graph contributes nothing to trading decisions. It serves coding agents only, through routes. The per-evaluation memory cost is about 2,000-2,400 prompt characters plus about 8 SQLite/SQLAlchemy queries and up to 3 broker calls, small next to the 4 LLM calls of up to 90 s each.

### Cited Findings
- `search_codegraph` is called only from `routes.py:271-274`. `refresh_codegraph` is called from `prune_and_compact` (`store.py:710`) and from first-time seeding when nodes are stale (`store.py:950-951`). The cold-start loadout never includes the code graph (`cold_start.py:39-46, 136-148`).
- The code-graph specs are 15 hard-coded symbols with AST-resolved line ranges (`store.py:728-835`).
- Per-evaluation memory I/O:
  - `init_db` early return (`store.py:94-96`).
  - 6 gathered reads (`cold_start.py:39-46`), including unbounded `list_scenarios` and `list_wiki_pages`, plus a possible extra skills query (`cold_start.py:48-50`).
  - The recap Trade query (`cold_start.py:255-260`).
  - Each store call opens a fresh aiosqlite connection with 3 PRAGMAs (`store.py:85-92`).
  - Broker calls cached for 30 s (`cold_start.py:177-186`).
  - Background writes: 1 L0 insert plus about 1-3 atom upserts plus 1 scenario upsert per evaluation (`client.py:55-74`; `extractor.py:298-335`).
- Prompt size: `char_budget=2400` (`committee.py:63`). [sim] loadout = 2,023 chars. Learning context is about 4 header lines plus 4 lessons (`learner.py:627-639`).

### Inferences
- At roughly 4 chars per token, memory adds about 500-600 input tokens per committee LLM call, plus about 150-400 for the learning context. The latency cost is negligible next to the LLM. The real cost is opportunity: the token budget is spent on boilerplate rather than outcome statistics.

### Gaps
- Latency was not measured on a production-size DB.

---

## Q7. Dead code, silent failures, data quality, lookahead risks, and test coverage

### Takeaway
Memory failures are silent by design: broad excepts, debug-level logs and unreferenced background tasks. There are several data-quality defects and one misattribution risk in the learner. Tests cover memory internals well but nothing proves that memory changes a decision.

### Cited Findings
**Silent failures:**
- `committee.py:66-67` logs at debug level only. `committee.py:72-73` and `committee.py:90-91` swallow errors with `except Exception: pass`, silently reverting to default weights and thresholds. `committee.py:244-245` is `except: pass` around `create_task`.
- Fire-and-forget without a stored reference or error callback: `client.py:65-74`, `committee.py:237`, `paper.py:314, 434, 707`, `live.py:737, 1145, 1309`, `proxy.py:84-92`. The surrounding try/except catches only scheduling errors, not task failures (fact). Python can garbage-collect unreferenced tasks mid-flight (Python docs; inference that it applies here).
- `cold_start._build_history_recap` swallows DB errors at debug level (`cold_start.py:306-307`).

**Dead or unused code:**
- Extractor LLM branch (`extractor.py:266-296`).
- `AgentMemoryClient.recall/capture` (`client.py:145-159`).
- `wiki_highlights` in the prompt path (`cold_start.py:120-123`).
- Seeded wiki pages and skills are mostly static boilerplate (`store.py:859-948`).
- Default disagreement: `min_buy_score = 68` (`committee.py:77`) versus the `cfgs.get(..., 70)` fallback (`committee.py:89`).

**Data quality:**
- Native price labelled € (`client.py:125`; `extractor.py:121-122`).
- Phantom "latest" trigger atoms that never expire ([sim]).
- Verdict and trade atoms overwritten per ticker.
- Default score "75" (`extractor.py:204`) and the learner's report fallback of 75 (`learner.py:70`).
- Paper and live lessons mixed in prompts (`learner.py:610-613`; `cold_start.py:295-302`; `TradeLesson` has no `is_live`, `models.py:189-204`).
- The cold-start 30-day recap mixes paper and live trades (`cold_start.py:256-260`, no `is_live` filter).
- Proxy writes share the trading namespace (`proxy.py:46-47`).

**Lookahead and misattribution:**
- The post-mortem uses the newest report by ticker, not the entry report (`learner.py:66-69`).
- Memory has no as-of filtering: cold start reads current state and `updated_at` order. Any backtest or replay of `evaluate_ticker` would leak future atoms, lessons and SystemConfig into past decisions (inference from `cold_start.py:39-46`, `committee.py:59-91`).
- Shadow presets freeze the main parameters at start (`shadow.py:252-266`), which is good practice and limits shadow-side lookahead.

**Tests:**
- `tests/test_agent_memory.py` (18 tests, `test_agent_memory.py:21-648`) covers CRUD, the cold-start budget and scenario matching, the recap 30-day window and ticker filter, extractor stop-words, dedup and HOLD-trigger skip, L1 ranking, prune, and code-graph drift.
- `tests/test_learner.py` covers post-mortem, calibration, the learning-context smoke test (`test_learner.py:122-128`) and idle decay.
- No test asserts that `memory_context` reaches the committee prompt, that live sells trigger post-mortems, that outcomes link to reports, or that memory changes any verdict. A grep of `tests/` for `full_prompt_context`, `memory_context`, `log_interaction` or `record_trade_event` outside `test_agent_memory.py` finds nothing.

### Inferences
- The suite would stay green if memory were deleted from the decision path entirely.

### Gaps
- Task garbage-collection loss was not observed empirically.

---

## Q8. Gaps ranked by likely impact on trading P&L

### Takeaway
The biggest limits on memory's P&L effect are structural. Memory acts only through an often-failing LLM prompt; it never stores verdict→outcome pairs; and the only numeric learning loop is cut off in live mode and driven by noisy win-rate statistics.

### Cited Findings (ranked; each item restates evidence above)
1. **Memory never touches numeric decisions, and the rules path ignores it.** It reaches only the committee LLM prompt (`committee.py:114, 150-152`). On the rules path (`committee.py:173-232`) it has zero effect. Entry, stop and size are deterministic and memory-free (`risk_manager.py:20-38`). Memory could instead adjust the score threshold, size or veto per setup from outcome statistics.
2. **No decision→outcome linkage.** No `report_id` on `OrderTrigger`, `Position` or `Trade` (`models.py:25-94, 125-187`). The SELL trade carries no score (`paper.py:382-396`). The initial stop and R-multiple are not preserved. Memory overwrites per ticker (`extractor.py:208-246`). Without this, no outcome-weighted retrieval or score calibration is possible.
3. **Live mode never learns automatically.** Post-mortem and calibration are wired only in `paper.py:450-457`; `live.py` has none. The learner filters by mode (`learner.py:216-236`), so live calibration happens only on manual trigger.
4. **Retrieval is ticker LIKE plus recency, with no setup, regime or outcome weighting** (`store.py:297-364`; `committee.py:62-64`). Past lessons from similar setups on other tickers are invisible except the 4 newest global lessons (`learner.py:610-613`).
5. **The best outcome datasets are unused.** Shadow portfolios (score→P&L on every verdict, including skipped ones) and review_tracker (pick→forward return) are display-only (`shadow.py:719`; `review_tracker.py:205-349`; consumers `jobs.py:392`, `telegram/bot.py:322`).
6. **The prompt budget goes to boilerplate and self-anchoring.** Static persona and rules take about 45% of the loadout ([sim]; `cold_start.py:57-67`). The ticker's own previous verdict is re-fed as a "fact" (`extractor.py:208-229`), a self-confirmation risk. Tail truncation cuts the ticker-specific sections first (`cold_start.py:151-152`).
7. **Learner misattribution and noise.** It uses the newest report rather than the entry report (`learner.py:66-69`). Calibration runs on win rate and streak over 20 trades with LLM-chosen jumps (`learner.py:247-401`), and the rules fallback always resets breakeven (`learner.py:431`).
8. **Data-quality errors in atoms.** € mislabels, phantom "latest" triggers, a default score of 75, paper/live mixing, and proxy namespace sharing (citations in Q7). These mislead the LLM on the path where memory matters.
9. **Operational issues.** L0 grows without scheduled pruning (`routes.py:277-279` is the only caller). Background tasks are unreferenced and unlogged (Q7). Cold start runs, with broker calls, even when no LLM will read it (`committee.py:59-67` vs `150`).
10. **The code graph is irrelevant to P&L.** It adds startup and compact cost only (`store.py:710, 950-951`).

### Inferences
- Fixing items 2 and 3 is a prerequisite for any measurable gain. Items 1, 4 and 5 are where the gains would come from. Items 6-8 matter only while memory stays a prompt-only influence.
- Validate any change against the shadow "baseline" control (`shadow.py:113-121`) rather than in-sample win rate.

### Gaps
- No production data was available to quantify the share of LLM-path decisions, the atom pollution or the calibration history. A follow-up should query `AnalysisReport.raw_signals->score_source`, `TradeLesson`, `SystemConfig` and `memory_l1_atoms` on the owner's machine.
