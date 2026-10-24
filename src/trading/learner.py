import logging
import datetime
from typing import Dict, Any, List, Optional
from sqlalchemy import select, desc, func

from config.settings import settings
from src.db.session import AsyncSessionLocal
from src.db.models import Trade, Position, AnalysisReport, TradeLesson, SystemConfig
from src.agents.base import llm_client

logger = logging.getLogger(__name__)

class TradeLearner:
    """
    Self-Reflective Trading Intelligence Engine.
    Performs trade post-mortems on wins and losses, discovers where things
    went wrong and what could have gone better, and autonomously tunes
    internal risk and committee parameters.
    """

    async def conduct_trade_post_mortem(self, trade_id: int) -> Dict[str, Any]:
        """
        Conducts a post-mortem on a closed trade, determining what succeeded or failed,
        and generates an actionable lesson learned.
        """
        async with AsyncSessionLocal() as session:
            # 1. Fetch closed trade
            res = await session.execute(select(Trade).where(Trade.id == trade_id))
            trade = res.scalars().first()
            if not trade or trade.order_type != "SELL":
                logger.warning(f"Post-mortem requested for non-sell or missing trade #{trade_id}")
                return {"success": False, "message": "Trade not found or not a closed sell."}

            # Check if post-mortem already exists
            existing_lesson_res = await session.execute(
                select(TradeLesson).where(TradeLesson.trade_id == trade_id)
            )
            if existing_lesson_res.scalars().first():
                logger.info(f"Post-mortem already exists for trade #{trade_id}")
                return {"success": True, "message": "Post-mortem already recorded."}

            ticker = trade.ticker.upper()
            pnl_eur = trade.realized_pnl_eur or 0.0
            pnl_pct = trade.realized_pnl_pct or 0.0
            exit_reason = trade.rationale or "Manual Exit"

            # 2. Fetch corresponding position and buy history
            pos_res = await session.execute(
                select(Position).where(Position.ticker == ticker).order_by(desc(Position.opened_at))
            )
            pos = pos_res.scalars().first()

            holding_hours = 0.0
            highest_price = trade.price_native
            entry_price = trade.price_native
            if pos:
                entry_price = pos.entry_price_native
                highest_price = pos.highest_price_native or entry_price
                if pos.opened_at and trade.executed_at:
                    delta = trade.executed_at - pos.opened_at
                    holding_hours = round(delta.total_seconds() / 3600.0, 1)

            max_runup_pct = round(((highest_price / entry_price) - 1.0) * 100, 2) if entry_price > 0 else 0.0

            # 3. Fetch original analysis report for committee conviction & context
            rep_res = await session.execute(
                select(AnalysisReport).where(AnalysisReport.ticker == ticker).order_by(desc(AnalysisReport.created_at))
            )
            report = rep_res.scalars().first()
            score = report.consensus_score if report else 75
            tech_bias = report.technical_summary if report else ""
            risk_summary = report.risk_summary if report else ""

            # 4. Classify outcome
            if pnl_pct > 0.4:
                outcome = "WIN"
            elif pnl_pct < -0.4:
                outcome = "LOSS"
            else:
                outcome = "BREAKEVEN"

            # 5. Diagnostic Evaluation (Deterministic Rules)
            what_went_well = ""
            what_went_wrong = ""
            lesson = ""

            if outcome == "LOSS":
                # Diagnosing where things went wrong
                is_stop_loss = "stop-loss" in exit_reason.lower() or "stop" in exit_reason.lower()
                
                if is_stop_loss and holding_hours <= 24.0:
                    what_went_wrong = (
                        f"Premature stop-out: Stop-loss was triggered in only {holding_hours}h at {trade.price_native}. "
                        f"The stop buffer or ATR multiplier was too tight to absorb normal intraday volatility."
                    )
                    what_went_well = f"Risk discipline executed properly: Loss was contained to {pnl_pct:+.2f}% (€{pnl_eur:+.2f})."
                    lesson = (
                        f"Widen ATR stop multiplier on {ticker} to at least 1.9x–2.1x to prevent stop-hunting and premature shake-outs."
                    )
                elif is_stop_loss and max_runup_pct >= 4.0:
                    what_went_wrong = (
                        f"Profit giveback: Position ran up +{max_runup_pct}% toward take-profit before collapsing back into stop-loss. "
                        f"Trailing stop was not ratcheted up aggressively enough to protect gains."
                    )
                    what_went_well = f"Entry timing was strong (reached +{max_runup_pct}% gain)."
                    lesson = (
                        f"Ratchet trailing stop to breakeven + 1% as soon as unrealized gain exceeds +4.0%."
                    )
                elif score < 75:
                    what_went_wrong = (
                        f"Low conviction entry: Entered trade with marginal committee consensus score of {score}/100. "
                        f"Setup lacked institutional edge and failed under selling pressure."
                    )
                    what_went_well = f"Stop-loss prevented deeper portfolio drawdown."
                    lesson = (
                        f"Raise minimum consensus score threshold to 74+ for automated entries in volatile conditions."
                    )
                else:
                    what_went_wrong = (
                        f"Price failed to maintain upward momentum and breached support. Exit reason: {exit_reason}."
                    )
                    what_went_well = f"System adhered to strict risk controls; loss was capped at {pnl_pct:+.2f}%."
                    lesson = (
                        f"Incorporate stricter volume and support confirmation before entry on {ticker}."
                    )

            elif outcome == "WIN":
                # Diagnosing what succeeded
                is_take_profit = "take-profit" in exit_reason.lower() or "target" in exit_reason.lower()
                
                if is_take_profit or pnl_pct >= 4.0:
                    what_went_well = (
                        f"Take-profit target achieved (+{pnl_pct:+.2f}% / €{pnl_eur:+.2f}) after {holding_hours}h. "
                        f"Committee thesis (Conviction: {score}/100) and entry trigger level executed cleanly."
                    )
                    what_went_wrong = "None — setup delivered as planned."
                    lesson = (
                        f"High conviction setups (Score {score}/100) with technical alignment offer reliable edge."
                    )
                else:
                    what_went_well = (
                        f"Profitable exit (+{pnl_pct:+.2f}% / €{pnl_eur:+.2f}). Trailing risk management secured gains."
                    )
                    what_went_wrong = "Position exited before hitting primary target, leaving modest upside."
                    lesson = (
                        f"Locking in gains during consolidation protects capital while compounding return."
                    )
            else:
                what_went_well = "Breakeven exit protected capital with negligible slippage."
                what_went_wrong = "Momentum stalled after entry, failing to break out."
                lesson = "Require confirmed volume surge (>1.2x) on breakout setups to avoid flat consolidation."

            # 6. Optional LLM Reflection Enrichment
            if llm_client.has_active_key():
                prompt = (
                    f"Analyze this completed trade for trading bot self-improvement:\n"
                    f"Ticker: {ticker}\n"
                    f"Outcome: {outcome} ({pnl_pct:+.2f}%, €{pnl_eur:+.2f})\n"
                    f"Holding Time: {holding_hours} hours\n"
                    f"Exit Reason: {exit_reason}\n"
                    f"Initial Committee Score: {score}/100\n"
                    f"Max Run-up: +{max_runup_pct}%\n\n"
                    f"Return JSON with:\n"
                    f'{{"what_went_well": string, "what_went_wrong": string, "lesson": string}}'
                )
                try:
                    llm_res = await llm_client.generate_json(
                        "You are a Senior Trading Performance Coach conducting a rigorous trade post-mortem.",
                        prompt
                    )
                    if llm_res and "lesson" in llm_res and len(llm_res["lesson"]) > 10:
                        what_went_well = llm_res.get("what_went_well", what_went_well)
                        what_went_wrong = llm_res.get("what_went_wrong", what_went_wrong)
                        lesson = llm_res["lesson"]
                except Exception as e:
                    logger.debug(f"LLM post-mortem enrichment fallback: {e}")

            # 7. Record Lesson in Database
            trade_lesson = TradeLesson(
                trade_id=trade.id,
                ticker=ticker,
                outcome=outcome,
                realized_pnl_eur=round(pnl_eur, 2),
                realized_pnl_pct=round(pnl_pct, 2),
                exit_reason=exit_reason,
                holding_period_hours=holding_hours,
                what_went_well=what_went_well,
                what_went_wrong=what_went_wrong,
                lesson=lesson,
                created_at=datetime.datetime.utcnow()
            )
            session.add(trade_lesson)
            await session.commit()
            await session.refresh(trade_lesson)

            logger.info(f"Recorded post-mortem #{trade_lesson.id} for {ticker} ({outcome}): {lesson}")
            return {
                "success": True,
                "lesson_id": trade_lesson.id,
                "ticker": ticker,
                "outcome": outcome,
                "pnl_pct": pnl_pct,
                "lesson": lesson
            }

    async def calibrate_internal_parameters(self, trade_id: Optional[int] = None, automatic: bool = False) -> Dict[str, Any]:
        """
        Spins up an autonomous Strategy & Risk Calibration Agent to review historical
        win/loss patterns, latest closed trade diagnostics, and adaptive lessons,
        and dynamically calibrate internal trading parameters.

        automatic=True (the call after each exit) only recalibrates once LEARNER_MIN_TRADES trades have
        closed since the last calibration: a few outcomes are mostly noise, and tuning on them overfits.
        Manual calibration (dashboard, Telegram) always runs.
        """
        async with AsyncSessionLocal() as session:
            if automatic and settings.LEARNER_MIN_TRADES > 1:
                last_res = await session.execute(select(SystemConfig).where(SystemConfig.key == "LAST_CALIBRATION_TIMESTAMP"))
                last_row = last_res.scalars().first()
                since_query = select(func.count(Trade.id)).where(Trade.order_type == "SELL")
                if last_row and last_row.value:
                    try:
                        since_query = since_query.where(Trade.executed_at > datetime.datetime.fromisoformat(last_row.value))
                    except ValueError:
                        pass
                closed_since = (await session.execute(since_query)).scalar() or 0
                if closed_since < settings.LEARNER_MIN_TRADES:
                    msg = f"Calibration waits for {settings.LEARNER_MIN_TRADES} closed trades ({closed_since} since the last one)."
                    logger.info(msg)
                    return {"success": False, "skipped": True, "message": msg, "adjustments": []}

            # 1. Fetch recent closed trades and lessons (last 20)
            res = await session.execute(
                select(Trade).where(Trade.order_type == "SELL").order_by(desc(Trade.executed_at)).limit(20)
            )
            closed_trades = res.scalars().all()
            if not closed_trades:
                return {"success": False, "message": "No closed trades available for calibration."}

            total_trades = len(closed_trades)
            wins = [t for t in closed_trades if (t.realized_pnl_pct or 0.0) > 0.3]
            losses = [t for t in closed_trades if (t.realized_pnl_pct or 0.0) < -0.3]
            win_count = len(wins)
            loss_count = len(losses)
            win_rate = (win_count / total_trades) if total_trades > 0 else 0.5

            # Calculate consecutive losses from latest trade
            consecutive_losses = 0
            for t in closed_trades:
                if (t.realized_pnl_pct or 0.0) < -0.3:
                    consecutive_losses += 1
                else:
                    break

            # Fetch target trade or latest trade
            target_trade = None
            if trade_id:
                t_res = await session.execute(select(Trade).where(Trade.id == trade_id))
                target_trade = t_res.scalars().first()
            if not target_trade:
                target_trade = closed_trades[0]

            # Fetch corresponding lesson
            lesson_res = await session.execute(
                select(TradeLesson).where(TradeLesson.trade_id == target_trade.id)
            )
            latest_lesson = lesson_res.scalars().first()
            if not latest_lesson:
                fallback_lesson_res = await session.execute(
                    select(TradeLesson).order_by(desc(TradeLesson.created_at)).limit(1)
                )
                latest_lesson = fallback_lesson_res.scalars().first()

            # 2. Fetch current active configs
            cfg_res = await session.execute(select(SystemConfig))
            configs = {c.key: c.value for c in cfg_res.scalars().all()}

            curr_min_score = int(configs.get("MIN_CONSENSUS_SCORE", settings.MIN_CONSENSUS_SCORE))
            curr_trig_score = int(configs.get("TRIGGER_MIN_SCORE", settings.TRIGGER_MIN_SCORE))
            curr_size_pct = float(configs.get("POSITION_SIZE_PCT", settings.POSITION_SIZE_PCT))
            curr_atr_mult = float(configs.get("ATR_STOP_MULTIPLIER", "1.75"))
            curr_tp_rr = float(configs.get("TAKE_PROFIT_RR", "2.1"))
            curr_be_pct = float(configs.get("BREAKEVEN_TRIGGER_PCT", "0.025"))
            curr_w_tech = float(configs.get("WEIGHT_TECHNICAL", "0.35"))
            curr_w_fund = float(configs.get("WEIGHT_FUNDAMENTAL", "0.30"))
            curr_w_sent = float(configs.get("WEIGHT_SENTIMENT", "0.15"))
            curr_w_risk = float(configs.get("WEIGHT_RISK", "0.20"))

            adjustment_reasons = []
            calibrated_params = {}

            # 3. Spin up Autonomous Parameter Calibration Agent via LLM
            agent_calibrated = False
            if llm_client.has_active_key() and latest_lesson:
                system_prompt = (
                    "You are the Chief Quantitative Strategist and Autonomous Risk Tuning Agent for an algorithmic hedge fund. "
                    "Your mandate is to continually calibrate internal system parameters following trade exits to eliminate structural "
                    "flaws identified in trade post-mortems, prevent repeated losses, and maximize risk-adjusted equity growth."
                )
                pnl_pct_val = target_trade.realized_pnl_pct or 0.0
                pnl_eur_val = target_trade.realized_pnl_eur or 0.0
                holding_h_val = latest_lesson.holding_period_hours or 0.0
                user_prompt = f"""
Latest Trade Post-Mortem:
- Ticker: {target_trade.ticker}
- Outcome: {latest_lesson.outcome or 'CLOSED'} (Realized PnL: {pnl_pct_val:+.2f}%, €{pnl_eur_val:+.2f})
- Exit Reason: {target_trade.rationale or 'Exit'}
- Holding Duration: {holding_h_val:.1f} hours
- What Went Well: {latest_lesson.what_went_well or 'N/A'}
- What Went Wrong: {latest_lesson.what_went_wrong or 'None identified'}
- Actionable Lesson: {latest_lesson.lesson or 'N/A'}

Recent Portfolio Performance:
- Total Closed Trades: {total_trades} (Wins: {win_count}, Losses: {loss_count})
- Win Rate: {win_rate*100:.1f}%
- Consecutive Losses: {consecutive_losses}

Current Active Parameters:
- MIN_CONSENSUS_SCORE: {curr_min_score} (Allowed range: 68 - 85)
- TRIGGER_MIN_SCORE: {curr_trig_score} (Allowed range: 72 - 88)
- ATR_STOP_MULTIPLIER: {curr_atr_mult} (Allowed range: 1.75 - 2.75)
- TAKE_PROFIT_RR: {curr_tp_rr} (Allowed range: 1.5 - 3.0)
- BREAKEVEN_TRIGGER_PCT: {curr_be_pct} (Allowed range: 0.015 - 0.05)
- Committee Weights: Technical={curr_w_tech}, Fundamental={curr_w_fund}, Sentiment={curr_w_sent}, Risk={curr_w_risk}

Calibration Directives:
1. If the trade suffered from gap down, slippage, or premature stop-out below technical support (e.g. BMW.DE), widen ATR_STOP_MULTIPLIER (e.g. 2.3x - 2.6x) to provide sufficient volatility buffer.
2. If previous trades achieved unrealized gains before reversing into stop-outs (e.g. DTE.DE / MBG.DE / SU.PA), tighten BREAKEVEN_TRIGGER_PCT (e.g. 0.02 - 0.025 / 2.0% - 2.5%) so trailing stops ratchet to breakeven earlier.
3. If under a losing streak or low win rate, increase MIN_CONSENSUS_SCORE (e.g. 80-82) and TRIGGER_MIN_SCORE (e.g. 82-85) to enforce higher selectivity.
4. If technical momentum failed despite high score, increase WEIGHT_RISK (e.g. 0.25 - 0.30) and reduce WEIGHT_TECHNICAL.
5. Provide actionable, concise bullet points in "adjustment_reasons" explaining each parameter change.

Return valid JSON with exactly these keys:
{{
  "MIN_CONSENSUS_SCORE": int,
  "TRIGGER_MIN_SCORE": int,
  "ATR_STOP_MULTIPLIER": float,
  "TAKE_PROFIT_RR": float,
  "BREAKEVEN_TRIGGER_PCT": float,
  "WEIGHT_TECHNICAL": float,
  "WEIGHT_FUNDAMENTAL": float,
  "WEIGHT_SENTIMENT": float,
  "WEIGHT_RISK": float,
  "adjustment_reasons": [string]
}}
"""
                try:
                    llm_res = await llm_client.generate_json(system_prompt, user_prompt)
                    if llm_res and isinstance(llm_res, dict) and "adjustment_reasons" in llm_res:
                        new_min_score = max(68, min(85, int(llm_res.get("MIN_CONSENSUS_SCORE", curr_min_score))))
                        new_trig_score = max(72, min(88, int(llm_res.get("TRIGGER_MIN_SCORE", curr_trig_score))))
                        new_atr_mult = round(max(1.75, min(2.75, float(llm_res.get("ATR_STOP_MULTIPLIER", curr_atr_mult)))), 2)
                        new_tp_rr = round(max(1.5, min(3.0, float(llm_res.get("TAKE_PROFIT_RR", curr_tp_rr)))), 2)
                        new_be_pct = round(max(0.015, min(0.05, float(llm_res.get("BREAKEVEN_TRIGGER_PCT", curr_be_pct)))), 3)

                        # Weights normalization
                        w_tech = max(0.05, min(0.50, float(llm_res.get("WEIGHT_TECHNICAL", curr_w_tech))))
                        w_fund = max(0.05, min(0.50, float(llm_res.get("WEIGHT_FUNDAMENTAL", curr_w_fund))))
                        w_sent = max(0.05, min(0.25, float(llm_res.get("WEIGHT_SENTIMENT", curr_w_sent))))
                        w_risk = max(0.10, min(0.50, float(llm_res.get("WEIGHT_RISK", curr_w_risk))))
                        total_w = w_tech + w_fund + w_sent + w_risk
                        w_tech = round(w_tech / total_w, 2)
                        w_fund = round(w_fund / total_w, 2)
                        w_sent = round(w_sent / total_w, 2)
                        w_risk = round(1.0 - (w_tech + w_fund + w_sent), 2)

                        calibrated_params = {
                            "min_consensus_score": new_min_score,
                            "trigger_min_score": new_trig_score,
                            "position_size_pct": curr_size_pct,  # position size is set manually, never calibrated
                            "atr_stop_multiplier": new_atr_mult,
                            "take_profit_rr": new_tp_rr,
                            "breakeven_trigger_pct": new_be_pct,
                            "weight_technical": w_tech,
                            "weight_fundamental": w_fund,
                            "weight_sentiment": w_sent,
                            "weight_risk": w_risk,
                            "MIN_CONSENSUS_SCORE": new_min_score,
                            "TRIGGER_MIN_SCORE": new_trig_score,
                            "POSITION_SIZE_PCT": curr_size_pct,  # position size is set manually, never calibrated
                            "ATR_STOP_MULTIPLIER": new_atr_mult,
                            "TAKE_PROFIT_RR": new_tp_rr,
                            "BREAKEVEN_TRIGGER_PCT": new_be_pct,
                            "WEIGHT_TECHNICAL": w_tech,
                            "WEIGHT_FUNDAMENTAL": w_fund,
                            "WEIGHT_SENTIMENT": w_sent,
                            "WEIGHT_RISK": w_risk
                        }
                        raw_reasons = llm_res.get("adjustment_reasons", [])
                        if isinstance(raw_reasons, list) and raw_reasons:
                            adjustment_reasons = [str(r) for r in raw_reasons if r]
                        agent_calibrated = True
                        logger.info(f"Calibration Agent successfully tuned parameters: {calibrated_params}")
                except Exception as e:
                    logger.warning(f"Calibration Agent LLM error, executing rule-based calibration: {e}")

            # 4. Fallback Rule-Based Adaptive Calibration (if LLM not active or fallback needed)
            if not agent_calibrated:
                new_min_score = curr_min_score
                new_trig_score = curr_trig_score
                new_atr_mult = curr_atr_mult
                new_tp_rr = curr_tp_rr
                new_be_pct = curr_be_pct
                w_tech, w_fund, w_sent, w_risk = curr_w_tech, curr_w_fund, curr_w_sent, curr_w_risk

                # A. Conviction Threshold Adjustment
                if consecutive_losses >= 2 or win_rate < 0.40:
                    new_min_score = min(82, max(78, curr_min_score + 1))
                    new_trig_score = min(84, max(80, curr_trig_score + 1))
                    adjustment_reasons.append(f"Losing streak ({consecutive_losses} consecutive): Raised min consensus score to {new_min_score} and trigger score to {new_trig_score} for higher selectivity.")
                elif win_rate >= 0.70 and consecutive_losses == 0 and total_trades >= 5:
                    new_min_score = max(70, min(74, curr_min_score - 2))
                    new_trig_score = max(76, min(80, curr_trig_score - 2))
                    adjustment_reasons.append(f"High win rate ({win_rate*100:.1f}%): Normalized min consensus score to {new_min_score}.")

                # C. Stop Loss ATR Multiplier & Slippage Adjustment
                recent_stop_outs = [t for t in losses[:5] if "stop" in (t.rationale or "").lower()]
                if len(recent_stop_outs) >= 2:
                    new_atr_mult = round(min(2.50, max(curr_atr_mult, curr_atr_mult + 0.15)), 2)
                    adjustment_reasons.append(f"Multiple stop-outs detected: Widened ATR stop multiplier to {new_atr_mult}x for volatility buffer.")

                # D. Profit Preservation / Breakeven Threshold
                new_be_pct = 0.025
                adjustment_reasons.append("Profit preservation priority: Calibrated trailing breakeven trigger to +2.5% gain.")

                # E. Committee Weights Tuning (Emphasize Risk on Drawdowns)
                if consecutive_losses >= 3:
                    w_risk = round(min(0.30, w_risk + 0.05), 2)
                    w_tech = round(max(0.25, w_tech - 0.05), 2)
                    adjustment_reasons.append(f"Defensive regime: Increased Risk Manager weighting to {int(w_risk*100)}% (Technical {int(w_tech*100)}%).")

                calibrated_params = {
                    "min_consensus_score": new_min_score,
                    "trigger_min_score": new_trig_score,
                    "position_size_pct": curr_size_pct,  # position size is set manually, never calibrated
                    "atr_stop_multiplier": new_atr_mult,
                    "take_profit_rr": new_tp_rr,
                    "breakeven_trigger_pct": new_be_pct,
                    "weight_technical": w_tech,
                    "weight_fundamental": w_fund,
                    "weight_sentiment": w_sent,
                    "weight_risk": w_risk,
                    "MIN_CONSENSUS_SCORE": new_min_score,
                    "TRIGGER_MIN_SCORE": new_trig_score,
                    "POSITION_SIZE_PCT": curr_size_pct,  # position size is set manually, never calibrated
                    "ATR_STOP_MULTIPLIER": new_atr_mult,
                    "TAKE_PROFIT_RR": new_tp_rr,
                    "BREAKEVEN_TRIGGER_PCT": new_be_pct,
                    "WEIGHT_TECHNICAL": w_tech,
                    "WEIGHT_FUNDAMENTAL": w_fund,
                    "WEIGHT_SENTIMENT": w_sent,
                    "WEIGHT_RISK": w_risk
                }

            # 5. Persist Calibrated Parameters to SystemConfig
            updated_db_records = {
                "MIN_CONSENSUS_SCORE": str(calibrated_params["MIN_CONSENSUS_SCORE"]),
                "TRIGGER_MIN_SCORE": str(calibrated_params["TRIGGER_MIN_SCORE"]),
                "ATR_STOP_MULTIPLIER": str(calibrated_params["ATR_STOP_MULTIPLIER"]),
                "TAKE_PROFIT_RR": str(calibrated_params["TAKE_PROFIT_RR"]),
                "BREAKEVEN_TRIGGER_PCT": str(calibrated_params["BREAKEVEN_TRIGGER_PCT"]),
                "WEIGHT_TECHNICAL": str(calibrated_params["WEIGHT_TECHNICAL"]),
                "WEIGHT_FUNDAMENTAL": str(calibrated_params["WEIGHT_FUNDAMENTAL"]),
                "WEIGHT_SENTIMENT": str(calibrated_params["WEIGHT_SENTIMENT"]),
                "WEIGHT_RISK": str(calibrated_params["WEIGHT_RISK"]),
                "LAST_CALIBRATION_TIMESTAMP": datetime.datetime.utcnow().isoformat(),
                "LEARNER_WIN_RATE": f"{win_rate*100:.1f}",
                "LEARNER_TOTAL_TRADES": str(total_trades)
            }

            for k, v in updated_db_records.items():
                cfg_item_res = await session.execute(select(SystemConfig).where(SystemConfig.key == k))
                cfg_item = cfg_item_res.scalars().first()
                if cfg_item:
                    cfg_item.value = v
                else:
                    session.add(SystemConfig(key=k, value=v))

            await session.commit()

            # Synchronize in-memory settings
            settings.MIN_CONSENSUS_SCORE = calibrated_params["MIN_CONSENSUS_SCORE"]
            settings.TRIGGER_MIN_SCORE = calibrated_params["TRIGGER_MIN_SCORE"]

            logger.info(f"Calibration completed: {calibrated_params}")
            return {
                "success": True,
                "total_trades": total_trades,
                "win_count": win_count,
                "loss_count": loss_count,
                "win_rate_pct": round(win_rate * 100, 1),
                "consecutive_losses": consecutive_losses,
                "calibrated_parameters": calibrated_params,
                "adjustments": adjustment_reasons
            }

    async def get_learning_context_for_agents(self) -> str:
        """
        Formats historical performance metrics and top lessons learned
        for direct prompt injection into InvestmentCommittee and RiskManagerAgent.
        """
        async with AsyncSessionLocal() as session:
            # Fetch recent lessons
            res = await session.execute(
                select(TradeLesson).order_by(desc(TradeLesson.created_at)).limit(4)
            )
            lessons = res.scalars().all()

            # Fetch active calibrated configs
            cfg_res = await session.execute(select(SystemConfig))
            configs = {c.key: c.value for c in cfg_res.scalars().all()}

            win_rate = configs.get("LEARNER_WIN_RATE", "50.0")
            min_score = configs.get("MIN_CONSENSUS_SCORE", str(settings.MIN_CONSENSUS_SCORE))
            size_pct = configs.get("POSITION_SIZE_PCT", str(settings.POSITION_SIZE_PCT))
            atr_mult = configs.get("ATR_STOP_MULTIPLIER", "1.75")
            tp_rr = configs.get("TAKE_PROFIT_RR", "2.1")
            be_pct = configs.get("BREAKEVEN_TRIGGER_PCT", "0.025")
            w_risk = configs.get("WEIGHT_RISK", "0.20")

            lines = [
                f"=== HISTORICAL TRADE LEARNING & ADAPTED PARAMETERS ===",
                f"• Recent Win Rate: {win_rate}% | Active Adapted Min Score: {min_score}/100",
                f"• Active Risk Sizing: {float(size_pct)*100:.1f}% | Stop ATR Multiplier: {atr_mult}x | TP R:R: {tp_rr}:1",
                f"• Trailing Breakeven Trigger: +{float(be_pct)*100:.1f}% | Committee Risk Weight: {float(w_risk)*100:.0f}%",
            ]

            if lessons:
                lines.append("• Key Lessons Learned from Past Trades:")
                for l in lessons:
                    lines.append(f"  - [{l.outcome} on {l.ticker} ({l.realized_pnl_pct:+.1f}%)]: {l.lesson}")
            else:
                lines.append("• Trade learning engine is active and will ingest closed trades as they occur.")

            return "\n".join(lines)

    async def get_learning_insights(self) -> Dict[str, Any]:
        """
        Returns full structured learning telemetry for the Web UI and Telegram bot.
        """
        async with AsyncSessionLocal() as session:
            # Lessons list
            res = await session.execute(
                select(TradeLesson).order_by(desc(TradeLesson.created_at)).limit(10)
            )
            lessons = res.scalars().all()

            # Configs
            cfg_res = await session.execute(select(SystemConfig))
            configs = {c.key: c.value for c in cfg_res.scalars().all()}

            # Closed trades stats
            trades_res = await session.execute(
                select(Trade).where(Trade.order_type == "SELL").order_by(desc(Trade.executed_at))
            )
            trades = trades_res.scalars().all()
            total_trades = len(trades)
            wins = sum(1 for t in trades if (t.realized_pnl_pct or 0.0) > 0.3)
            losses = sum(1 for t in trades if (t.realized_pnl_pct or 0.0) < -0.3)
            total_realized_pnl = sum((t.realized_pnl_eur or 0.0) for t in trades)
            win_rate = round((wins / total_trades * 100), 1) if total_trades > 0 else 0.0

            return {
                "total_closed_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": win_rate,
                "total_realized_pnl_eur": round(total_realized_pnl, 2),
                "active_parameters": {
                    "min_consensus_score": int(configs.get("MIN_CONSENSUS_SCORE", settings.MIN_CONSENSUS_SCORE)),
                    "trigger_min_score": int(configs.get("TRIGGER_MIN_SCORE", settings.TRIGGER_MIN_SCORE)),
                    "position_size_pct": float(configs.get("POSITION_SIZE_PCT", settings.POSITION_SIZE_PCT)),
                    "atr_stop_multiplier": float(configs.get("ATR_STOP_MULTIPLIER", 1.75)),
                    "take_profit_rr": float(configs.get("TAKE_PROFIT_RR", 2.1)),
                    "breakeven_trigger_pct": float(configs.get("BREAKEVEN_TRIGGER_PCT", 0.025)),
                    "committee_weights": {
                        "technical": float(configs.get("WEIGHT_TECHNICAL", 0.35)),
                        "fundamental": float(configs.get("WEIGHT_FUNDAMENTAL", 0.30)),
                        "sentiment": float(configs.get("WEIGHT_SENTIMENT", 0.15)),
                        "risk": float(configs.get("WEIGHT_RISK", 0.20))
                    },
                    "last_calibrated": configs.get("LAST_CALIBRATION_TIMESTAMP", "")
                },
                "recent_lessons": [
                    {
                        "id": l.id,
                        "ticker": l.ticker,
                        "outcome": l.outcome,
                        "realized_pnl_eur": l.realized_pnl_eur,
                        "realized_pnl_pct": l.realized_pnl_pct,
                        "exit_reason": l.exit_reason,
                        "holding_period_hours": l.holding_period_hours,
                        "what_went_well": l.what_went_well,
                        "what_went_wrong": l.what_went_wrong,
                        "lesson": l.lesson,
                        "created_at": l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else ""
                    }
                    for l in lessons
                ]
            }

trade_learner = TradeLearner()
