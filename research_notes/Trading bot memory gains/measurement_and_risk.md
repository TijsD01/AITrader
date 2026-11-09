# Measuring whether memory/learning changes raise gains, and the risks they carry

Repo state read at commit `a25150f`. All repo paths are relative to `/home/user/AITrader`. No `data/` directory exists in this checkout, so there are no real trade counts. All sample-size numbers below are planning figures, not measurements.

## Q1. Which backtest-discipline rules apply to a memory change?

### Takeaway
The skill's rules map directly onto a memory experiment. LLM verdicts cannot be replayed on past data, so committee-driven changes must be forward-tested with shadow portfolios. Results under ~30 trades are noise. Tuning and reporting must use separate periods. Every report must state its period, costs and remaining biases.

### Cited Findings
- No look-ahead: a decision at bar `t` may use only data available at the close of `t-1` (`.claude/skills/backtest-discipline/SKILL.md:12`).
- Survivorship bias: `config/default_universe.py` is today's universe and overstates past returns (`SKILL.md:14`).
- Charge fees with the real model (`calculate_broker_fee` / `calculate_round_trip_fee`). Skip trades that `evaluate_trade_economic_viability` rejects (`SKILL.md:20`).
- Respect `WHOLE_SHARES_ONLY`, add slippage, fill gapped stops at the next price, and respect `MAX_POSITIONS` / `MAX_POSITION_PCT` / cash (`SKILL.md:21-23`).
- Compare against buy-and-hold of a benchmark after costs. Report total return, CAGR, max DD, Sharpe/Sortino, trade count, win rate, average win/loss, exposure % and fees (`SKILL.md:27-28`).
- Tune on the first part of the period and report on an untouched last part (walk-forward or hold-out). "A parameter tuned on the full period proves nothing" (`SKILL.md:29`).
- "Too few trades (under ~30) means the result is noise. Say so." (`SKILL.md:30`).
- "LLM committee verdicts cannot be replayed honestly on past data (the model has seen the future)". Backtest only the rule-based parts, or use shadow portfolios for forward tests of committee-driven rules (`SKILL.md:31`).
- State period, universe, costs, slippage and the remaining biases. "A result without these is not a result." (`SKILL.md:35`).

### Inferences
- Memory only changes the LLM verdict, so a historical backtest of a memory change is ruled out by `SKILL.md:31`. The only honest route is a forward (live-time, paper) A/B.
- The ~30-trade floor in `SKILL.md:30` is a noise floor, not a power target. Q3 shows a +0.2R edge needs roughly 150–900 trades, depending on the design.

### Gaps
- The skill gives no rule on how many variants may be tried (multiple testing). Q4 fills this from outside sources.

## Q2. How do ShadowPortfolios and TradeLearner work, and can a shadow run "memory on" vs "memory off"?

### Takeaway
No, not with the current code. Every shadow consumes the same committee report, and that report was produced with memory on. Shadows can vary only entry/exit/sizing rules applied after the verdict. A memory ablation needs a second `evaluate_ticker` call with memory disabled, routed to a dedicated shadow. That arm must not write back into memory.

### Cited Findings: where memory enters
- `InvestmentCommittee.evaluate_ticker` loads the cold-start loadout `agent_memory_client.get_cold_start_loadout(query=ticker, char_budget=2400, include_lessons=False)` into `memory_context` (`src/agents/committee.py:59-67`).
- It then loads `trade_learner.get_learning_context_for_agents()` into `learning_context` (`src/agents/committee.py:69-73`).
- Both strings go only into the Portfolio Manager LLM `user_prompt` (`src/agents/committee.py:111-116`). The technical, fundamental, sentiment and risk agents run without memory (`committee.py:94-99`). A grep for `memory|learning_context|trade_learner` in `technical.py`, `fundamental.py`, `sentiment.py`, `risk_manager.py` and `base.py` finds nothing.
- If the LLM is not active or its output is invalid, the verdict comes from a weighted rules formula (`committee.py:174-196`, `score_source = "rules"`). That formula ignores memory text but reads the learner-tuned `WEIGHT_*` and `MIN_CONSENSUS_SCORE` from `SystemConfig` (`committee.py:75-91`).
- Every verdict is written back to agent memory with `log_interaction(..., trigger_extraction=True)` (`committee.py:234-243`).
- The cold-start loadout re-injects the L2 "scenario" whose key matches the ticker (`evaluation-<ticker>`), i.e. the bot's own previous deliberation on that ticker (`src/memory/cold_start.py:71-101`). It also injects up to 5 L1 atoms found by ticker query (`cold_start.py:43`, `105-111`) and a "what had happened" recap (`cold_start.py:53-55`).

