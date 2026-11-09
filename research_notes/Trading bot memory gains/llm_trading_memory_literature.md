# Memory and reflection in LLM trading agents: designs, reported gains, and evidence quality

Scope: published memory/reflection designs for LLM and multi-agent equity trading (FinMem, FinAgent, FinCon, TradingAgents, CryptoTrade, META, CLQT, EvolveTrade, Reflexion lineage), what they report, how it was tested, the critiques, and transferable patterns for a committee bot (technical/fundamental/sentiment/risk agents, EU+US equities, pullback/breakout triggers, SQLite memory injected as prompt text). Research date: 2026-11-09. Primary PDFs for FinMem, FinCon, FinAgent, TradingAgents, CryptoTrade and CLQT were read as full text. The TradingAgents repo was cloned at commit 9fa1196 (2026-11-04).

## Q1. What memory designs exist, what is the retrieval key, and how do outcomes feed back?

### Takeaway
Nearly every published design is a variant of Generative-Agents scoring (recency + relevance + importance), with text-embedding relevance and an LLM-written "reflection" as the outcome channel. The 2026 work shifts to two things: structured state keys (indicator vectors, regime/asset/event tags) and settled, benchmark-relative outcomes. The best-known open-source system (TradingAgents) dropped similarity retrieval entirely for a recency-ordered decision log with 5-day alpha.

### Cited Findings
**FinMem** (Yu et al., arXiv 2311.13743, v1 23 Nov 2023)
- **Layers.** Long-term memory has three layers keyed by data timeliness: shallow holds daily news insights, intermediate holds 10-Q insights, deep holds 10-K insights plus "extended reflections." — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **Score.** Each memory gets recency + relevancy + importance, each scaled to [0,1] and summed.
  - Recency = exp(−δ/Q), with Q = 14 / 90 / 365 days for shallow / intermediate / deep.
  - Relevancy = cosine similarity of ada-002 embeddings between the memory text and the prompt query (ticker + trader-character text).
  - Importance = v × α^δ. v is drawn from {40, 60, 80} with layer-dependent probabilities; α = 0.9 / 0.967 / 0.988.
  - Memories are purged when recency < 0.05 or importance < 5. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **Outcome feedback.** An access counter promotes memories that are "pivotal for investment success": +5 importance, recency reset to 1.0, and a move to a deeper layer. Top-K = 5 per layer is retrieved. Working-memory capacity is 3×K. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **Training uses next-day labels.** In "training," FinMem observes next-day price direction as ground-truth labels. It writes reflections that explain the link between the retrieved memories and those future moves, and these reflections persist into testing. In testing, it uses the trailing M-day cumulative return and "extended reflection" instead. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **FAISS store, ticker-filtered.** The FAISS store does "cosine-similarity-based semantic searches on specific tickers." Retrieval is therefore ticker-filtered first, then text-similar. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)

