# Graph Report - AITrader  (2026-11-16)

## Corpus Check
- 167 files · ~226,839 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 8 file(s) not represented in the graph (top: (none) 4, .mdc 1, .example 1)

## Summary
- 3479 nodes · 10265 edges · 140 communities (118 shown, 22 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 703 edges (avg confidence: 0.91)
- Token cost: 714,417 input · 0 output

## Community Hubs (Navigation)
- test_trigger_gate.py
- Any
- test_audit_security.py
- asyncio
- test_t212_resting_orders.py
- Memory and Reflection in LLM Trading Age
- TelegramAssistant
- test_main_parity_exit.py
- shadow.py
- Position
- app.js
- main_strategy.py
- init_db
- LiveBroker
- memory/routes.py
- Measuring Memory/Learning Gains and Thei
- test_telegram_trigger_gate.py
- test_agent_memory.py
- ShadowPortfolios
- test_shadow_learner.py
- Trading212Client
- test_promote.py
- get_main_rules
- write_main_rules
- db/models.py
- AgentMemoryStore
- PaperBroker
- Clean UI with Module Toggles Implementat
- test_agent_robustness.py
- test_audit_shadow.py
- Internal Code Audit: Agent-Memory Subsys
- esc
- test_trade_logic_fixes.py
- test_review_tracker.py
- Trade
- plan_buy
- test_trading212_broker.py
- live.py
- PortfolioSnapshot
- test_outcome_memory.py
- test_shadow.py
- UserHolding
- saveSettings
- settings.py
- test_user_holdings_guards.py
- datetime
- levels.py
- web/routes.py
- test_research_strategy.py
- shadow_learner.py
- switchTab
- re
- test_committee_jobs.py
- enabledPlugins
- test_entry_sizing.py
- calculate_broker_fee
- cash_flows.py
- test_user_holdings.py
- showToast
- Shadow Learning, Outcome Memory, Promote
- Shadow learning, outcome memory, promote
- test_audit_outcomes.py
- test_audit_learner.py
- test_settings_persistence.py
- Any
- test_trade_stats.py
- test_ticker_detail.py
- Report: Link outcomes to verdicts before
- test_broker_tradability.py
- test_technicals.py
- get_trigger_min_score
- MarketScanner
- Python requirements
- test_risk_invariants.py
- test_currency.py
- run_market_session
- execute_sell
- market.py
- loadPortfolio
- test_shadow_api.py
- LLMClient
- post
- loadTriggers
- conftest.py
- test_cash_flows.py
- TradeLearner
- Any
- holdings.py
- refresh_positions_pnl
- BaseModel
- Ponytail-audit skill
- Resting Trading 212 Orders Implementatio
- My Holdings Implementation Plan
- AgentMemoryClient
- TradingScheduler
- asyncio
- My Holdings UI mockup
- features.js
- Shadow exit tests, trigger cancel reason
- My holdings (hands-off positions) - desi
- SystemConfig
- _step
- Backtest discipline skill
- Resting orders at Trading 212 - design
- _call_upstream_llm
- get_active_broker
- test_user_holdings_api.py
- Skills environment
- _shadow_call
- CLAUDE.md project guide
- server.py
- WatchlistItem
- Ticker detail sheet: design
- monitor_resting_orders
- _run
- Ticker detail sheet: implementation prog
- MarketDataProvider
- broker_can_buy
- _plan_trigger
- Unslop UI design skill (anti-slop rules
- memory_stats.py
- news.py
- test_active_broker_proxy_delegation
- check_broker_status
- test_broker_status.py
- _start_job
- _arm_two_triggers_for_one_free_slot
- FakeCommittee
- get_ticker_detail
- test_live_execution_fee_accounting
- RevalidatingStaticFiles
- promote_shadow
- test_ticker_info_is_cached_between_quote
- _FakeTicker
- _isolated
- test_price_history_range_mapping
- sync_portfolio
- start-memory-server.sh

## God Nodes (most connected - your core abstractions)
1. `LiveBroker` - 196 edges
2. `Position` - 143 edges
3. `OrderTrigger` - 129 edges
4. `init_db()` - 121 edges
5. `SystemConfig` - 108 edges
6. `Trade` - 89 edges
7. `PortfolioSnapshot` - 81 edges
8. `write_main_rules()` - 72 edges
9. `get_main_rules()` - 67 edges
10. `Internal Code Audit: Agent-Memory Subsystem` - 67 edges

## Surprising Connections (you probably didn't know these)
- `LLM output merged over deterministic reports` --references--> `LLMClient`  [EXTRACTED]
  CLAUDE.md → src/agents/base.py
- `Committee prompt: numeric track record replaces cold-start memory and lessons` --conceptually_related_to--> `InvestmentCommittee`  [INFERRED]
  docs/superpowers/plans/2026-11-09-shadow-tests-cancel-reason-trade-stats.md → src/agents/committee.py
- `Cold-Start Loadout` --conceptually_related_to--> `ColdStartEngine`  [EXTRACTED]
  research_notes/Trading bot memory gains/internal_code_audit.md → src/memory/cold_start.py
- `No As-Of Filtering in Memory` --rationale_for--> `ColdStartEngine`  [EXTRACTED]
  research_notes/Trading bot memory gains/internal_code_audit.md → src/memory/cold_start.py
- `Tail Truncation of Loadout` --rationale_for--> `ColdStartEngine`  [EXTRACTED]
  research_notes/Trading bot memory gains/internal_code_audit.md → src/memory/cold_start.py

## Import Cycles
- 2-file cycle: `src/db/session.py -> src/trading/main_strategy.py -> src/db/session.py`

## Hyperedges (group relationships)
- **Caveman style mirrored across agent rule files** — agents_caveman_style, _clinerules_caveman_caveman_style, _github_copilot_instructions_caveman_style, _opencode_agents_caveman_style, _windsurf_rules_caveman_caveman_style [EXTRACTED 1.00]
- **Vendored ponytail skill family** — _claude_skills_ponytail_skill, _claude_skills_ponytail_review_skill, _claude_skills_ponytail_audit_skill, _claude_skills_ponytail_debt_skill, _claude_skills_ponytail_skill_shortcut_comment_convention [EXTRACTED 1.00]
- **Closing the decision-to-outcome loop** — reports_trading_bot_memory_gains_item1_link_decision_to_outcome, reports_trading_bot_memory_gains_item2_daily_outcome_labeler, reports_trading_bot_memory_gains_item3_honest_learner, reports_trading_bot_memory_gains_item6_bounded_numeric_gate, reports_trading_bot_memory_gains_paired_memory_ab_test [INFERRED 0.85]
- **Memory write and extraction pipeline** — src_agents_committee_investmentcommittee, src_memory_client_log_interaction, src_memory_client_record_trade_event, src_memory_client_record_trigger_event, src_memory_extractor_assetextractor, src_memory_store_agentmemorystore [EXTRACTED 1.00]
- **Memory on/off paired shadow ablation** — research_notes_trading_bot_memory_gains_measurement_and_risk_memory_on_off_paired_ablation, research_notes_trading_bot_memory_gains_measurement_and_risk_use_memory_flag, research_notes_trading_bot_memory_gains_measurement_and_risk_report_variant_shadow_field, src_agents_committee_evaluate_ticker, src_trading_shadow_follow_report, research_notes_trading_bot_memory_gains_measurement_and_risk_triple_barrier_decision_labels [EXTRACTED 1.00]
- **Time-integrity outcome memory patterns** — research_notes_trading_bot_memory_gains_llm_trading_memory_literature_point_in_time_memory_integrity, research_notes_trading_bot_memory_gains_llm_trading_memory_literature_outcome_embargo, research_notes_trading_bot_memory_gains_llm_trading_memory_literature_benchmark_relative_settled_outcome_labels, research_notes_trading_bot_memory_gains_llm_trading_memory_literature_settled_decision_log_with_5_day_alpha, research_notes_trading_bot_memory_gains_llm_trading_memory_literature_memory_provenance_logging [INFERRED 0.85]
- **Resting T212 order sync loop** — docs_superpowers_plans_2026_11_03_t212_resting_orders_t212_order_sync_job, docs_superpowers_plans_2026_11_03_t212_resting_orders_sync_resting_entries, docs_superpowers_plans_2026_11_03_t212_resting_orders_execute_buy_split, docs_superpowers_plans_2026_11_03_t212_resting_orders_place_stop_limit, docs_superpowers_plans_2026_11_03_t212_resting_orders_cancel_resting_entries [EXTRACTED 1.00]
- **Bot | Mine owner flip flow** — docs_superpowers_plans_2026_11_05_my_holdings_bot_mine_slider, docs_superpowers_plans_2026_11_05_my_holdings_holdings_api, docs_superpowers_plans_2026_11_05_my_holdings_release_to_user, docs_superpowers_plans_2026_11_05_my_holdings_take_from_user, docs_superpowers_plans_2026_11_05_my_holdings_user_holdings_table [EXTRACTED 1.00]
- **Shadow learning to promote-to-main flow** — docs_superpowers_plans_2026_11_10_shadow_learning_strategy_params_schema, docs_superpowers_plans_2026_11_10_shadow_learning_shadow_learner, docs_superpowers_plans_2026_11_10_shadow_learning_outcome_atoms, docs_superpowers_plans_2026_11_10_shadow_learning_promote_api, docs_superpowers_plans_2026_11_10_shadow_learning_main_rules_resolver, docs_superpowers_plans_2026_11_10_shadow_learning_pause_main_learner [EXTRACTED 1.00]
- **Promote safety controls** — docs_superpowers_specs_2026_11_10_shadow_learning_design_promote_flow, docs_superpowers_specs_2026_11_10_shadow_learning_design_promote_live_confirm, docs_superpowers_specs_2026_11_10_shadow_learning_design_main_learner_paused, docs_superpowers_specs_2026_11_10_shadow_learning_design_non_overridable_interlocks, docs_superpowers_specs_2026_11_10_shadow_learning_design_rules_version [EXTRACTED 1.00]
- **Shadow learning loop** — docs_superpowers_specs_2026_11_10_shadow_learning_design_outcome_memory, docs_superpowers_specs_2026_11_10_shadow_learning_design_search_outcomes, docs_superpowers_specs_2026_11_10_shadow_learning_design_shadow_learner, docs_superpowers_specs_2026_11_10_shadow_learning_design_step_cap, docs_superpowers_specs_2026_11_10_shadow_learning_design_apply_param_change, docs_superpowers_specs_2026_11_10_shadow_learning_design_shadow_param_changes [EXTRACTED 1.00]
- **Bot/Mine flip mechanism** — docs_superpowers_specs_2026_11_05_my_holdings_design_own_switch_slider, docs_superpowers_specs_2026_11_05_my_holdings_design_holdings_api, docs_superpowers_specs_2026_11_05_my_holdings_design_release_to_user, docs_superpowers_specs_2026_11_05_my_holdings_design_take_from_user, docs_superpowers_specs_2026_11_05_my_holdings_design_userholding [EXTRACTED 1.00]
- **Tab navigation via switchTab (desktop nav, bottom dock, More sheet)** — src_web_static_index_desktop_nav, src_web_static_index_bottom_nav, src_web_static_index_mobile_more_sheet, src_web_static_app_switchtab [EXTRACTED 1.00]
- **Shadow edit and promote-to-main flow** — src_web_static_index_shadow_tab, src_web_static_index_shadow_editor_dialog, src_web_static_index_promote_dialog, src_web_static_index_main_rules_banner [INFERRED 0.85]

## Communities (140 total, 22 thin omitted)

### Community 0 - "test_trigger_gate.py"
Cohesion: 0.08
Nodes (70): evaluate_manual_trigger(), evaluate_trigger_verdict(), result(), _hard_block(), blocked(), _has_stop_below_entry(), invalid_ticker_result(), is_tradable_quote_type() (+62 more)

### Community 1 - "Any"
Cohesion: 0.05
Nodes (3): is_trigger_expired(), get_client(), test_resting_orders_flag()

### Community 2 - "test_audit_security.py"
Cohesion: 0.05
Nodes (41): _allowed_origins(), _check_basic_auth(), _deny(), _host_allowed(), install_web_security(), _is_kill_switch_on(), _is_loopback_client(), _is_private_client() (+33 more)

### Community 3 - "asyncio"
Cohesion: 0.07
Nodes (10): check_universe(), main(), merge_llm_report(), _resolve_symbol_lines(), _asset_version(), lifespan(), serve_index(), _history() (+2 more)

### Community 4 - "test_t212_resting_orders.py"
Cohesion: 0.11
Nodes (48): _cancel_done(), set_kill_switch(), test_live_resting_fill_books_rules_version_and_starting_stop(), test_live_trigger_monitor_fill_books_rules_version_and_starting_stop(), _breakout(), _check(), _drain_kill_switch_tasks(), _positions() (+40 more)

### Community 5 - "Memory and Reflection in LLM Trading Age"
Cohesion: 0.07
Nodes (42): Agent Market Arena (arXiv 2510.11695), Agentic Trading Survey (arXiv 2605.19337), Alpha Illusion (Ye et al. 2026), Benchmark-Relative Settled Outcome Labels, Beta-Binomial / Empirical-Bayes Shrinkage, Capped Top-K Injection with Decay, Chronologically Consistent LLMs (He, Lv, Manela, Wu), CLQT (Qu and Chen 2026) (+34 more)

### Community 6 - "TelegramAssistant"
Cohesion: 0.09
Nodes (33): TelegramAssistant, _capture_bot(), _owner_chat_id(), test_telegram_analyze_command(), test_telegram_buy_command(), test_telegram_buy_invalid_amount_rejected(), test_telegram_calibrate_string_types(), test_telegram_calibrate_with_trades() (+25 more)

### Community 7 - "test_main_parity_exit.py"
Cohesion: 0.11
Nodes (41): _drain(), _sell(), _set_manual(), test_adopted_holding_is_manual(), test_bot_position_sell_is_not_flagged(), test_broker_side_close_found_by_the_sync_runs_the_learner(), test_buy_flags_position_and_trade(), test_learner_failure_never_fails_the_sell() (+33 more)

### Community 8 - "shadow.py"
Cohesion: 0.11
Nodes (39): ShadowAccount, ShadowParamChange, ShadowPosition, ShadowSnapshot, ShadowStrategyRow, ShadowTrigger, apply_param_change(), _clean_label() (+31 more)

### Community 9 - "Position"
Cohesion: 0.07
Nodes (28): OrderTrigger, Position, _live_trigger(), test_broker_switch_starts_a_new_history_for_loss_and_return(), test_cash_capped_at_broker_buying_power(), test_daily_loss_limit_counts_only_todays_drop_and_turns_on_kill_switch(), test_live_broker_full_execution_lifecycle(), test_live_broker_get_triggers_has_current_price_and_distance() (+20 more)

### Community 10 - "app.js"
Cohesion: 0.06
Nodes (45): adviceReportsCache, ALLOC_COLORS, applyTheme(), collectShadowParams(), crosshairPlugin, cssVar(), drawShadowChart(), flipping (+37 more)

### Community 11 - "main_strategy.py"
Cohesion: 0.08
Nodes (31): Subsystem & symbol map, Shadow learning and promote, _apply_settings(), _check_range(), _defaults(), _dump(), effective_max_positions(), _load_rows() (+23 more)

### Community 12 - "init_db"
Cohesion: 0.07
Nodes (26): init_db(), test_init_db_syncs_main_rules(), test_paper_broker_full_lifecycle(), _make_paper_trigger(), test_ai_trigger_price_calculation(), test_auto_arm_triggers_score_above_80(), mock_eval(), mock_stock() (+18 more)

### Community 13 - "LiveBroker"
Cohesion: 0.08
Nodes (36): pick_bot_levels(), _price(), LiveBroker, _assert_unchanged(), _state(), _take_state(), test_exit_waits_for_flip_and_skips_released_position(), test_pick_bot_levels_default_when_none_valid() (+28 more)

### Community 14 - "memory/routes.py"
Cohesion: 0.11
Nodes (37): Items needing owner decision (memory API, proxy, Alpaca, code graph), AddAtomRequest, AddConversationRequest, ApiResponse, ChatMemoryContextRequest, CodeGraphNode, CreateSkillRequest, CreateWikiRequest (+29 more)

### Community 15 - "Measuring Memory/Learning Gains and Thei"
Cohesion: 0.08
Nodes (42): backtest-discipline Skill, 30-Trade Noise Floor, A/A Noise Test, Bailey and Lopez de Prado 2012, Sharpe Ratio Efficient Frontier, Bailey and Lopez de Prado 2014, Deflated Sharpe Ratio, Bailey, Borwein, Lopez de Prado, Zhu 2017, Probability of Backtest Overfitting, ChronoBERT/ChronoGPT (AEA 2026), Counterfactual Outcome Tracking (+34 more)

### Community 16 - "test_telegram_trigger_gate.py"
Cohesion: 0.11
Nodes (44): buy_env(), _insert_position(), _insert_trigger(), _report(), _stock(), telegram_env(), test_analyze_auto_arms_an_approved_buy_at_the_threshold_and_names_the_real_threshold(), test_analyze_does_not_arm_an_untradable_instrument() (+36 more)

### Community 17 - "test_agent_memory.py"
Cohesion: 0.09
Nodes (27): Dead code purge list, Item 7: memory hygiene fixes, _build_history_recap, ColdStartEngine, AssetExtractor, ColdStartLoadout, L2Scenario, L3CoreProfile (+19 more)

### Community 18 - "ShadowPortfolios"
Cohesion: 0.09
Nodes (6): _inherited(), _return_pct(), ShadowPortfolios, ShadowStrategy, _utcnow(), test_main_return_after_deposit_is_not_inflated()

### Community 19 - "test_shadow_learner.py"
Cohesion: 0.12
Nodes (40): update_strategy(), test_manual_learn_does_not_reuse_trades_already_learned_from(), _add_closed(), _add_pnls(), _freeze(), _history(), _learner(), _learner_raised_stop_scale() (+32 more)

### Community 20 - "Trading212Client"
Cohesion: 0.09
Nodes (13): _ccy(), _matching(), _older_than(), _suffix_for(), Trading212Client, test_instrument_list_outage_fails_open_once_per_retry_window(), test_t212_history_transactions_pages_until_known_ref(), handler() (+5 more)

### Community 21 - "test_promote.py"
Cohesion: 0.08
Nodes (33): _main_learner_paused(), create_strategy(), slug(), client(), _drop_new_history(), _history(), live(), _main_regime_on() (+25 more)

### Community 22 - "get_main_rules"
Cohesion: 0.10
Nodes (34): get_main_rules(), is_default_shape(), sync_settings_from_main_rules(), _clear_rows(), _isolated_rules(), _row(), _set_row(), test_as_strategy_carries_rules() (+26 more)

### Community 23 - "write_main_rules"
Cohesion: 0.17
Nodes (35): write_main_rules(), derive_trigger_levels(), _clean_ticker(), _pending(), _positions(), _report(), _run_session(), _stock() (+27 more)

### Community 24 - "db/models.py"
Cohesion: 0.08
Nodes (15): Item 1: report_id + initial 1R on trigger/position/trade, No row links verdict to outcome, AnalysisReport, is_european_ticker(), test_recommendations_show_only_latest_report_per_ticker(), test_antigravity_llm_client(), test_fastapi_endpoints(), test_is_european_ticker() (+7 more)

### Community 25 - "AgentMemoryStore"
Cohesion: 0.15
Nodes (10): Memory as per-ticker last-state cache, L0Message, L1Atom, AgentMemoryStore, _like_pattern(), _query_tokens(), test_codegraph_auto_repairs_drift(), test_l1_ranking_and_prune_and_compact() (+2 more)

### Community 26 - "PaperBroker"
Cohesion: 0.10
Nodes (8): PaperBroker, get_effective_deployable_cash(), test_paper_trigger_viability_uses_the_live_broker_fees(), test_api_settings_capital_limits(), test_broker_portfolio_summary_capital_limits(), test_get_effective_deployable_cash_helper(), test_paper_broker_trigger_and_buy_cap_enforcement(), test_paper_buy_books_the_starting_stop()

### Community 27 - "Clean UI with Module Toggles Implementat"
Cohesion: 0.08
Nodes (30): Mobile-first Home Redesign Implementation Plan, Home accessibility and design pass, Compact hero and KPI/trigger row, Home DOM contract test (tests/test_web_home_contract.py), Larger Home position cards, Playwright screenshot harness, Home status block (status and controls), Clean UI with Module Toggles Implementation Plan (+22 more)

### Community 28 - "test_agent_robustness.py"
Cohesion: 0.12
Nodes (16): InvestmentCommittee, FundamentalAnalystAgent, RiskManagerAgent, SentimentAnalystAgent, TechnicalAnalystAgent, _stock(), test_committee_llm_malformed_and_nonbuy_cap(), fake() (+8 more)

### Community 29 - "test_audit_shadow.py"
Cohesion: 0.10
Nodes (25): _check(), clamp_step(), _finite(), _flag(), _num(), ParamSpec, to_json_schema(), validate() (+17 more)

### Community 30 - "Internal Code Audit: Agent-Memory Subsys"
Cohesion: 0.08
Nodes (26): Code Graph Irrelevant to P&L, Cold-Start Loadout, Dead Extractor LLM Branch, Euro Mislabel of Native Prices, Internal Code Audit: Agent-Memory Subsystem, L0 Conversation Storage, L1 Memory Atoms, L2 Scenario (+18 more)

### Community 31 - "esc"
Cohesion: 0.13
Nodes (38): advicePrice(), adviceScoreBlock(), adviceVerdictBadge(), esc(), gainCls(), getJson(), loadLearningInsights(), loadMarketReview() (+30 more)

### Community 32 - "test_trade_logic_fixes.py"
Cohesion: 0.12
Nodes (22): _alpaca_close(), _delete_paper_rows(), _delete_positions(), _FakeAlpacaClient, _has_open_paper_position(), _insert_paper_trigger(), _live_position(), _LiveMode (+14 more)

### Community 33 - "test_review_tracker.py"
Cohesion: 0.08
Nodes (13): fmt_digest_time(), ReviewTracker, MarketDigest, digest_headline(), test_compare_past_week_reviews_evaluation(), test_digest_headline_names_manual_scan_not_an_exchange_open(), test_digest_time_converts_stored_utc_to_amsterdam(), test_market_open_advice_card_statuses() (+5 more)

### Community 34 - "Trade"
Cohesion: 0.14
Nodes (30): Trade, _arrange_calibration_trades(), _arrange_idle_decay(), _arrange_window(), _calibration_rows(), _db_scores(), _set_paused(), _set_rows() (+22 more)

### Community 35 - "plan_buy"
Cohesion: 0.10
Nodes (24): allocate_pending_triggers(), plan_buy(), plan_pending_triggers(), _risk_cap_note(), test_a_share_above_max_position_pct_leaves_the_trigger_unfunded(), test_fractional_mode_is_unchanged(), test_no_allocation_buys_nothing(), test_one_share_is_limited_by_free_cash() (+16 more)

### Community 36 - "test_trading212_broker.py"
Cohesion: 0.07
Nodes (27): Trading212Error, test_book_live_fill_records_position_trade_and_snapshot(), _cleanup(), _open_position(), test_buy_ambiguous_ticker_refused(), test_buy_fills_fully_and_places_stop(), test_buy_partial_fill_cancels_rest_and_books_filled(), test_buy_post_validation_error_is_not_looked_up() (+19 more)

### Community 38 - "PortfolioSnapshot"
Cohesion: 0.11
Nodes (20): PortfolioSnapshot, test_sync_adopts_untracked_and_closes_only_when_confirmed(), test_sync_waits_for_inflight_buy(), _bot_position(), _cash_sync_after_own_buy(), clean_money(), _daily_loss_with_mine_at(), _live_snapshot_count() (+12 more)

### Community 39 - "test_outcome_memory.py"
Cohesion: 0.10
Nodes (26): write(), record_outcome(), test_loadout_shows_active_mode_outcomes_only(), clean_outcomes(), _drain(), _noop(), _open_baseline_then_stop_out(), _open_position() (+18 more)

### Community 40 - "test_shadow.py"
Cohesion: 0.14
Nodes (21): _data(), _report(), _set_config(), shadows(), enable(), _reset_all(), test_deposit_into_the_live_account_does_not_lift_the_main_return(), test_entry_type_lookup_failure_still_commits_closes() (+13 more)

### Community 41 - "UserHolding"
Cohesion: 0.21
Nodes (24): UserHolding, _held(), _live(), mine_holding(), test_own_buy_in_t212_app_does_not_trip_kill_switch(), _add_mine(), _add_plan(), _cleanup() (+16 more)

### Community 42 - "saveSettings"
Cohesion: 0.11
Nodes (33): loadAllData(), loadBrokerStatus(), loadMarketStatus(), loadSettings(), onAutoBuyChange(), onTradingModeChange(), openSettingsGroup(), pingBrokerStatus() (+25 more)

### Community 43 - "settings.py"
Cohesion: 0.11
Nodes (18): KILL_SWITCH_ACTIVE interlock, MAX_DAILY_LOSS_EUR breaker, Audit plan (aitrader), Already there: ATR stop, daily breaker, regime filter, Lock design: _monitor_lock / _t212_lock contention, Scheduler misfire_grace_time, Missing consensus score default bypasses minimum, NaN/non-finite price guards (+10 more)

### Community 44 - "test_user_holdings_guards.py"
Cohesion: 0.07
Nodes (12): asml(), rest(), RestingFake, clean_snapshots(), fake(), FakeT212, test_create_trigger_refuses_mine_ticker(), test_execute_buy_refuses_mine_ticker_manual_and_auto() (+4 more)

### Community 45 - "datetime"
Cohesion: 0.14
Nodes (18): check_market_hours(), _eu_session(), get_market_statuses(), is_eu_ticker(), quotes_moving(), us_session_local(), test_eu_session_unaffected(), test_market_statuses_follow_exchange_timezone() (+10 more)

### Community 46 - "levels.py"
Cohesion: 0.11
Nodes (19): effective_breakeven_pct(), max_hold_exceeded(), _naive_utc(), pct_trail_stop(), plan_entry(), _positive_number(), test_trailing_presets_do_not_lock_breakeven_before_their_trail(), test_max_hold_exceeded() (+11 more)

### Community 47 - "web/routes.py"
Cohesion: 0.11
Nodes (21): get_holdings(), get_job(), get_latest_market_digest(), get_learning_insights(), get_main_rules_status(), get_market_status(), get_portfolio(), get_portfolio_history() (+13 more)

### Community 48 - "test_research_strategy.py"
Cohesion: 0.10
Nodes (19): anonymize_text(), apply_risk_budget(), check_market_regime(), compute_trailing_stop(), regime_index_for_ticker(), test_breakeven_lock_default_trigger_is_still_plus_one_percent(), test_breakeven_lock_never_lowers_the_stop_and_atr_trail_still_applies(), _committee_patches() (+11 more)

### Community 49 - "shadow_learner.py"
Cohesion: 0.10
Nodes (15): _avg(), compute_stats(), _guard(), _is_reversal(), _locked_reversals(), _r_multiple(), _ranges(), _result() (+7 more)

### Community 50 - "switchTab"
Cohesion: 0.10
Nodes (30): analyzeDirect(), closeMobileDropdown(), filterScreener(), inspectReport(), moreIsSettingsOnly(), moreTabs(), replayClass(), resumePendingJob() (+22 more)

### Community 51 - "re"
Cohesion: 0.15
Nodes (19): _html(), test_app_js_position_actions(), test_banner_before_total_value(), test_home_ids_present(), test_kill_switch_button_hooks(), test_settings_shortcuts_match_a_fold(), _client(), _html() (+11 more)

### Community 52 - "test_committee_jobs.py"
Cohesion: 0.16
Nodes (17): _tickers_in_use(), _report(), _run(), test_analyze_runs_as_a_job(), calls(), test_arm_reuses_a_recent_debate_report_but_not_a_stale_one(), calls(), test_blocked_arm_job_carries_the_gate_detail() (+9 more)

### Community 53 - "enabledPlugins"
Cohesion: 0.10
Nodes (25): source, source, source, enabledPlugins, caveman@caveman, commit-commands@claude-plugins-official, ponytail@ponytail, pr-review-toolkit@claude-plugins-official (+17 more)

### Community 54 - "test_entry_sizing.py"
Cohesion: 0.11
Nodes (17): calculate_position_allocation(), entry_allocation_from_summary(), new_entry_allocation(), risk_capped_allocation(), test_alert_dashboard_and_buy_agree_and_leftover_cash_moves_on(), arm(), test_entry_allocation_reads_a_portfolio_summary(), test_eur_capital_cap_counts_invested_money() (+9 more)

### Community 55 - "calculate_broker_fee"
Cohesion: 0.14
Nodes (13): calculate_broker_fee(), calculate_round_trip_fee(), evaluate_trade_economic_viability(), get_min_viable_order_eur(), fee_for(), _fee(), _t212(), test_eur_stock_free() (+5 more)

### Community 56 - "cash_flows.py"
Cohesion: 0.12
Nodes (13): add_cash_flow(), broker_flow_amount(), delete_cash_flow(), detect_flow(), list_cash_flow_points(), list_cash_flows(), live_history_start(), _parse_utc() (+5 more)

### Community 57 - "test_user_holdings.py"
Cohesion: 0.20
Nodes (19): backfill_user_holdings(), _bf_clean(), _bf_live(), _bf_seed(), _clean(), _pos(), _refresh(), _row() (+11 more)

### Community 58 - "showToast"
Cohesion: 0.15
Nodes (25): closePromoteDialog(), closeShadowEditor(), confirmPromote(), copyShadow(), deleteShadow(), formatRuleValue(), learnShadow(), loadMainRulesBanner() (+17 more)

### Community 59 - "Shadow Learning, Outcome Memory, Promote"
Cohesion: 0.17
Nodes (20): Shadow Learning, Outcome Memory, Promote to Main: Implementation Plan, Docs and final verification (CLAUDE.md, AGENTS.md), Main entry behaviour under promoted rules, Main exit behaviour (paper and live) by rules_version, Main closes write outcomes; cold-start excludes shadow outcomes; pruning, Main rule set resolver (MainRules, get_main_rules, write/revert, rules_version), Outcome atoms (record_outcome, search_outcomes), MAIN_LEARNER_PAUSED pauses automatic calibration (+12 more)

### Community 60 - "Shadow learning, outcome memory, promote"
Cohesion: 0.14
Nodes (17): Shadow learning, outcome memory, promote to main: design, apply_param_change, ignore_risk_veto param, Learning strength slider (off/low/medium/high), MAIN_LEARNER_PAUSED, Main rule set (get_main_rules / MainRules), Outcome memory atoms (record_outcome), Outcome atom pruning (500 per strategy + monthly summary) (+9 more)

### Community 61 - "test_audit_outcomes.py"
Cohesion: 0.14
Nodes (15): delete_shadow_outcomes(), record_main_outcome(), _add_sells(), clean_outcomes(), live_mode(), _n(), _pos(), test_delete_shadow_outcomes_never_raises() (+7 more)

### Community 62 - "test_audit_learner.py"
Cohesion: 0.21
Nodes (18): TradeLesson, test_history_recap_uses_lesson_and_handles_null_pnl_pct(), _add_lesson(), _add_sells(), audit_db(), _calibrate_by_llm(), _calibrate_by_rules(), _rows() (+10 more)

### Community 63 - "test_settings_persistence.py"
Cohesion: 0.13
Nodes (9): Settings, update_env_file(), test_kill_switch_persists_to_system_config(), test_resting_orders_switch_cancels_orders_when_turned_off(), test_score_auto_tune_and_trigger_score_round_trip(), test_settings_persistence_cycle(), test_startup_clamps_stored_threshold_and_max_positions(), test_unknown_live_broker_fails_at_startup() (+1 more)

### Community 64 - "Any"
Cohesion: 0.17
Nodes (4): fmt_date_nl(), fmt_eur(), fmt_num(), fmt_pct()

### Community 65 - "test_trade_stats.py"
Cohesion: 0.16
Nodes (13): _mean(), _r_multiple(), summarize_pct(), summarize_r(), _too_few(), track_record_block(), _all_trades_n(), test_paper_trigger_stores_report_id_and_position_links_trigger() (+5 more)

### Community 66 - "test_ticker_detail.py"
Cohesion: 0.21
Nodes (17): _FakeT212, _get(), _held(), _position(), _t212(), test_breakeven_hidden_once_stop_reaches_entry(), test_breakeven_uses_promoted_rules_for_positions_opened_under_them(), fake_rules() (+9 more)

### Community 67 - "Report: Link outcomes to verdicts before"
Cohesion: 0.11
Nodes (14): Live-money safety skill, Live orders need TRADING_MODE=LIVE and LIVE_TRADING_ENABLED, Money-path test list, Order size caps (WHOLE_SHARES_ONLY, MAX_POSITIONS, MAX_POSITION_PCT), Report: Link outcomes to verdicts before memory pays, Item 2: triple-barrier benchmark-relative outcome labeler, Item 4: live post-mortems and is_live on TradeLesson, Item 5: numeric track-record loadout block (+6 more)

### Community 68 - "test_broker_tradability.py"
Cohesion: 0.17
Nodes (14): _report(), _rows(), _stock(), StubClient, t212(), test_empty_instrument_list_fails_open(), test_manual_dashboard_buy_is_refused_before_committee_and_broker(), test_manual_telegram_buy_is_refused_before_committee_and_broker() (+6 more)

### Community 69 - "test_technicals.py"
Cohesion: 0.13
Nodes (11): fake_yahoo(), _history(), _pandas_reference(), _scan_with(), test_adx_separates_a_trend_from_chop(), test_atr_pct_is_atr_over_price(), test_chop_filter_can_be_switched_off(), test_non_finite_quote_gives_no_data() (+3 more)

### Community 70 - "get_trigger_min_score"
Cohesion: 0.14
Nodes (13): get_trigger_min_score(), _arm_from_ai(), BuyRequest, _calculate_safe_allocation(), create_trigger(), create_trigger_from_ai(), CreateTriggerFromAIRequest, CreateTriggerRequest (+5 more)

### Community 71 - "MarketScanner"
Cohesion: 0.15
Nodes (9): MarketScanner, bench_intel(), bench_risk(), run(), bench_signal(), run(), _median_us(), test_scanner_rewards_stocks_near_their_52_week_high() (+1 more)

### Community 72 - "Python requirements"
Cohesion: 0.11
Nodes (18): Python requirements, aiosqlite, alpaca-py, apscheduler, fastapi, google-genai, httpx, numpy (+10 more)

### Community 73 - "test_risk_invariants.py"
Cohesion: 0.17
Nodes (14): _buy(), _no_clock(), test_allocation_is_zero_for_bad_cash(), test_bad_atr_still_gives_the_breakeven_stop(), test_bad_quote_neither_fires_nor_cancels_a_trigger(), test_bad_trigger_levels_are_invalid(), test_buy_with_bad_number_is_rejected(), test_flash_crash_gap_through_stop_cancels_instead_of_buying() (+6 more)

### Community 74 - "test_currency.py"
Cohesion: 0.16
Nodes (8): CurrencyService, _svc(), test_eur_and_usd(), test_event_loop_never_waits_for_an_fx_fetch(), test_failed_fetch_backs_off(), test_other_currencies_fetched(), test_pence_divided_by_100(), test_unknown_currency_falls_back()

### Community 77 - "market.py"
Cohesion: 0.14
Nodes (4): compute_technicals(), _ema_last(), test_flat_series_has_neutral_rsi_and_zero_adx(), test_market_data_fetches_enough_history_for_a_200_day_sma()

### Community 78 - "loadPortfolio"
Cohesion: 0.19
Nodes (18): addCashFlow(), closePosition(), eur(), executePaperBuy(), isLiveMode(), loadCashFlows(), loadPortfolio(), loadValueHistory() (+10 more)

### Community 79 - "test_shadow_api.py"
Cohesion: 0.13
Nodes (5): client(), _snapshot(), test_patch_is_all_or_nothing(), test_patch_label_and_params_saved_with_one_history_row(), test_patch_label_truncated_to_60()

### Community 80 - "LLMClient"
Cohesion: 0.19
Nodes (3): LLM Path (score_source llm), LLMClient, test_extract_json_returns_only_dicts()

### Community 81 - "post"
Cohesion: 0.12
Nodes (9): calibrate_learning_parameters(), learn_shadow(), reconnect_broker(), refresh_market_data(), reset_shadow(), resume_main_learner(), revert_main_rules_route(), toggle_killswitch() (+1 more)

### Community 82 - "loadTriggers"
Cohesion: 0.17
Nodes (17): armAITrigger(), armTriggerFlow(), armTriggerFromInput(), cancelTrigger(), clearTriggerFeedback(), confirmArmTrigger(), hasNum(), loadTriggers() (+9 more)

### Community 83 - "conftest.py"
Cohesion: 0.17
Nodes (7): reset_strategy_cache(), _finish_live_learner_tasks(), _no_real_env_writes(), _reset_shadow_strategy_cache(), setup_test_db(), _init(), _telegram_owner_chat()

### Community 84 - "test_cash_flows.py"
Cohesion: 0.31
Nodes (12): CashFlow, net_cash_flows_eur(), _check_api(), _check_deposit_flow(), _cleanup(), _fresh_live_history(), _snap(), test_cash_flow_api_add_and_remove() (+4 more)

### Community 85 - "TradeLearner"
Cohesion: 0.16
Nodes (4): Learning loop (TradeLearner calibration), Item 3: statistically honest learner (expectancy in R, CI trigger), Three Parallel Learning Stores, TradeLearner

### Community 87 - "holdings.py"
Cohesion: 0.25
Nodes (10): holding_dict(), holdings_enabled(), holdings_value_eur(), is_mine(), list_holdings(), mine_tickers(), _reanchor(), refresh_holding_prices() (+2 more)

### Community 89 - "BaseModel"
Cohesion: 0.13
Nodes (10): add_cash_flow(), CashFlowRequest, manual_sell(), MarketSessionRequest, OwnerRequest, run_market_session_endpoint(), SellRequest, set_holding_owner() (+2 more)

### Community 90 - "Ponytail-audit skill"
Cohesion: 0.19
Nodes (9): Ponytail-audit skill, Lean categories: delete, reuse, stdlib, yagni, merge, split, Ponytail-debt skill (shortcut ledger), no-trigger rot tag, Ponytail-review skill, Four-part finding format, Ponytail skill (lazy senior dev mode), Ponytail levels lite/full/ultra (+1 more)

### Community 91 - "Resting Trading 212 Orders Implementatio"
Cohesion: 0.23
Nodes (10): Resting Trading 212 Orders Implementation Plan, BREAKOUT_LIMIT_BUFFER_PCT (default 0.5), Cancel resting buys on kill switch and trigger cancel, DAY entry orders resting at broker, Split execute_buy into _plan_live_buy and _book_live_fill, Trading212Client.place_stop_limit, LiveBroker.sync_resting_entries, t212_order_sync scheduler job (monitor_resting_orders, 1 min) (+2 more)

### Community 92 - "My Holdings Implementation Plan"
Cohesion: 0.25
Nodes (12): My Holdings Implementation Plan, One-time startup backfill_user_holdings, Home tab My holdings list and Bot | Mine slider, LiveBroker._cancel_trigger_locked, Holdings API (GET /api/holdings, POST /api/holdings/{ticker}/owner), holdings_enabled() gate (live Trading 212 only), src/trading/holdings.py helpers (mine_tickers, is_mine, list_holdings), Guards: no buy, trigger or scan on a Mine ticker (+4 more)

### Community 94 - "TradingScheduler"
Cohesion: 0.23
Nodes (7): TradingScheduler, test_us_cron_jobs_use_new_york_timezone(), run(), test_scheduler_crons_only_mon_to_fri(), test_scheduler_check_agrees_with_the_rule(), test_scheduler_registers_t212_order_sync(), test_trigger_arming_checks_one_share_fits()

### Community 95 - "asyncio"
Cohesion: 0.16
Nodes (9): _latest_live_cash(), _t212_values(), test_live_cash_sync_adds_no_snapshot_when_cash_is_unchanged(), test_live_cash_sync_keeps_last_balance_when_broker_is_disconnected(), test_live_cash_sync_records_broker_cash_and_keeps_unlisted_positions_open(), test_manual_refresh_never_overlaps_the_scheduled_trigger_check(), test_market_data_refresh_endpoint_reports_the_refresh_result(), test_trigger_job_still_checks_triggers_when_balance_sync_fails() (+1 more)

### Community 96 - "My Holdings UI mockup"
Cohesion: 0.26
Nodes (13): My Holdings UI mockup, Segmented allocation bar: Bot / Yours (hatched) / Cash, Bot / Mine ownership segmented toggle, Bot position detail: Entry, Now, Stop, Target, Bot positions section (count badge), Dark-theme mobile single-column card layout, Expandable holding row: ticker tile, value, % change, chevron, My holding detail: Avg price, Now, P&L (+5 more)

### Community 97 - "features.js"
Cohesion: 0.24
Nodes (8): applyModules(), isEnabled(), load(), renderToggles(), resolveModules(), setEnabled(), allOff, require

### Community 98 - "Shadow exit tests, trigger cancel reason"
Cohesion: 0.32
Nodes (10): Shadow exit tests, trigger cancel reasons, numeric track record Implementation Plan, ShadowStrategy.breakeven_r, Committee prompt: numeric track record replaces cold-start memory and lessons, effective_breakeven_pct helper, Shadow presets breakeven_1r, no_breakeven, score_76, OrderTrigger.cancel_reason, Production DB evidence (23 trades, 39% win, 14/18 triggers cancelled), OrderTrigger.report_id and Position.trigger_id linking (+2 more)

### Community 99 - "My holdings (hands-off positions) - desi"
Cohesion: 0.29
Nodes (10): My holdings (hands-off positions) - design, GET /api/holdings and POST /api/holdings/{ticker}/owner, src/trading/holdings.py helpers (mine_tickers, is_mine, holdings_value_eur, list_holdings), Holdings sync in _sync_t212_locked, Mine (hands-off) holding, Bot | Mine slider (.own-switch), LiveBroker.release_to_user (Bot -> Mine), LiveBroker.take_from_user (Mine -> Bot) (+2 more)

### Community 100 - "SystemConfig"
Cohesion: 0.21
Nodes (8): SystemConfig, bench_db(), run(), idle_decay_db(), paused_learner(), test_learner_recalibrates_only_after_enough_closed_trades(), test_trigger_min_score_prefers_the_saved_setting(), set_saved()

### Community 102 - "_step"
Cohesion: 0.21
Nodes (6): _int_step(), _normalise_weights(), _step(), test_step_ignores_a_non_finite_proposal(), test_step_leaves_a_value_outside_its_range_alone(), test_weight_pushed_past_its_range_by_normalising_still_moves()

### Community 103 - "Backtest discipline skill"
Cohesion: 0.20
Nodes (5): Backtest discipline skill, Benchmark vs buy-and-hold after costs, Survivorship bias, Outcome embargo (time integrity), Paired memory-on/off A/B test

### Community 104 - "Resting orders at Trading 212 - design"
Cohesion: 0.25
Nodes (6): Resting orders at Trading 212 - design, BREAKOUT_LIMIT_BUFFER_PCT setting, Breakout Stop-Limit Buy, Demo account checks before live, t212_order_sync job (1 min), T212_RESTING_ORDERS setting

### Community 105 - "_call_upstream_llm"
Cohesion: 0.27
Nodes (4): anthropic_messages_proxy(), _call_upstream_llm(), _content_to_text(), openai_chat_completions_proxy()

### Community 107 - "test_user_holdings_api.py"
Cohesion: 0.31
Nodes (9): _call(), enabled(), test_get_holdings_empty_in_paper_mode(), test_owner_bot_calls_take(), test_owner_broker_exception_returns_409(), test_owner_feature_off_returns_409(), test_owner_mine_calls_release(), test_owner_rejects_bad_value() (+1 more)

### Community 108 - "Skills environment"
Cohesion: 0.29
Nodes (8): Cline caveman rule, Copilot caveman instructions, OpenCode caveman AGENTS block, Windsurf caveman rule (always_on), Skills environment, Claude Code skills environment doc, Enabled-by-default plugins (superpowers, caveman, ponytail, security-guidance...), Graphify code knowledge graph

### Community 109 - "_shadow_call"
Cohesion: 0.20
Nodes (8): copy_shadow(), create_shadow(), edit_shadow(), _shadow_call(), ShadowCopy, ShadowCreate, ShadowPatch, undo_shadow_change()

### Community 110 - "CLAUDE.md project guide"
Cohesion: 0.22
Nodes (6): AGENTS.md agent instructions, Query TencentDB CodeGraph for symbols, CLAUDE.md project guide, Broker selection (active_broker proxy), Decision pipeline, Web access security

### Community 111 - "server.py"
Cohesion: 0.44
Nodes (4): main(), create_memory_app(), lifespan(), main()

### Community 112 - "WatchlistItem"
Cohesion: 0.22
Nodes (7): WatchlistItem, add_watchlist(), cancel_trigger(), delete_shadow(), remove_cash_flow(), remove_watchlist(), WatchlistRequest

### Community 113 - "Ticker detail sheet: design"
Cohesion: 0.42
Nodes (8): Ticker detail sheet: design, breakeven_at level, MarketDataProvider.get_price_history, Inline Chart.js level-lines plugin, level_sets (position/trigger/report/shadow), GET /api/ticker/{ticker}/detail, tickerLink helper / open-ticker action, <dialog id=tickerSheet> full-screen sheet

### Community 114 - "monitor_resting_orders"
Cohesion: 0.25
Nodes (5): _fake_broker(), test_monitor_resting_orders_books_and_notifies(), test_monitor_resting_orders_noop_when_flag_off(), test_monitor_resting_orders_swallows_errors(), test_startup_cancels_resting_orders_when_not_synced()

### Community 115 - "_run"
Cohesion: 0.31
Nodes (4): _Proc, _run(), test_positive_exit_not_retried(), test_signal_death_retried()

### Community 116 - "Ticker detail sheet: implementation prog"
Cohesion: 0.43
Nodes (7): Ticker detail sheet: implementation progress, Final gate: fresh ponytail and design reviews, Shadow summary row ids, Trading 212 currentPrice for held live tickers (price_source), /api/ticker/{ticker}/detail endpoint and get_price_history, Ticker detail sheet frontend (dialog, chart, level plugin), tickerLink wired into lists

### Community 118 - "broker_can_buy"
Cohesion: 0.25
Nodes (5): broker_can_buy(), test_alpaca_or_no_client_is_never_checked(), test_cfd_only_and_unlisted_cannot_be_bought(), test_stock_and_etf_match_can_be_bought(), test_two_matches_are_ambiguous()

### Community 119 - "_plan_trigger"
Cohesion: 0.29
Nodes (3): _old_levels(), test_arm_trigger_from_report_accepts_a_plan_or_computes_its_own(), test_plan_trigger_matches_the_previous_calculation()

### Community 120 - "Unslop UI design skill (anti-slop rules"
Cohesion: 0.29
Nodes (6): Unslop UI design skill (anti-slop rules), Use a design system of tokens, Inner radius = outer radius - padding, Loading states (skeletons, spinners, optimistic UI), One emphasized primary CTA, WCAG AA text contrast

### Community 124 - "check_broker_status"
Cohesion: 0.29
Nodes (3): check_broker_status(), get_broker_status(), ping_broker()

### Community 125 - "test_broker_status.py"
Cohesion: 0.48
Nodes (5): test_api_broker_ping_endpoint(), test_api_broker_status_endpoint(), test_cached_status_reports_current_kill_switch(), test_live_broker_connection_check_graceful_handling(), test_paper_broker_connection_check()

### Community 126 - "_start_job"
Cohesion: 0.40
Nodes (4): _analyze(), analyze_ticker(), AnalyzeRequest, _start_job()

### Community 127 - "_arm_two_triggers_for_one_free_slot"
Cohesion: 0.47
Nodes (4): _arm_two_triggers_for_one_free_slot(), _cleanup(), test_paper_adjust_funds_the_best_trigger_and_zeroes_the_weaker_one(), test_unfunded_trigger_does_not_attempt_a_buy_when_its_price_is_hit()

### Community 129 - "get_ticker_detail"
Cohesion: 0.50
Nodes (4): get_ticker_detail(), _iso(), _level_set(), _r2()

### Community 132 - "promote_shadow"
Cohesion: 0.50
Nodes (3): _promote_call(), promote_shadow(), PromoteRequest

## Knowledge Gaps
- **65 isolated node(s):** `Benchmark vs buy-and-hold after costs`, `Order size caps (WHOLE_SHARES_ONLY, MAX_POSITIONS, MAX_POSITION_PCT)`, `Money-path test list`, `Ponytail levels lite/full/ultra`, `Four-part finding format` (+60 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 895 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **22 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `LiveBroker` connect `LiveBroker` to `Any`, `test_t212_resting_orders.py`, `_t212_ambiguous`, `test_main_parity_exit.py`, `Position`, `main_strategy.py`, `Measuring Memory/Learning Gains and Thei`, `Internal Code Audit: Agent-Memory Subsys`, `Trade`, `test_trading212_broker.py`, `live.py`, `PortfolioSnapshot`, `UserHolding`, `settings.py`, `test_user_holdings_guards.py`, `Shadow learning, outcome memory, promote`, `Report: Link outcomes to verdicts before`, `execute_sell`, `conftest.py`, `refresh_positions_pnl`, `Resting Trading 212 Orders Implementatio`, `My Holdings Implementation Plan`, `My holdings (hands-off positions) - desi`, `SystemConfig`, `Resting orders at Trading 212 - design`, `CLAUDE.md project guide`?**
  _High betweenness centrality (0.109) - this node is a cross-community bridge._
- **Are the 11 inferred relationships involving `LiveBroker` (e.g. with `OrderTrigger` and `PortfolioSnapshot`) actually correct?**
  _`LiveBroker` has 11 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Benchmark vs buy-and-hold after costs`, `Order size caps (WHOLE_SHARES_ONLY, MAX_POSITIONS, MAX_POSITION_PCT)`, `Money-path test list` to the rest of the system?**
  _65 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `test_trigger_gate.py` be split into smaller, more focused modules?**
  _Cohesion score 0.07572861920688008 - nodes in this community are weakly interconnected._
- **Why does `SystemConfig` connect `SystemConfig` to `test_trigger_gate.py`, `get_ticker_detail`, `asyncio`, `test_t212_resting_orders.py`, `TelegramAssistant`, `test_main_parity_exit.py`, `shadow.py`, `_isolated`, `main_strategy.py`, `init_db`, `LiveBroker`, `Measuring Memory/Learning Gains and Thei`, `test_telegram_trigger_gate.py`, `ShadowPortfolios`, `test_promote.py`, `get_main_rules`, `write_main_rules`, `db/models.py`, `PaperBroker`, `test_agent_robustness.py`, `Internal Code Audit: Agent-Memory Subsys`, `test_trade_logic_fixes.py`, `Trade`, `live.py`, `test_shadow.py`, `settings.py`, `web/routes.py`, `test_research_strategy.py`, `calculate_broker_fee`, `cash_flows.py`, `test_user_holdings.py`, `test_audit_learner.py`, `test_settings_persistence.py`, `get_trigger_min_score`, `test_cash_flows.py`, `TradeLearner`, `holdings.py`, `BaseModel`, `TradingScheduler`, `_step`?**
  _High betweenness centrality (0.078) - this node is a cross-community bridge._
- **Are the 66 inferred relationships involving `Position` (e.g. with `_tickers_in_use()` and `TradingScheduler`) actually correct?**
  _`Position` has 66 INFERRED edges - model-reasoned connections that need verification._
- **Should `Any` be split into smaller, more focused modules?**
  _Cohesion score 0.04660493827160494 - nodes in this community are weakly interconnected._