### Cited Findings: how shadows work
- Each shadow is a fully simulated portfolio. It "sees the same committee verdicts as the main bot" and applies its own entry, exit and sizing rules. Shadows "never feed the learner, agent memory or Telegram" (`src/trading/shadow.py:1-14`).
- `ShadowStrategy` fields cover score offset, `accept_hold`, `ignore_risk_veto`, entry mode, pullback/stop scale, R:R, trailing stops, max positions, hold days, risk %, ATR trail and regime filter (`shadow.py:61-81`). No field selects the report, the prompt or memory.
- Presets are `aggressive`, `momentum` and `baseline`. `baseline` is "a control group" for simulation effects (`shadow.py:89-122`). Custom strategies come from `SHADOW_CUSTOM_STRATEGIES` JSON (`shadow.py:127-157`). The default is `SHADOW_STRATEGIES="aggressive"` (`config/settings.py:89-92`).
- Main-bot parameters are frozen per shadow at its start (`frozen_params`, `shadow.py:234-249`, `275-302`). The learner therefore does not move a running shadow's own thresholds.
- `follow_report(report, data, followed_main)` loops all strategies over one shared `report`: same `action`, `risk.approved` and `consensus_score` (`shadow.py:336-406`).
- Call sites pass the single report from `evaluate_ticker`: `src/scheduler/jobs.py:380-382`, `jobs.py:650`, `src/telegram/bot.py:1060`, `src/web/routes.py:266`.
- Triggers expire through `is_trigger_expired` (`shadow.py:432-434`) and fill only while the market is open (`shadow.py:435-437`).
- `record_snapshots` stores about hourly (`SNAPSHOT_INTERVAL` = 55 min) total values per shadow and for `MAIN`, with deposit-neutral `return_pct` (`shadow.py:48`, `51-54`, `642-685`).
- `_trade_stats` reports only closed-trade count, wins, losses, win rate, realized PnL and average EUR per trade (`shadow.py:695-706`). It has no R-multiple, no dispersion and no confidence interval.

### Cited Findings: how the learner works
- `conduct_trade_post_mortem` runs on a closed SELL. It builds a rule-based what-went-well/wrong lesson, optionally overwrites it with an LLM "performance coach" output, and stores a `TradeLesson` (`src/trading/learner.py:21-204`, LLM enrichment at `153-175`).
- Post-mortem and automatic calibration are called from `PaperBroker` after each sell (`src/trading/paper.py:447-457`). No call to `trade_learner` exists in `src/trading/live.py`.
- `calibrate_internal_parameters` waits for `LEARNER_MIN_TRADES` (=20, `config/settings.py:126`) closed trades since the last calibration. It uses the last 20 closed trades (`learner.py:225-246`).
- An LLM calibrator may move up to nine parameters at once within clamps: MIN_CONSENSUS_SCORE 68–85, TRIGGER_MIN_SCORE 72–88, ATR mult 1.75–2.75, TP R:R 1.5–3.0, breakeven 1.5–5%, and four weights (`learner.py:360-370`).
- The rule fallback raises thresholds on `consecutive_losses >= 2 or win_rate < 0.40` (`learner.py:415-418`). It lowers them on win rate ≥ 70% (`learner.py:419-422`). It widens the ATR stop after ≥ 2 recent stop-outs (`learner.py:425-428`). It shifts weight from technical to risk after ≥ 3 straight losses (`learner.py:434-437`).
- `get_learning_context_for_agents` injects the 4 newest `TradeLesson` rows, with ticker, outcome and PnL %, plus the adapted parameters into every committee prompt (`learner.py:603-641`). The query has no paper/live filter (`learner.py:609-613`).
- `decay_idle_thresholds` lowers raised score thresholds 1 point/day after `LEARNER_IDLE_DECAY_DAYS` (=7) without a buy (`learner.py:516-601`, `config/settings.py:127`).