**FinAgent** (Zhang et al., KDD 2024, arXiv 2402.18485)
- **Design.** Multimodal input: news, prices, K-line and trading charts. "Diversified retrieval" issues separate short-, medium- and long-term query texts against a market-intelligence memory. Two reflection levels:
  - Low-level reflection links market intelligence to the price moves that followed.
  - High-level reflection reviews past decisions using the trading chart. — [FinAgent PDF](https://arxiv.org/pdf/2402.18485)
- **Auxiliary agents.** Rule-based "augmented tools" (MACD, KDJ&RSI, ZMR) and expert guidance are fed in as auxiliary agents. — [FinAgent PDF](https://arxiv.org/pdf/2402.18485)

**FinCon** (Yu et al., arXiv 2407.06567; NeurIPS 2024)
- **Design.** A manager agent with analyst agents. Within an episode, CVaR risk alerts make the manager risk-averse after a sudden PnL drop. — [FinCon PDF](https://arxiv.org/pdf/2407.06567)
- **CVRF (Conceptual Verbal Reinforcement).** Training replays the same window for several episodes. After each episode, the LLM compares the more profitable and less profitable episode and "conceptualizes" which analyst perspectives drove the difference. It then rewrites "investment beliefs" into the prompts, a "textual gradient descent." — [FinCon PDF](https://arxiv.org/pdf/2407.06567)
  - The "learning rate" is the overlap percentage of trading actions between consecutive episodes.
  - Procedural memory ranks events by recency, relevance and importance, as in FinMem.

**TradingAgents** (Xiao et al., Tauric Research, arXiv 2412.20138, Dec 2024)
- **Paper design.** Role-play firm: analysts (fundamental, sentiment, news, technical), bull/bear researcher debate, trader, risk team, fund manager. The paper mentions a "reflective agent" but publishes no memory ablation. — [TradingAgents PDF](https://arxiv.org/pdf/2412.20138)
- **Repo history.** The repo shipped a per-agent `FinancialSituationMemory`: BM25 over situation text, with lessons attached. v0.2.4 (2026-04-25) removed it, "subsumed by the persistent decision log." The changelog says the `reflect_and_remember()` plumbing was "dead." It also notes a bug fix: "Empty memory no longer triggers fabricated past-lessons in agent prompts." — [TradingAgents CHANGELOG (repo)](https://github.com/TauricResearch/TradingAgents/blob/main/CHANGELOG.md)
- **Current memory (v0.7.x, Oct–Nov 2026).** An append-only markdown log stores (ticker, date, rating, decision). — [TradingAgents repo, `tradingagents/memory/`](https://github.com/TauricResearch/TradingAgents)
  - A decision settles only after its full holding window has traded (default 5 days). Its score is the raw return plus alpha vs a benchmark (SPY, or an exchange-suffix map).
  - A 2–4 sentence reflection prompt is told to "say so plainly if the window is too short to judge the thesis."
  - Retrieval is the 5 most recent same-ticker entries plus 3 recent cross-ticker reflections. There is no similarity search.
  - Only the Portfolio Manager reads memory.
  - Decisions are scored from the first close after the decision date. This avoids crediting news the system could not have traded on (#1518).

**CryptoTrade** (Li et al., EMNLP 2024, arXiv 2407.09546)
- **Design.** A reflection agent reviews recent trades and returns, then writes a reflection report. The trading agent consumes that report alongside on-chain, off-chain and news inputs. — [CryptoTrade PDF](https://arxiv.org/pdf/2407.09546)

**Reflexion** (Shinn et al., NeurIPS 2023, arXiv 2303.11366)
- **Design.** "Verbal reinforcement": after a failed episode, the agent writes a self-reflection into an episodic buffer and retries. This assumes fast, clean feedback. — [Reflexion arXiv](https://arxiv.org/abs/2303.11366)
- **Critique.** A 2026 survey questions whether that assumption holds in trading, where outcomes are delayed and noisy. — [Agentic Trading survey, arXiv 2605.19337](https://arxiv.org/html/2605.19337v1)

**2025–2026 successors**
- **META** (Xu et al., arXiv 2609.28771, AACL-IJCNLP 2026 Findings) — [META HTML](https://arxiv.org/html/2609.28771)
  - Memory entries are (state, action, outcome, optional 3–5 sentence reflection).
  - The state has two parts: an indicator feature vector (momentum, volatility, mean reversion) and a normalized OHLCV window.
  - Retrieval is cosine top-k (k=5) with a similarity threshold τ=0.7, keyed on market state, not ticker.
  - A curation operator prunes low-utility or redundant entries. Retrieved cases are re-critiqued at test time.
- **CLQT** (Qu & Chen, arXiv 2606.29771, 2026) — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
  - Three memory tiers:
    - Working: the last 3 rounds.
    - Episodic: event→action→outcome records, decay 0.95 per round (half-life ≈13.5 rounds).
    - Semantic: generalized cross-asset patterns and typed lessons, decay 0.98 per round (half-life ≈34 rounds).
  - Retrieval relevance = recency + asset overlap + event-type match + regime match.
  - Episodes are consolidated every 12 rounds by merging on shared asset, event type and outcome.
  - A `memory_provenance` field logs which memories fed each decision.
- **EvolveTrade** (Kim et al., arXiv 2609.17632, Sep 2026). The system prompt is treated as a text policy. A Policy Agent rewrites it at intervals from decision traces and realized portfolio feedback. — [EvolveTrade abstract](https://arxiv.org/abs/2609.17632)
- **Survey taxonomy.**
  - Working memory is split into a deterministic, LLM-read-only state store and the generative context.
  - Episodic memory is (S_t, A_t, O_{t+k}, τ), where τ is the time the outcome became observable. An "Outcome Embargo" hides outcomes until then.
  - The survey recommends time-decayed relevance and regime "contextual conditioning."
  - In its reproducibility audit, only FinSage (2025) reports a timestamp rule, an embargo and a drift check. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)

### Inferences
- The field has converged on four ideas:
  - time-decayed scores;
  - LLM-written reflections as the outcome channel;
  - top-k injection into the prompt;
  - promotion or pruning by usefulness.
- None of these four is validated in isolation by a clean out-of-sample ablation (see Q2).
- TradingAgents is the most-used open-source system. Its move from BM25 situation memory to a short recency log with settled alpha is a practitioner signal. Simpler, correctly time-stamped, benchmark-relative outcome memory was preferred over similarity retrieval that was, in part, silently not working.
- FinMem's training-phase reflections are written while the model sees next-day price labels. Those reflections enter test-time memory as "experience." This is a form of hindsight leakage into memory content, even when the test window itself is later.

### Gaps
- StockAgent (arXiv 2407.18957) was not opened in this session. It is understood to be a market-simulation study of investor agents, not a memory-design paper, but this is unverified here.
- FinAgent's exact memory scoring formula was not extracted beyond "diversified retrieval" with short/medium/long queries.
- FinSage's memory design was not read beyond the survey's audit table.

## Q2. What gains were reported, over what periods and assets, against what baselines? Out-of-sample? Replications and critiques?

### Takeaway
Headline gains are large, but they come from short windows (3–9 months) and 1–6 tickers. Most test inside the backbone LLM's pretraining window, with single runs and no cost model. Independent re-tests show 50–72% decay in Sharpe or return once the window moves past the model cutoff. Long, broad backtests (FINSABER, 100+ symbols, 20 years) find that the advantages "deteriorate significantly." The one rigorous closed-loop memory ablation (CLQT) found memory and reflection return-neutral within run-to-run noise.

### Cited Findings
**FinMem** — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- Setup: trained 2021-08-17 to 2022-10-05, tested 2022-10-06 to 2023-04-10 (≈6 months). Five stocks: TSLA, NFLX, AMZN, MSFT, COIN. Backbone GPT-4-Turbo, single-share buy/sell/hold, no transaction costs reported.
- Reported cumulative return (CR) and Sharpe:

| Ticker | FinMem CR / Sharpe | Buy-and-hold CR | Notes |
|---|---|---|---|
| TSLA | 61.78% / 2.68 | −18.63% | |
| NFLX | 36.45% | 35.51% | Essentially equal to buy-and-hold |
| AMZN | 4.89% | −10.77% | |
| MSFT | 23.26% | 14.69% | |
| COIN | 34.98% | −30.01% | |

- **Top-K ablation (TSLA only).** CR was 52.1% (K=1), 29.4% (K=3), 54.7% (K=5) and 79.4% (K=10). The response is non-monotonic, and K=10 "resulted in significant losses in stable market conditions." — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **No memory-off comparison.** The ablations vary K, risk character and backbone. None compares memory on vs memory off on matched data. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)

**FinAgent** — [FinAgent PDF](https://arxiv.org/pdf/2402.18485)
- Setup: data 2022-06-01 to 2024-01-01. Test is 2023-06-01 to 2024-01-01 (≈7 months) on AAPL, AMZN, GOOGL, MSFT, TSLA and ETHUSD.
- Claim: "over 36% average improvement on profit," with 92.27% annual return on TSLA. Compared against 12 baselines, including FinMem and buy-and-hold.
- Component ablation on TSLA and ETH:
  - Adding low-level reflection lifts annual return by +45.6% on TSLA and +101.5% on ETH.
  - Adding high-level reflection lifts it a further +56% on TSLA and +4.7% on ETH.
  - Adding the rule-based tools cut ETH return by 21%.
- Single test window, no costs stated. The test overlaps GPT-4-class pretraining (GPT-4o cutoff is October 2023 per the Profit Mirage paper).

**FinCon** — [FinCon PDF](https://arxiv.org/pdf/2407.06567)
- Setup: trained 2022-01-03 to 2022-10-04 (repeated episodes over the same window), tested 2022-10-05 to 2023-06-10 (≈8 months). Eight single stocks plus a 3-stock portfolio.
- Belief-update (CVRF) ablation:

| Asset | With belief updates (CR / Sharpe) | Without belief updates (CR / Sharpe) |
|---|---|---|
| GOOG | 25.08% / 1.05 | −11.94% / −0.50 |
| NIO | 17.46% | 8.20% |
| Portfolio (TSLA, MSFT, PFE) | 113.8% / Sharpe 3.27 | 28.4% |

- Results come "after only four training episodes" over a single in-sample training window.

**TradingAgents** — [TradingAgents PDF](https://arxiv.org/pdf/2412.20138)
- Setup: test 2024-01-01 to 2024-03-29 (≈3 months, 3 stocks).
- Reported CR and Sharpe:

| Ticker | CR | Sharpe | Max drawdown |
|---|---|---|---|
| AAPL | 26.62% | 8.21 | 0.91% |
| GOOGL | 24.36% | 6.39 | — |
| AMZN | 23.21% | 5.60 | — |

- Baselines were rule-based only: buy-and-hold, MACD, KDJ+RSI, ZMR, SMA. There is no memory ablation.

**CryptoTrade** — [CryptoTrade PDF](https://arxiv.org/pdf/2407.09546)
- Setup: three ≈2-month test windows (bear 2023-04/06, sideways 2023-06/08, bull 2023-10/12) on BTC, ETH and SOL.
- The paper says it chose test dates after GPT-3.5's September 2021 cutoff, but its headline results use GPT-4o. GPT-4o's cutoff (October 2023) covers most of those windows.
- It is only "comparable" to buy-and-hold and MACD, beating "more than half" of the signals.
- Prompt ablation: Sharpe 0.23 with all components vs 0.06 without reflection; return 28.47% vs 17.14%.

**META** — [META HTML](https://arxiv.org/html/2609.28771)
- Setup: five instruments (CL, ES, NQ, QQQ, BTC) on 4-hour bars. The held-out set is the last three 100-candle windows per asset. The metric is directional accuracy, not PnL.
- Directional accuracy without → with memory:

| Asset | Without memory | With memory |
|---|---|---|
| CL | 52.5% | 56.0% |
| ES | 50.0% | 64.0% |
| NQ | 40.0% | 62.0% |
| QQQ | 55.0% | 61.5% |
| BTC | 48.0% | 56.0% |

- The text and table disagree on QQQ (60.0 vs 61.5). No costs or slippage are modeled; the authors name both as limitations.

**Independent re-tests and critiques**
- **Profit Mirage** (Li et al., arXiv 2510.07920, Oct 2025) — [Profit Mirage HTML](https://arxiv.org/html/2510.07920v1)
  - Ran FinMem, FinAgent, QuantAgent, FinCon and TradingAgents on GPT-4o (cutoff Oct 2023). Pre-cutoff window Q2–Q3 2021; post-cutoff window Q3–Q4 2024; universe NASDAQ-100. The market returned similar amounts in both windows (+13.79% vs +13.35%).
  - Decay after the cutoff:
    - FinMem: total return −71.85%, the largest. The authors cite its "strong reliance on memorized historical patterns."
    - FinCon: Sharpe −62.23%.
    - TradingAgents: Sharpe −55.68%, return −50.18%.
    - QuantAgent: Sharpe −51.48%.
  - Tool-using agents (FinAgent, QuantAgent) were "slightly" more resilient.
- **FINSABER** (Li et al., arXiv 2505.07078, KDD 2026 D&B oral, v6 June 2026). Over 20 years and 100+ symbols, earlier LLM advantages "deteriorate significantly." Its explanations: survivorship and data-snooping bias in earlier evaluations; LLM strategies are too conservative in bull markets and too aggressive in bear markets. It calls for regime-aware risk controls over more framework complexity. — [FINSABER abstract](https://arxiv.org/abs/2505.07078)
- **From Knowing to Doing / KTD-Fin** (Zhu et al., arXiv 2605.28359, May 2026). Ten frontier-LLM agents on CSI300, 2024–2026, with ticker, date and timestamp masking. Returns are "largely explained by passive market and style exposure, with limited evidence of persistent stock-selection alpha." Unmasked rationales cite brand identity and specific historical days. — [KTD-Fin arXiv](https://arxiv.org/abs/2605.28359)
- **CLQT memory ablation** (arXiv 2606.29771, 2026). Setup: 26 bi-weekly rounds in a benign bull market (SPY +27%, VIX median 17). — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
  - Run-to-run Sharpe noise is ±0.42 (1σ). Repeated runs move Sharpe by up to ~2.3.
  - Removing modules, ΔSharpe vs a 2.26 baseline:

| Module removed | ΔSharpe | Range |
|---|---|---|
| Memory | +0.52 | [+0.24, +0.72] |
| Reflection | +0.09 | [−0.16, +0.39] |

  - Both changes are within noise. The authors' verdict: memory is "returns-neutral" but kept because it is "the only source of the strategy-consistency signal." Reflection is "Conditional... deferrable for raw returns."
  - The authors say one bull regime "cannot test" the learning loop.
- **Alpha Illusion** (Ye et al., arXiv 2605.16895, May 2026, position paper). Reported alpha from FinCon, FinMem, TradingAgents, FinAgent, QuantAgent and FLAG-Trader cannot yet be separated from temporal contamination, unmodeled frictions, short-window Sharpe uncertainty, narrative fitting and parametric priors. It proposes six validity tests, including "predictive calibration," and a modular design where LLMs feed independent calibration, risk and execution modules. — [Alpha Illusion abstract](https://arxiv.org/abs/2605.16895)
- **Reporting audit.** The 2026 Agentic Trading survey finds only 2 of 19 primary studies report extractable time-consistent splits. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
- **Cost models.** CLQT cites a separate audit (Xia et al. 2026): only 1 of 19 closed-loop studies reports an explicit transaction-cost model. — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
- **Contamination-free benchmarks**
  - StockBench (arXiv 2510.02209, Oct 2025, v2 Mar 2026): "most LLM agents struggle to outperform the simple buy-and-hold baseline." A search-engine summary claimed the opposite; the abstract was used here. — [StockBench abstract](https://arxiv.org/abs/2510.02209)
  - LiveTradeBench ran 50-day live evaluations of 21 LLMs, with a fixed-length memory of past observations. — [LiveTradeBench](https://www.emergentmind.com/papers/2511.03628)
  - Agent Market Arena had a memory-building phase in May–July 2025 and live evaluation from 1 Aug 2025. — [When Agents Trade (AMA)](https://www.alphaxiv.org/abs/2510.11695v1)
  - No memory ablations from these three were found.
- **Counterpoint on lookahead size.** Chronologically consistent models (ChronoBERT/ChronoGPT, He, Lv, Manela, Wu, arXiv 2502.21206) find lookahead bias "modest" for news-based next-day return prediction. That is a single-task finding. — [Chronologically Consistent LLMs](https://arxiv.org/abs/2502.21206)
  - Glasserman & Lin (arXiv 2309.17322) also study look-ahead in GPT sentiment return prediction. — [Glasserman & Lin](https://arxiv.org/abs/2309.17322)

### Inferences
- Treat every headline number in the original papers as in-sample with respect to LLM pretraining, and as unreplicated:
  - FinMem 61.8% TSLA
  - FinAgent "36% average improvement"
  - FinCon 113.8%
  - TradingAgents Sharpe 5.6–8.2
- A Sharpe of 8.21 over 3 months on one stock (TradingAgents AAPL) is statistically meaningless. CLQT's ±0.42 run-to-run noise band shows that single-run Sharpe deltas below ~0.5 are noise even for a full year of bi-weekly rounds.
- Memory-on vs memory-off evidence:
  - FinCon and CryptoTrade ablations are positive but in-sample, short and single-run.
  - META is positive on directional accuracy only, with tiny held-out sets.
  - CLQT, the most careful test, is neutral to slightly negative on returns.
  - Net: no credible evidence yet that prompt-injected memory or reflection raises risk-adjusted returns out of sample.

### Gaps
- No published study runs a memory ablation across a full regime cycle (bull plus bear) on post-cutoff data with costs and repeated seeds. CLQT's authors say so explicitly.
- No independent replication was found for FinAgent's or FinCon's reflection ablations.
- Profit Mirage's per-agent absolute pre/post Sharpe values appear only in a figure and were not extracted.
- No study evaluates memory designs on EU equities.

## Q3. Should memory be retrieved by market-situation similarity (indicators/regime) or by ticker name?

### Takeaway
Weak but consistent evidence favors structured state keys (indicator vectors, regime/event tags) over ticker-name or free-text embedding keys. Ticker-name keys also let the LLM recall memorized ticker narratives, which is exactly the leakage the critiques identify. Pure similarity retrieval has a documented bias of its own: it returns mostly winners. No paper runs a clean ticker-vs-situation ablation.

### Cited Findings
- **META.** Retrieval by indicator-vector similarity beat text-embedding retrieval by +2 to +11 points of directional accuracy, at ~0.006 s vs ~3.1 s per query (≈500× faster). Adding state-keyed memory raised accuracy +3.5 to +22 points across 5 instruments. The authors note static similarity "does not adapt to regime changes." — [META HTML](https://arxiv.org/html/2609.28771)
- **CLQT.** Relevance combines recency with asset overlap, event-type match and regime match. That makes it a hybrid key, not ticker-only. Memory was return-neutral in a single bull regime. — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
- **Survey.** It recommends contextual conditioning: record the regime each memory applies to, and use time-decayed relevance. Old episodes with high similarity can mislead after regime shifts. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
- **FinMem.** Searches are filtered to the specific ticker, then ranked by text similarity. Profit Mirage attributes FinMem's 71.85% post-cutoff return decay to "memorized historical patterns." — [FinMem PDF](https://arxiv.org/pdf/2311.13743); [Profit Mirage](https://arxiv.org/html/2510.07920v1)
- **KTD-Fin.** With tickers and dates visible, agent rationales cite brand identity and specific historical days. Blinded, they reduce to factor rankings. — [KTD-Fin arXiv](https://arxiv.org/abs/2605.28359)
- **TradingAgents.** The current design uses ticker plus recency (5 same-ticker entries) plus 3 cross-ticker lessons. It dropped BM25 situation-text retrieval. — [TradingAgents repo](https://github.com/TauricResearch/TradingAgents)
- **Practitioner report (TradeMemory, not peer-reviewed).**
  - Similarity retrieval on XAUUSD M15 keyed on ATR proximity, session and regime returned 5 of 5 winners from a store of 9 wins and 3 losses.
  - Winners cluster in common conditions; losses sit in atypical regions with low similarity.
  - The agent then entered a strategy it had avoided across 2,000 no-memory decisions:

| Metric | No memory | With memory |
|---|---|---|
| Profit factor | 2.42 | 0.94 |
| PnL | +$176 | −$28 |

  - Tiny sample (6–7 trades). The fix forces ≥20% losses into top-k. — [dev.to: "I gave my trading agent memory and it made everything worse"](https://dev.to/mnemox/i-gave-my-trading-agent-memory-and-it-made-everything-worse-28a3)

### Inferences
- For a committee bot, the useful retrieval key is the setup, not the name:
  - trigger type (pullback / breakout);
  - regime (index trend, volatility bucket);
  - indicator state (RSI band, distance to MA, ATR%);
  - score bucket;
  - market (EU / US).
- Ticker should be at most a secondary filter. Per-ticker samples are tiny, and ticker strings invite parametric recall of the company's story.
- Similarity retrieval must be outcome-stratified (force losers into the set) or replaced by aggregate stats per bucket. Otherwise it creates a confirmation loop.

### Gaps
- No controlled study compares ticker-keyed vs situation-keyed retrieval on PnL with post-cutoff data.
- META's held-out set is three 100-candle windows per asset, with no costs.

## Q4. Numeric feedback (weights, thresholds, Bayesian/bandit calibration) or text "lessons" in prompts: which works better?

### Takeaway
No head-to-head study exists. Indirect evidence leans toward deterministic numeric feedback:
- Text lessons are noisy and unvalidated; the best-controlled test found reflection return-neutral.
- LLM self-correction without external signal is unreliable.
- The 2026 critiques explicitly recommend moving calibration out of the LLM into an independent numeric module.
Text-policy rewriting (FinCon CVRF, EvolveTrade) has only in-sample or unquantified support.

### Cited Findings
- **Text lessons, positive results.** FinCon's verbal belief updates and CryptoTrade's reflection ablation are positive, but in-sample, single-window and single-run (numbers in Q2). — [FinCon PDF](https://arxiv.org/pdf/2407.06567); [CryptoTrade PDF](https://arxiv.org/pdf/2407.09546)
- **EvolveTrade.** Text-policy refinement "often improves" Sharpe and cumulative return "in most evaluated settings." The abstract gives no numbers or out-of-sample statement. — [EvolveTrade abstract](https://arxiv.org/abs/2609.17632)
- **CLQT.** Reflection was return-neutral: ΔSharpe +0.09 when removed, within ±0.42 noise. Memory was also neutral. — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
- **Alpha Illusion.** "Language confidence is not tradable probability." Any system mapping verbal confidence to position size needs predictive calibration. The paper proposes LLMs as information interfaces feeding independent calibration, risk and execution modules. — [Alpha Illusion abstract](https://arxiv.org/abs/2605.16895)
- **Representation Signatures / Risk-Feedback Alignment** (arXiv 2605.28850, 2026). Over 52 decision steps, accumulated risk memory improved alignment with the risk gate and narrowed the calibration gap. The agents did not become profitable. Source is a search summary; the full text was not read. — [arXiv 2605.28850](https://arxiv.org/html/2605.28850v1)
- **General LLM evidence.** Intrinsic self-correction without external feedback often fails to improve, and can degrade, reasoning. — [Huang et al., "LLMs Cannot Self-Correct Reasoning Yet", arXiv 2310.01798](https://arxiv.org/abs/2310.01798)
- **Reflexion.** It works where feedback is immediate and binary (unit tests, task success). — [Reflexion arXiv](https://arxiv.org/abs/2303.11366)
- **Survey caveat on delay.** Reflexion-style self-correction "assumes prompt feedback," but trading outcomes arrive late. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
- **TradingAgents' evolved design.** It keeps text reflection but anchors it to a numeric, benchmark-relative settled outcome (5-day alpha). The reflection prompt must cite the figure and flag when the window is too short. That is a hedge against narrative over-fitting. — [TradingAgents repo, `memory/reflection.py`](https://github.com/TauricResearch/TradingAgents)

### Inferences
- Numbers should carry the learning; text should explain it. Concretely:
  - Track per-bucket hit rate and expectancy with shrinkage. Buckets are agent × setup × regime × market.
  - Use a Beta-Binomial prior on win rate, or a James-Stein / empirical-Bayes pull to the global mean.
  - Use those posteriors to adjust thresholds or agent weights within hard bounds.
  - Inject only the resulting numbers, with n and a credible interval, into the prompt.
- This avoids small-sample lesson poisoning and is auditable. It also matches the "independent calibration module" recommendation.
- Free-text lessons, if kept, should be:
  - generated only from settled, benchmark-relative outcomes;
  - required to cite the number and the sample size;
  - capped in count;
  - never allowed to override hard risk rules.

### Gaps
- No paper directly compares bandit or Bayesian score calibration against prompt-text lessons for LLM trading agents. The recommendation above is inferred from adjacent evidence, not tested.

## Q5. Known failure modes: memory poisoning from small-sample noise, recency bias, confirmation loops, prompt bloat

### Takeaway
All four failure modes are documented, mostly in surveys, practitioner reports and sensitivity results rather than in dedicated studies. The concrete ones:
- Retrieval returning only winners (confirmation).
- Non-monotonic top-K sensitivity (bloat and noise).
- Hindsight text leaking into memory.
- Fabricated lessons when memory is empty.
- Silently dead memory plumbing.
- Too-short outcome windows judged as thesis failures.

### Cited Findings
- **Small samples and confirmation.** The TradeMemory report:
  - Similarity retrieval returned 5 of 5 winners while the store held 3 losses.
  - The model's prior ("breakout works") "resonated" with the biased recall and raised confidence to 0.85.
  - That confidence was attached to its two worst trades (−$437 combined).
  - Separately, a regime-classifier bug mislabeled 5 records "unknown." — [dev.to TradeMemory post](https://dev.to/mnemox/i-gave-my-trading-agent-memory-and-it-made-everything-worse-28a3)
- **Prompt bloat.** FinMem K=10 retrieved memories gave the highest TSLA return but "significant losses in stable market conditions." The authors attribute this to trivial messages mixing with critical ones. K=3 underperformed K=1. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
- **Context dilution.** The survey cites "Lost in the Middle"; aggressive pruning drops live-position reasoning. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
- **Recency and regime drift.** Old, highly similar episodes mislead after regime shifts. Static similarity retrieval "does not adapt to regime changes." — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1); [META HTML](https://arxiv.org/html/2609.28771)
- **Recency in practice.** Exponential recency decay (FinMem Q=14/90/365 days; CLQT 0.95/0.98 per round) is a hand-set design choice. None of the papers tunes it out of sample. — [FinMem PDF](https://arxiv.org/pdf/2311.13743); [CLQT PDF](https://arxiv.org/pdf/2606.29771)
- **Hindsight and oracle leakage into memory.**
  - The survey's "Oracle Fallacy": retrieving an episode that carries a post-hoc narrative. Fix: an Outcome Embargo. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
  - FinMem's training reflections are written while the model sees next-day price labels. — [FinMem PDF](https://arxiv.org/pdf/2311.13743)
  - TradingAgents moved outcome scoring to the first close after the decision date. — [TradingAgents CHANGELOG](https://github.com/TauricResearch/TradingAgents/blob/main/CHANGELOG.md)
- **Fabricated lessons and dead plumbing.** TradingAgents fixed "Empty memory... triggers fabricated past-lessons in agent prompts." It removed `reflect_and_remember()` as "dead" plumbing. — [TradingAgents CHANGELOG](https://github.com/TauricResearch/TradingAgents/blob/main/CHANGELOG.md)
- **Too-short outcome windows.** The TradingAgents reflection prompt warns that "a thesis written for months is not disproved by a week, and a lesson that ignores the difference is read by later runs as an established failure." — [TradingAgents repo, `memory/reflection.py`](https://github.com/TauricResearch/TradingAgents)
- **Poisoning from uncurated sources.** Social and forum sources are open to noise injection. The survey's fix is a "Hierarchy of Truth" in which internal state overrides external text. — [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
- **Run-to-run noise hides everything.** Repeated CLQT runs move Sharpe by up to ~2.3. One benign regime masks module value. — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
- **Regime asymmetry.** FINSABER finds LLM agents too conservative in bulls and too aggressive in bears. — [FINSABER abstract](https://arxiv.org/abs/2505.07078)
- **Duplicate settlement.** TradingAgents guards against settling the same decision twice, which "would otherwise count the same decision twice in past context and in every aggregate." — [TradingAgents repo, `memory/log.py`](https://github.com/TauricResearch/TradingAgents)

### Inferences
- The highest-risk failure for this bot: committee prompts receive free-text "lessons" distilled from a few trigger outcomes, retrieved by ticker or similarity, with no sample size shown. That combines small-sample poisoning, confirmation and leakage of hindsight narrative.
- An empty or near-empty memory must render as nothing, not as an invitation to invent lessons.

### Gaps
- No quantitative study of memory poisoning rates or confirmation loops in LLM trading agents was found beyond the single practitioner report.

## Q6. Transferable design patterns, ranked by strength of evidence

### Takeaway
The strongest-evidenced patterns concern evaluation hygiene and time-integrity, not memory cleverness. Among memory designs, structured situation keys plus numeric, shrunk outcome statistics have the best indirect support. LLM-written free-text lessons have the weakest out-of-sample support.

### Cited Findings
Ranked from strongest to weakest evidence. Each item gives the evidence basis, then the transfer to this bot.

1. **Point-in-time integrity of memory content.** Strong: multiple independent studies.
   - Evidence: post-cutoff decay of 50–72% in Profit Mirage; FINSABER's long-horizon deterioration; KTD-Fin's masking result. — [Profit Mirage](https://arxiv.org/html/2510.07920v1); [FINSABER](https://arxiv.org/abs/2505.07078); [KTD-Fin](https://arxiv.org/abs/2605.28359)
   - Transfer: store `observable_at` for every outcome. Never inject an outcome before it settles. Score from the first tradable price after the decision. Keep training-time hindsight text out of memory. — [TradingAgents CHANGELOG](https://github.com/TauricResearch/TradingAgents/blob/main/CHANGELOG.md); [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
2. **Judge memory changes against noise, with repeated runs and costs.** Strong.
   - Evidence: CLQT's ±0.42 Sharpe noise band; only 1 of 19 studies models costs. — [CLQT PDF](https://arxiv.org/pdf/2606.29771)
   - Transfer: A/B memory-on vs memory-off through the existing `ShadowPortfolios` before trusting any gain.
3. **Benchmark-relative, settled outcome labels.** Moderate: practitioner convergence plus the critiques.
   - Evidence: TradingAgents' 5-day alpha vs index; KTD-Fin finds raw returns mostly beta and style. — [TradingAgents repo](https://github.com/TauricResearch/TradingAgents); [KTD-Fin](https://arxiv.org/abs/2605.28359)
   - Transfer: label trigger and trade outcomes as alpha vs the relevant index (STOXX 600 / DAX for EU, SPY for US) over a fixed horizon. Also record MAE and MFE.
4. **Structured situation keys over ticker or free text.** Moderate-weak: one paper with ablation, plus survey and CLQT design.
   - Evidence: META indicator-vector retrieval +2 to 11 accuracy points over text embeddings; CLQT regime, event and asset match. — [META](https://arxiv.org/html/2609.28771); [CLQT](https://arxiv.org/pdf/2606.29771)
   - Transfer: key memories on (trigger_type, regime, RSI/ATR/MA-distance buckets, score bucket, market). Keep ticker secondary.
5. **Outcome-stratified retrieval with explicit sample size.** Moderate-weak: one practitioner report plus FinMem K sensitivity.
   - Evidence: winners-only retrieval caused a profit factor collapse. — [dev.to TradeMemory](https://dev.to/mnemox/i-gave-my-trading-agent-memory-and-it-made-everything-worse-28a3)
   - Transfer: show wins and losses with n. Force ≥20% losers into the set, or show bucket aggregates instead of anecdotes.
6. **Numeric calibration outside the LLM.** Weak-moderate: position papers plus adjacent results, no head-to-head.
   - Evidence: Alpha Illusion's call for an independent calibration module; the self-correction literature. — [Alpha Illusion](https://arxiv.org/abs/2605.16895); [Huang et al.](https://arxiv.org/abs/2310.01798)
   - Transfer: shrink per-bucket win rate and expectancy (Beta prior). Adjust `TRIGGER_MIN_SCORE` or agent weights within hard caps. Inject numbers, not prose.
7. **Small, capped injection (top-k ≈ 3–5) with decay.** Weak: FinMem's single-ticker K ablation and survey commentary. — [FinMem PDF](https://arxiv.org/pdf/2311.13743); [Agentic Trading survey](https://arxiv.org/html/2605.19337v1)
   - Transfer: hard token budget; recency decay; consolidate duplicates.
8. **Empty-memory guard and memory provenance logging.** Weak as performance evidence, strong as a correctness practice.
   - Evidence: TradingAgents' fabricated-lessons fix; CLQT's `memory_provenance`. — [TradingAgents CHANGELOG](https://github.com/TauricResearch/TradingAgents/blob/main/CHANGELOG.md); [CLQT](https://arxiv.org/pdf/2606.29771)
   - Transfer: log which memory IDs entered each committee prompt, so a shadow test can attribute outcomes.
9. **LLM reflection and text lessons / text-policy rewriting** (Reflexion, CVRF, EvolveTrade). Weakest: in-sample, unreplicated; CLQT found it return-neutral. — [FinCon](https://arxiv.org/pdf/2407.06567); [EvolveTrade](https://arxiv.org/abs/2609.17632); [CLQT](https://arxiv.org/pdf/2606.29771)
   - Transfer, if kept: 2–4 sentences, must cite the outcome number and window, flag short windows, and never override risk rules. — [TradingAgents `reflection.py`](https://github.com/TauricResearch/TradingAgents)
10. **Layered memory by data timeliness** (FinMem shallow/intermediate/deep). Weakest: never ablated on/off; the largest post-cutoff decay. — [FinMem PDF](https://arxiv.org/pdf/2311.13743); [Profit Mirage](https://arxiv.org/html/2510.07920v1)
    - Transfer: low priority. Use only a simple decay on source age.

### Inferences
- **Expected gain.** It is unknown, and plausibly zero, for return. The published evidence supports memory mainly as a consistency, audit and risk-discipline aid (CLQT's verdict), not as a source of alpha.
- **Most defensible build order for this repo:**
  1. Point-in-time settled alpha labels on trigger and trade events.
  2. Per-setup/regime bucket statistics with shrinkage.
  3. A capped numeric "track record" block in the committee prompt, empty when n is small.
  4. Shadow A/B test (memory on vs off) over enough events to clear noise.
  5. Only then consider text lessons.
- **Minimum sample.** A rough guide: with win-rate noise SE ≈ sqrt(p(1−p)/n), n ≈ 100 settled events per bucket gives ±5 percentage points. Buckets with n < 20–30 should show "insufficient data" rather than a lesson. This is a statistical inference, not from a cited trading paper.

### Gaps
- No source quantifies the return benefit of any single pattern above in out-of-sample, post-cutoff, cost-aware equity trading. The ranking reflects strength of evidence, not effect size.
- EU equity evidence is absent from the literature reviewed.