### Inferences
- **What blocks an ablation today:** shadows differ only after the verdict. The memory effect lives inside the verdict. So any shadow-vs-main gap today mixes rule differences with simulation effects. It says nothing about memory.
- **"Frozen" is incomplete:** shadows freeze thresholds, but the verdicts they consume still drift with memory and learner lessons. All arms share that drift, so it cancels in a shadow-vs-shadow comparison. It does not cancel in a before/after comparison of the main bot.
- **Minimal ablation design in this codebase:**
  1. Add an optional flag to `evaluate_ticker`, for example `use_memory: bool = True` (or `memory_mode="off"|"loadout_only"|"lessons_only"`). It skips the loadout and/or learning context (`committee.py:59-73`) and skips `log_interaction` (`committee.py:234-243`) for the off arm. Skipping the write prevents the counterfactual arm from contaminating memory and the L2 scenario of that ticker.
  2. In `run_market_session` / the opportunity scanner, call `evaluate_ticker` a second time for the same `data` snapshot with memory off. Do this only when `score_source == "llm"`; under the rules fallback both arms are the same.
  3. Add a `report_variant` field to `ShadowStrategy`, and pass a dict of reports to `follow_report`. Each shadow then picks its variant. The pair should be `baseline` (memory on) vs `baseline_nomem` (memory off), so the only difference is the verdict.
  4. Persist both verdicts per candidate (ticker, timestamp, price, action, score, levels, arm) for decision-level labels (see Q6).
- **Cost and limits:** the extra call doubles LLM calls per candidate, under a 300 s timeout per evaluation (`jobs.py:296-298`). LLM sampling is nondeterministic, so some arm disagreement is pure noise. Run a memory-on vs memory-on "A/A" pair for a while to measure that noise floor. Shadows still share cash and slot competition effects (`max_positions`), so portfolio-level differences carry path dependence. Decision-level metrics avoid it (Q3, Q6).
- The main bot learns only from paper sells (`paper.py:447-457`). Lessons are pooled across paper and live (`learner.py:609-613`). An ablation must state which trades fed the memory.

### Gaps
- Exact shadow fill/fee code (`shadow.py:475-555`) was not reviewed line by line for slippage realism.
- No production DB was available, so the actual trade rate, and with it the experiment duration, is unknown.

## Q3. How many closed trades/decisions are needed to detect a given improvement?

### Takeaway
Detecting +0.2R per trade between two independent arms needs about 390–880 closed trades per arm (80% power, α=0.05 two-sided, per-trade SD 1.0–1.5R). Detecting +5pp win rate needs about 1,550 per arm. A paired design on the same candidates cuts this 2–5×. Labeling every decision instead of only filled trades multiplies the sample rate. At realistic trade rates, a portfolio-level verdict on a small memory change takes years. Measure at the decision level.

### Cited Findings
- Standard two-proportion sample size at α=0.05 two-sided and 80% power: a 60% vs 45% example needs about 171 per group. Normal-approximation formula: n = [z₁₋α/₂·√(2p̄q̄) + z₁₋β·√(p₁q₁+p₂q₂)]² / (p₁−p₂)² — [MedCalc](https://www.medcalc.org/manual/sample-size-comparison-of-proportions.php), [StatPages](https://statpages.info/proppowr.html), [CUHK Casagrande-Pike-Smith calculator](https://www2.ccrb.cuhk.edu.hk/stat/proportion/Casagrande.htm).
- MinTRL = 1 + (1 − γ₃·SR + ((γ₄−1)/4)·SR²)·(z₁₋α/(SR − SR*))². SR is non-annualised and in the periodicity of the observations — [Bailey & López de Prado, "The Sharpe Ratio Efficient Frontier", J. Risk 2012](https://www.risk.net/journal-risk/2223785/sharpe-ratio-efficient-frontier); [PerformanceAnalytics MinTrackRecord docs](https://rdrr.io/cran/PerformanceAnalytics/man/MinTrackRecord.html) (refSR "must be non-annualized"). The formula was transcribed by the fetch tool; check it against the paper.
- "A longer track record will be required the smaller SR is, or the more negatively skewed returns are" — [López de Prado slides](https://newyork.qwafafew.org/wp-content/uploads/sites/4/2017/01/Lopez_de_Prado_Sharpe.pdf) (via search snippet).

### Inferences (own calculations; z₀.₉₇₅=1.96, z₀.₈=0.84, (z_a+z_b)² = 7.85)
- **Per-trade SD in R:** with a −1R stop and +2R target (TP R:R ~2.1 default, `learner.py:625`), outcomes are roughly binary. At 40% win, mean = +0.2R and SD ≈ 1.47R. At 50% win, SD ≈ 1.5R. Breakeven/trailing exits shrink SD toward ~1.0–1.25R.
- **Mean-R difference, two independent arms** (n per arm = 2·7.85·σ²/δ²):

  | per-trade SD | δ = +0.1R | δ = +0.2R | δ = +0.3R |
  |---|---|---|---|
  | 1.0R | 1,570 | 392 | 174 |
  | 1.25R | 2,453 | 613 | 273 |
  | 1.5R | 3,532 | 883 | 392 |

- **Paired design** (same candidate scored by both arms; n = 2(1−ρ)·7.85·σ²/δ²): σ=1.5, δ=0.2 needs 441 pairs at ρ=0.5 and 177 pairs at ρ=0.8. Here ρ is the correlation of the two arms' outcomes on the same candidate. It is high when both arms buy the same setups and differ only on a minority.
- **Win rate +5pp:** 50→55% needs 1,565 per arm and 40→45% needs 1,533. A +10pp gain (45→55%) needs about 391. Win rate is a weak metric for a memory change: it ignores payoff size, and the learner already defines "win" as > +0.3% (`learner.py:248-249`).
- **One-arm proof that the edge is > 0 (MinTRL, 95% one-sided, normal returns):** a per-trade mean of +0.2R at σ=1.5R (per-trade SR 0.133) needs about 155 trades. At σ=1.0R it needs about 70. At +0.1R/1.5R it needs about 611. Positive skew (trailing-stop winners) lowers these slightly, to about 144 for the first case.
- **Practical consequence:** sessions evaluate at most 4 candidates each (`jobs.py:280-283`), and only those clearing `TRIGGER_MIN_SCORE` that then fill become trades. Closed trades will likely number in the tens per month at most, which is unverified with no DB. So per-trade portfolio A/B tests of a memory change will not reach power for many months. Use decision-level labels: each evaluated candidate scored by a fixed rule-based outcome (Q6). They yield several times more observations, and memory-on/off disagreements are where the information is.
- **Sequential monitoring:** checking the A/B gap every week and stopping at the first p<0.05 inflates false positives. Fix the sample size in advance, or use an alpha-spending/sequential test. This is a standard trial-design point; I did not fetch a source for it.

### Gaps
- No actual per-trade R distribution from this bot (no DB). Estimate σ from the first 30–50 shadow trades and recompute.
- I did not source a sequential-testing reference (e.g. O'Brien-Fleming, mSPRT). It is flagged here, not cited.

## Q4. What does external practice say: walk-forward, deflated Sharpe, multiple testing, MinTRL, LLM leakage, regime shift?

### Takeaway
Count every memory/learner variant tried. Deflate the best result by that count (DSR) or demand t ≥ 3. Require a minimum track record before trusting a Sharpe. Treat hold-out tests with suspicion (PBO). Remember that LLMs carry memorised future knowledge, so only forward tests are valid. Name-anonymisation also helps against "distraction".

### Cited Findings
- Deflated Sharpe Ratio corrects "two leading sources of performance inflation: Selection bias under multiple testing and non-Normally distributed returns" — [Bailey & López de Prado 2014, DSR (PDF)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf); [SSRN 2460551](https://papers.ssrn.com/abstract=2460551).
- "The most important piece of information missing from virtually all backtests ... is the number of trials attempted ... a backtest where the researcher has not controlled for the extent of the search involved in his or her finding is worthless" — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
- DSR "is a PSR where the rejection threshold is adjusted to reflect the multiplicity of trials". The expected maximum SR across trials "is greater than zero, even if the true SR is zero" and grows with the number of independent trials N and the variance of the SRs tested. DSR uses five extra inputs: skewness, kurtosis, sample length T, the variance of the tested SRs, and N — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
- The paper's example: an annualised SR of 2.5 over 5 years of daily data is rejected at 95% confidence after N=88 trials once non-normal returns are accounted for — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
- "Multiple testing exercises should be carefully planned in advance ... Investment theory, not computational power, should motivate what experiments are worth conducting." "Every additional trial irremediably increases the probability of a false positive." The paper proposes the 1/e (secretary-problem) stopping rule — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).
- Expected-max SR approximation: E[max SR] ≈ √V·[(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))], with γ ≈ 0.5772 — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf). The formula symbols did not survive PDF text extraction; this form comes from the fetch tool's rendering and should be checked against the paper.
- Probability of Backtest Overfitting (PBO), estimated via combinatorially symmetric cross-validation (CSCV). The authors argue "conventional hold-out methods tend to be unreliable and inaccurate" for investment backtests — [Bailey, Borwein, López de Prado, Zhu, J. Computational Finance 2017](https://www.risk.net/journal-of-computational-finance/2471206/the-probability-of-backtest-overfitting); [SSRN 2326253](https://papers.ssrn.com/abstract=2326253).
- Harvey, Liu & Zhu: a new factor should clear t > 3.0 rather than 2.0, given the number of tests run in the literature. They argue most claimed findings in financial economics are likely false — [NBER w20592](https://nber.org/papers/w20592); [RFS 2016 via EconPapers](https://econpapers.repec.org/RePEc:oup:rfinst:v:29:y:2016:i:1:p:5-68.).
- LLM look-ahead bias: an LLM "may have specific knowledge of the stock returns that followed a news article." The "distraction effect" is that "general knowledge of the companies named interferes with the measurement of a text's sentiment." In-sample, anonymised headlines outperformed, so distraction mattered more than look-ahead there. Out-of-sample, look-ahead is not a concern but distraction may persist. Anonymisation is the proposed remedy — [Glasserman & Lin, arXiv 2309.17322](https://arxiv.org/abs/2309.17322).
- A "Lookahead Propensity" statistic, based on date-only recall queries, drops to near zero just after the training cutoff. Predictive power is amplified on high-propensity firm-dates — [Gao, Jiang, Yan, arXiv 2512.23847](https://huggingface.co/papers/2512.23847) (search snippet only).
- Models trained on data covering the outcome period earn higher Sharpe, which the authors attribute to contamination — [DatedGPT, arXiv 2603.11838](https://arxiv.org/pdf/2603.11838) (search snippet only). A ChronoBERT/ChronoGPT study finds the effect "relatively modest" and model/application dependent — [AEA 2026 program](https://www.aeaweb.org/conference/2026/program/paper/BrRDGQHe). The two sources disagree on magnitude.
- Look-Ahead-Bench evaluates end-to-end agentic LLM trading workflows for look-ahead and alpha decay — [arXiv 2601.13770](https://arxiv.org/pdf/2601.13770). A 2025 re-evaluation reports that LLM advantages often shrink under broader and longer tests with survivorship/look-ahead/data-snooping controls — [arXiv 2505.07078](https://arxiv.org/html/2505.07078v5). Memory/reflection agent designs like FinMem (layered memory, immediate and extended reflection) are described in [InvestorBench, arXiv 2412.18174](https://arxiv.org/pdf/2412.18174). All are from search snippets, not full reads.

### Inferences
- **Walk-forward here = forward paper time.** Memory and the learner cannot be replayed (`SKILL.md:31`). "Walk-forward" therefore means: freeze the memory config, run it forward on shadows for a pre-set number of decisions, then decide. Any memory tweak after peeking starts a new trial, and the trial count N grows.
- **Keep a trial ledger.** Every memory variant, prompt change and learner rule change counts toward N. Every automatic learner calibration does too: it is a data-driven parameter selection on the last 20 trades (`learner.py:234-246`). Compute DSR with that N, or apply a Bonferroni/Harvey-Liu-Zhu style t ≥ 3 hurdle before promoting a change.
- **Pretraining leakage in forward tests:** pure forward decisions cannot know the future. The LLM's prior knowledge of big-cap names (the "distraction effect") still biases verdicts identically in both arms, so it cancels in a memory on/off ablation. It does not cancel against a rules-only baseline.
- **Regime shift:** both arms of a concurrent A/B face the same market, so the regime cancels in the difference. Lessons learned in one regime can hurt in the next, though. Split results by the existing regime flag (`_regime_allows`, `shadow.py:269-273`; index-above-SMA). Expire or down-weight memory atoms and lessons older than one regime.

### Gaps
- I did not fetch a primary source on walk-forward analysis itself. The skill's rule (`SKILL.md:29`) and the PBO paper's critique of hold-out cover it here.
- The DatedGPT, Lookahead Propensity, KTD-Fin and Look-Ahead-Bench claims come from search snippets, not full-text reads.
- No literature found that isolates the effect of a memory/reflection module on LLM trading-agent returns with proper multiple-testing control.

## Q5. What feedback-loop risks do memory and learner changes carry?

### Takeaway
The current loop has short windows and recency-weighted lessons. It reacts to streaks as short as 2 trades, and it re-injects the bot's own prior verdict on the same ticker. It can also re-tune up to 9 parameters per calibration. All of these favour self-reinforcement and overfitting to noise. Memory sees outcomes only for trades that were bought, so its "experience" is a selected sample.

### Cited Findings (repo)
- Threshold tightening fires on 2 consecutive losses or win rate < 40% over the last 20 trades (`learner.py:415-418`).
- Loosening fires on ≥ 70% win rate (`learner.py:419-422`).
- The LLM calibrator can set nine parameters at once within wide clamps (`learner.py:360-370`). Its prompt tells it to raise thresholds "under a losing streak" (`learner.py:339-340`).
- Only the 4 newest lessons, with ticker and PnL %, are injected into every committee prompt (`learner.py:609-613`, `634-637`).
- The committee prompt asks the PM to factor in "where previous trades went wrong or succeeded" (`committee.py:103-108`).
- The committee's own previous deliberation on the same ticker is re-injected as the L2 scenario (`cold_start.py:71-101`). Every new verdict is logged back with extraction (`committee.py:234-243`).
- Post-mortems exist only for closed SELL trades (`learner.py:30-33`). Untraded candidates and expired triggers produce no lesson.
- The post-mortem picks the newest `Position` for the ticker, not the one tied to the trade (`learner.py:47-50`). This can attach the wrong entry and high-water mark if a ticker was traded twice.
- Selective-labels problem: when a decision-maker's own choices decide which cases get an outcome label, evaluation and learning from those labels are biased. Lakkaraju et al. propose "contraction" for evaluation under unobserved confounders — [Lakkaraju, Kleinberg, Leskovec et al., KDD 2017](https://www.cs.cornell.edu/home/kleinber/kdd17-selective.pdf).

### Inferences
- **Two-loss streaks are common noise.** At a 50% win rate, two losses in a row follow any given point 25% of the time. At 40%, it is 36%. The rule therefore toggles often. In the LLM path, each toggle is an untracked trial.
- **Self-reinforcement/anchoring.** Re-injecting "we said BUY/HOLD on X last time" pushes toward consistency with the earlier verdict rather than toward the new data. A few lucky wins on a ticker appear as "WIN on X +5%" lessons and can raise future scores on X. That is the hot-hand loop. Test it: compare verdict changes on repeat evaluations of the same ticker in memory-on vs memory-off arms.
- **One-sided memory.** Lessons come only from fills, and fills come only from high-score BUY verdicts, so memory never learns "this HOLD/rejection was a mistake". If losses get louder lessons (the LLM coach writes "what went wrong"), the loop drifts toward ever-tighter selectivity until `decay_idle_thresholds` pulls it back (`learner.py:516-601`). The two can oscillate.
- **Overfitting weights.** Weights affect only the rules fallback (`committee.py:174-178`), yet the learner tunes them on 20 trades. Nine free parameters on 20 noisy outcomes is classic overfitting.
- **Mitigations implementable here:**
  - Require ≥ N decisions *and* a CI that excludes zero before any memory/learner change is promoted.
  - Cap lesson influence: inject aggregated stats rather than the 4 latest anecdotes, and drop ticker-specific outcome numbers from the prompt.
  - Exclude the same-ticker L2 scenario from the prompt in one ablation arm.
  - Log every calibration as a trial with before/after params (`SystemConfig` rows already store `LEARNER_*` keys, `learner.py:470-481`).
  - Keep the `baseline` shadow frozen as a long-run control (`shadow.py:115-121`).

### Gaps
- No empirical measure in this repo of how often calibrations flip direction (no DB).

## Q6. Does the repo track counterfactual outcomes of skipped or expired ideas?

### Takeaway
Only weakly. `ReviewTracker.compare_past_week_reviews` compares last week's digest picks with the current price, unpersisted and with no barriers or fees. Expired, cancelled and HOLD/rejected decisions get no outcome label. Every decision is already persisted with its price, levels and full report in `AnalysisReport` (session path), so free labels can be computed after the fact.

### Cited Findings
- Paper triggers past `TRIGGER_EXPIRY_DAYS` (=7, `config/settings.py:84`) are set to `EXPIRED` and skipped. No price path after expiry is recorded (`paper.py:896-899`; check in `risk.py:353-362`).
- Live does the same (`live.py:1680-1682`, `1818-1844`), and so do shadows (`shadow.py:432-434`).
- Triggers cancelled because "the price has already run through" are marked `CANCELLED` without an outcome (`paper.py:918-925`; shadows `shadow.py:449-452`).
- `OrderTrigger` stores `target_price`, `current_price_at_creation`, `stop_loss`, `take_profit`, `consensus_score`, `status` and `created_at` (`src/db/models.py:168-187`). That is enough to replay a triple-barrier outcome later from price history.
- `AnalysisReport` stores action, score, target, stop, all summaries and `raw_signals` (the full report incl. price) for every evaluated candidate (`src/db/models.py:125-145`). It is written in the session path (`jobs.py:302-316`), for HOLD/SELL verdicts too.
- `ReviewTracker.compare_past_week_reviews` takes `MarketDigest.top_picks` from the past 7 days, dedups by ticker and fetches a live quote. It computes `pnl_pct` vs the recommended price and flags TARGET/STOP only if the *current* price is beyond them. It returns a win rate (`src/data/review_tracker.py:205-286`).
- It is point-in-time: no path, no intraday barrier hits, no fees, nothing persisted, and only recommended picks.
- A grep for `counterfactual|forward_return|hindsight` in `src/` finds nothing relevant.
- The Telegram path notes when a score "would have armed a trigger" but was held back by the verdict, and does not record its outcome (`src/telegram/bot.py:1055-1057`).
- Selective-labels literature: outcomes exist only for cases the decision-maker acted on, and evaluating alternative policies needs explicit handling — [Lakkaraju et al., KDD 2017](https://www.cs.cornell.edu/home/kleinber/kdd17-selective.pdf); [Learning with selectively labeled data, arXiv 2306.07566](https://arxiv.org/html/2306.07566v4).

### Inferences
- **Free labels:** a daily job could label every `AnalysisReport` and every `EXPIRED`/`CANCELLED` `OrderTrigger`/`ShadowTrigger` older than H days with a triple-barrier outcome:
  - first hit of its own stop (−1R) or target (+kR) within H trading days, else the return at H;
  - the entry is the trigger level if touched, else no-fill with "missed move" recorded separately;
  - store it in a new table, plus a migration entry in `_run_migrations` if columns are added to existing tables, per CLAUDE.md.

  Use daily yfinance bars with the fees from `fees.py`. Each label uses only data after `created_at`, which removes look-ahead.
- **Uses of the labels:**
  1. A decision-level metric for the memory A/B: average R of BUY verdicts by arm, plus calibration of score vs realized R. It needs no fill and no portfolio path dependence, and it gives several times more observations than closed trades.
  2. Correcting one-sided memory: feed "missed winner"/"avoided loser" lessons for rejected or expired ideas into the learner, so it is not trained only on fills.
- **Off-policy caveat:** the bot's policy is deterministic (score ≥ threshold → arm). Inverse-propensity estimates of an alternative policy are therefore impossible for ideas the policy never takes. The paired memory-off re-evaluation in Q2 solves this by scoring the same candidates under both policies. An alternative is a small randomized exploration in shadows only (e.g. a shadow that takes a random 10% of HOLDs above score 55), with the propensity logged. Never do this in the main/live bot.
- **Rules to keep labels clean:** the label must be written only after its horizon ends. It must never be injected into memory before then. Labels must be split by regime and by `score_source` (`llm` vs `rules`).

### Gaps
- The opportunity-scanner (`jobs.py:~600-650`) and Telegram paths were not checked for whether they also persist `AnalysisReport`.
- `review_tracker.py:286-349` (portfolio evolution part) was not read in full.

## Q7. What concrete measurement and safety plan follows (synthesis)?

### Takeaway
Pre-register one memory change at a time. Ablate it on paired verdicts (memory on vs off on the same candidate snapshot). Score it on decision-level triple-barrier labels plus shadow portfolios. Promote it only after a pre-fixed sample with a deflated or t ≥ 3 test, and never let the off arm or unexpired labels write into memory.

### Cited Findings
- Forward-only testing for LLM-driven rules (`SKILL.md:31`). Count trials, because unreported trials make a result "worthless" — [DSR paper](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf). Use a t > 3 hurdle under multiple testing — [Harvey, Liu, Zhu](https://nber.org/papers/w20592). Hold-out tests are unreliable for strategy selection — [PBO paper](https://www.risk.net/journal-of-computational-finance/2471206/the-probability-of-backtest-overfitting).

### Inferences (plan, in order)
1. **Pre-register:** write down the hypothesis, primary metric (mean R per BUY decision at a fixed horizon, after fees), sample size (from Q3, e.g. ~180–440 paired disagreeing decisions for +0.2R), the stop date and the trial number in the ledger.
2. **A/A first:** run two memory-on arms for ~2 weeks to measure LLM verdict noise and shadow-simulation noise. The `baseline` shadow already exists as a simulation control (`shadow.py:115-121`).
3. **A/B on paired verdicts:**
   - Add `use_memory` to `evaluate_ticker`, with no `log_interaction` for the off arm.
   - Add `report_variant` to `ShadowStrategy`, with shadows `baseline` vs `baseline_nomem`.
   - Persist both verdicts with an arm tag.
   - Count only `score_source == "llm"` decisions.
4. **Label:** a daily job writes triple-barrier outcomes for all decisions/triggers past their horizon (Q6).
5. **Analyse:**
   - Primary: paired difference in mean R, with a bootstrap CI over decisions clustered by ticker/day.
   - Secondary: shadow portfolio return, max DD, Sharpe/Sortino, fees and benchmark (`SKILL.md:27-28`).
   - Report DSR with N = ledger count, and MinTRL for the shadow's per-trade SR.
   - Split by regime.
6. **Promotion gate:** the CI excludes 0, the t ≥ 3 or DSR > 0.95 hurdle is met, there are no regime-specific reversals, and drawdown is no worse than the control. Otherwise keep the old config. Paper-only until promoted. Never enable on live without the `live-money-safety` skill review.
7. **Ongoing guardrails for the learner:**
   - Raise effective `LEARNER_MIN_TRADES`, or require a statistical trigger rather than a 2-loss streak.
   - Restrict the LLM calibrator to one parameter per calibration.
   - Log each calibration as a trial.
   - Separate paper and live lessons (`learner.py:609-613`).
   - Inject aggregated, not anecdotal, lessons.
   - Add counterfactual (rejected/expired) lessons so memory is not trained only on fills.

### Gaps
- Exact implementation effort and LLM cost of doubling committee calls depend on provider latency. `agy` CLI calls are slow, per the timeout comment at `jobs.py:298`. Not measured.
