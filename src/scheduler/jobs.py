import logging
import asyncio
import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from typing import Optional, Union, Dict, Any, List
from zoneinfo import ZoneInfo

from config.settings import settings
from src.screener.scanner import market_scanner
from src.data.market import market_data_provider
from src.agents.committee import investment_committee
from src.trading.broker import active_broker as paper_broker, get_active_broker, active_broker
from src.trading.risk import calculate_position_allocation, derive_trigger_levels
from src.trading.shadow import shadow_portfolios
from src.telegram.bot import telegram_bot
from sqlalchemy import select, desc
from src.db.session import AsyncSessionLocal
from src.db.models import AnalysisReport, MarketDigest, Position, OrderTrigger, SystemConfig
from src.data.review_tracker import review_tracker
from src.trading.risk import check_market_regime

logger = logging.getLogger(__name__)

class TradingScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=settings.TIMEZONE)
        # Serializes trigger/stop checks so a manual refresh cannot overlap a scheduled run
        self._monitor_lock = asyncio.Lock()

    @staticmethod
    def _should_arm_trigger(report: Dict[str, Any], trigger_min_score: int) -> bool:
        """
        Arms only a buy verdict that the Risk Officer approved and that scores at or above the threshold.
        The verdict matters on its own: when the learner raises MIN_CONSENSUS_SCORE above TRIGGER_MIN_SCORE,
        a score can clear the trigger threshold while the committee still says HOLD.
        """
        return (
            report["consensus_score"] >= trigger_min_score
            and report.get("action") in ("BUY", "STRONG_BUY")
            and report["risk"].get("approved", True)
        )

    async def _scheduled_market_session(self, market: str, session: str):
        """Safeguard for scheduled cron jobs: only run on weekdays."""
        now = datetime.datetime.now(ZoneInfo(settings.TIMEZONE))
        if now.weekday() > 4:
            logger.info(f"Weekend detected ({now.strftime('%A')}). Skipping scheduled {market} {session} session.")
            return
        await self.run_market_session(market=market, session=session)

    def start(self):
        # 1. European Market Open Job (09:00 CET, Mon-Fri)
        self.scheduler.add_job(
            self._scheduled_market_session,
            CronTrigger(day_of_week="mon-fri", hour=settings.EU_OPEN_HOUR, minute=settings.EU_OPEN_MINUTE, timezone=settings.TIMEZONE),
            args=["EU", "OPEN"],
            id="eu_market_open",
            misfire_grace_time=3600
        )

        # 2. European Market Close Job (17:30 CET, Mon-Fri)
        self.scheduler.add_job(
            self._scheduled_market_session,
            CronTrigger(day_of_week="mon-fri", hour=settings.EU_CLOSE_HOUR, minute=settings.EU_CLOSE_MINUTE, timezone=settings.TIMEZONE),
            args=["EU", "CLOSE"],
            id="eu_market_close",
            misfire_grace_time=3600
        )

        # 3. US Market Open Job (09:30 New York time, Mon-Fri)
        self.scheduler.add_job(
            self._scheduled_market_session,
            CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone="America/New_York"),
            args=["US", "OPEN"],
            id="us_market_open",
            misfire_grace_time=3600
        )

        # 4. US Market Close Job (16:00 New York time, Mon-Fri)
        self.scheduler.add_job(
            self._scheduled_market_session,
            CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone="America/New_York"),
            args=["US", "CLOSE"],
            id="us_market_close",
            misfire_grace_time=3600
        )

        # 5. Position Monitor (Stop Loss & Take Profit checks every 10 minutes)
        self.scheduler.add_job(
            self.monitor_open_positions,
            IntervalTrigger(minutes=10),
            id="position_monitor"
        )

        # 6. Price Trigger Monitor (Checks pending limit pullback/breakout triggers every 3 minutes)
        self.scheduler.add_job(
            self.monitor_price_triggers,
            IntervalTrigger(minutes=3),
            id="price_trigger_monitor"
        )

        # 7. Market Opportunity Scanner (Scans every 30 minutes during open market hours for setups with score >= 80)
        self.scheduler.add_job(
            self.scan_market_opportunities,
            IntervalTrigger(minutes=30),
            id="market_opportunity_scanner"
        )

        self.scheduler.start()
        logger.info("Background Trading Scheduler initiated successfully.")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Trading Scheduler stopped.")

    async def check_and_run_startup_sessions(self):
        """
        Runs on bot startup: If today is a weekday and current time falls within
        open market hours for EU or US, and today's OPEN review has not been created yet,
        automatically triggers the opening market session!
        """
        try:
            now = datetime.datetime.now(ZoneInfo(settings.TIMEZONE))
            today_date = now.date()

            # Monday=0, Sunday=6
            if now.weekday() > 4:
                logger.info("Weekend detected. Skipping startup market session catch-up.")
                return

            async with AsyncSessionLocal() as session:
                # 1. European Market Check
                eu_open = now.replace(hour=settings.EU_OPEN_HOUR, minute=settings.EU_OPEN_MINUTE, second=0, microsecond=0)
                eu_close = now.replace(hour=settings.EU_CLOSE_HOUR, minute=settings.EU_CLOSE_MINUTE, second=0, microsecond=0)
                if eu_open <= now <= eu_close:
                    res = await session.execute(
                        select(MarketDigest).where(
                            MarketDigest.market == "EU",
                            MarketDigest.session == "OPEN"
                        ).order_by(desc(MarketDigest.timestamp))
                    )
                    latest_eu = res.scalars().first()
                    should_run_eu = False
                    if not latest_eu or not latest_eu.timestamp:
                        should_run_eu = True
                    else:
                        # Convert timestamp to local date
                        latest_date = latest_eu.timestamp.date()
                        if latest_date != today_date:
                            should_run_eu = True

                    if should_run_eu:
                        logger.info("Startup catch-up: European Market is currently OPEN and Opening Review not yet sent today. Triggering opening session...")
                        await self.run_market_session(market="EU", session="OPEN")

                # 2. US Market Check
                us_open = now.replace(hour=settings.US_OPEN_HOUR, minute=settings.US_OPEN_MINUTE, second=0, microsecond=0)
                us_close = now.replace(hour=settings.US_CLOSE_HOUR, minute=settings.US_CLOSE_MINUTE, second=0, microsecond=0)
                if us_open <= now <= us_close:
                    res = await session.execute(
                        select(MarketDigest).where(
                            MarketDigest.market == "US",
                            MarketDigest.session == "OPEN"
                        ).order_by(desc(MarketDigest.timestamp))
                    )
                    latest_us = res.scalars().first()
                    should_run_us = False
                    if not latest_us or not latest_us.timestamp:
                        should_run_us = True
                    else:
                        latest_date = latest_us.timestamp.date()
                        if latest_date != today_date:
                            should_run_us = True

                    if should_run_us:
                        logger.info("Startup catch-up: US Market is currently OPEN and Opening Review not yet sent today. Triggering opening session...")
                        await self.run_market_session(market="US", session="OPEN")
        except Exception as e:
            logger.error(f"Error checking startup market sessions: {e}")

    async def run_opening_review(self, market: str = "EU"):
        """Explicit on-demand runner for Opening Market Review."""
        return await self.run_market_session(market=market, session="OPEN")

    async def run_market_session(self, market: str, session: str, target_chat_id: Optional[Union[int, str]] = None):
        """
        Runs candidate scanning, invokes agent committee, executes auto-buys if high conviction,
        arms limit triggers if pending pullback, and broadcasts market digest.
        """
        try:
            logger.info(f"Triggering scheduled session: {market} {session}")
            
            # 1. Scan market universe (expanded candidate pool)
            candidates = await asyncio.to_thread(market_scanner.scan_universe, market=market, limit=12)
            summary = await paper_broker.get_portfolio_summary()
            reports = []
            executed_trades = []
            armed_triggers = []

            # Fetch existing open positions and pending triggers to enforce anti-overlap
            trigger_min_score = settings.TRIGGER_MIN_SCORE
            async with AsyncSessionLocal() as db_session:
                pos_check = await db_session.execute(
                    select(Position.ticker).where(Position.status == "OPEN")
                )
                open_tickers = set(t.upper() for t in pos_check.scalars().all())

                trig_check = await db_session.execute(
                    select(OrderTrigger.ticker).where(OrderTrigger.status == "PENDING")
                )
                pending_tickers = set(t.upper() for t in trig_check.scalars().all())

                cfg_trig = await db_session.execute(
                    select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE")
                )
                trig_c = cfg_trig.scalars().first()
                if trig_c and trig_c.value:
                    try:
                        trigger_min_score = int(trig_c.value)
                    except ValueError:
                        pass

            # Prioritize actionable candidates (not already held and no pending trigger)
            actionable_cands = [c for c in candidates if c["ticker"].upper() not in open_tickers and c["ticker"].upper() not in pending_tickers]
            other_cands = [c for c in candidates if c["ticker"].upper() in open_tickers or c["ticker"].upper() in pending_tickers]

            # Always select up to 4 candidates to evaluate for the morning review
            selected_cands = actionable_cands[:4]
            if len(selected_cands) < 4:
                needed = 4 - len(selected_cands)
                selected_cands.extend(other_cands[:needed])

            # 2. Convene committee on selected candidates
            for cand in selected_cands:
                ticker = cand["ticker"].upper()
                try:
                    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
                    if not data:
                        continue

                    summary = await paper_broker.get_portfolio_summary()
                    report = await asyncio.wait_for(
                        investment_committee.evaluate_ticker(data, summary["total_value_eur"]),
                        timeout=45.0
                    )

                    # Persist AnalysisReport
                    async with AsyncSessionLocal() as db_session:
                        rep_entry = AnalysisReport(
                            ticker=ticker,
                            action=report["action"],
                            consensus_score=report["consensus_score"],
                            target_price=report["risk"]["take_profit_price"],
                            stop_loss=report["risk"]["stop_loss_price"],
                            summary=report["summary"],
                            technical_summary=report["technical"]["analysis_summary"],
                            fundamental_summary=report["fundamental"]["analysis_summary"],
                            sentiment_summary=report["sentiment"]["sentiment_summary"],
                            risk_summary=report["risk"]["risk_summary"],
                            debate_transcript=report["debate_transcript"],
                            raw_signals=report
                        )
                        db_session.add(rep_entry)
                        await db_session.commit()

                    # 3. Automatically Arm AI Triggers for Scores >= 80 with Anti-Overlap Guardrails
                    if ticker in open_tickers:
                        logger.info(f"Skipping trigger for {ticker}: position already open (anti-overlap).")
                        report["status_type"] = "ALREADY_HELD"
                    elif ticker in pending_tickers:
                        logger.info(f"Skipping trigger for {ticker}: pending trigger already active (anti-overlap).")
                        report["status_type"] = "TRIGGER_PENDING"
                    elif self._should_arm_trigger(report, trigger_min_score):
                        # Automatically arm trigger for approved buy setups scoring >= 80
                        curr_summary = await paper_broker.get_portfolio_summary()
                        available_cash = curr_summary.get("available_cash_eur", 0.0)
                        regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, ticker, data.get("currency"))

                        if not regime_ok:
                            logger.info(f"Skipping trigger for {ticker}: {regime_msg}")
                            report["status_type"] = "REGIME_FILTER"
                        elif available_cash < 20.0:
                            logger.info(f"Skipping trigger for {ticker}: insufficient available cash (€{available_cash:.2f}).")
                            report["status_type"] = "INSUFFICIENT_CASH"
                        else:
                            alloc_eur = calculate_position_allocation(
                                available_cash=available_cash,
                                current_positions_count=len(open_tickers),
                                recommended_size_pct=report["risk"].get("recommended_size_pct"),
                                full_budget_utilization=settings.FULL_BUDGET_UTILIZATION
                            )

                            trigger_type, trigger_price, sl, tp = derive_trigger_levels(report, data["price"])

                            trig_res = await paper_broker.create_price_trigger(
                                ticker=ticker,
                                name=data.get("name", ticker),
                                currency=data["currency"],
                                trigger_type=trigger_type,
                                target_price=trigger_price,
                                current_price_at_creation=data["price"],
                                stop_loss=sl,
                                take_profit=tp,
                                allocation_eur=alloc_eur,
                                consensus_score=report["consensus_score"],
                                ai_rationale=report.get("trigger_rationale", report["summary"])
                            )
                            if trig_res.get("success"):
                                pending_tickers.add(ticker)
                                report["status_type"] = "TRIGGER_ARMED"
                                armed_triggers.append(trig_res)
                                await telegram_bot.notify_trigger_armed(trig_res)
                            else:
                                logger.warning(f"Could not auto-arm trigger for {ticker}: {trig_res.get('message')}")
                                report["status_type"] = "ON_RADAR"
                    else:
                        report["status_type"] = "ON_RADAR"

                    await shadow_portfolios.follow_report(
                        report, data, followed_main=report.get("status_type") == "TRIGGER_ARMED"
                    )
                    reports.append(report)
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout evaluating candidate {ticker} in {market} session, skipping.")
                    continue
                except Exception as cand_err:
                    logger.error(f"Error evaluating candidate {ticker}: {cand_err}", exc_info=True)
                    continue

            # 4. Compare with past-week reviews
            comparison = await review_tracker.compare_past_week_reviews(current_market=market, current_session=session)

            # 5. Send Full Telegram Report
            updated_summary = await paper_broker.get_portfolio_summary()
            positions = await paper_broker.get_positions()
            review_text = ""
            if session.upper() in ("OPEN", "MANUAL_SCAN", "REVIEW"):
                review_text = await telegram_bot.notify_market_open(
                    market=market,
                    reports=reports,
                    executed_trades=executed_trades,
                    portfolio=updated_summary,
                    comparison=comparison,
                    armed_triggers=armed_triggers,
                    positions=positions,
                    target_chat_id=target_chat_id
                )
            else:
                review_text = await telegram_bot.notify_market_close(
                    market=market,
                    positions=positions,
                    closed_trades_today=executed_trades,
                    portfolio=updated_summary,
                    comparison=comparison,
                    target_chat_id=target_chat_id
                )

            # 6. Archive Market Digest with portfolio state and generated review text
            headline = f"{market.upper()} Market {session.capitalize()} Wrap"
            content = f"Evaluated {len(selected_cands)} candidates. Top opportunities: {', '.join([r['ticker'] for r in reports])}"
            await review_tracker.save_session_review(
                market=market.upper(),
                session_type=session.upper(),
                headline=headline,
                content=content,
                top_picks=reports,
                portfolio_state={**updated_summary, "positions": positions},
                review_text=review_text or content
            )
        except Exception as session_err:
            logger.error(f"Error in run_market_session({market}, {session}): {session_err}", exc_info=True)

    async def monitor_open_positions(self) -> List[Dict[str, Any]]:
        """Monitors open positions for stop-loss and take-profit triggers."""
        async with self._monitor_lock:
            closed = await self._check_stops_and_targets()
            await shadow_portfolios.check_exits()
            await shadow_portfolios.record_snapshots(main_summary_fn=get_active_broker().get_portfolio_summary)
            return closed

    async def _check_stops_and_targets(self) -> List[Dict[str, Any]]:
        try:
            triggered_sells = await paper_broker.check_stops_and_targets()
            if triggered_sells:
                logger.info(f"Position monitor processed {len(triggered_sells)} exits: {[s.get('ticker') for s in triggered_sells]}")
            return triggered_sells
        except Exception as e:
            logger.error(f"Error checking stops and targets: {e}")
            return []

    async def monitor_price_triggers(self) -> List[Dict[str, Any]]:
        """Syncs the account balance, then executes automatic buys for pending triggers whose price levels are hit."""
        async with self._monitor_lock:
            try:
                # Cash only: the full sync reconciles positions and is reserved for manual refreshes
                await paper_broker.sync_cash_balance()
            except Exception as e:
                logger.error(f"Cash balance sync failed, continuing with last known balance: {e}")
            executed = await self._check_price_triggers()
            await shadow_portfolios.check_triggers()
            return executed

    async def _sync_account_balance(self) -> Dict[str, Any]:
        """Runs the full broker sync (cash and positions) for a manual refresh."""
        try:
            return await paper_broker.sync_account_state()
        except Exception as e:
            logger.error(f"Account balance sync failed, continuing with last known balance: {e}")
            return {}

    async def _check_price_triggers(self) -> List[Dict[str, Any]]:
        try:
            executed_triggers = await paper_broker.check_and_execute_triggers()
            for t in executed_triggers:
                await telegram_bot.notify_trigger_executed(t)
            return executed_triggers
        except Exception as e:
            logger.error(f"Error checking and executing price triggers: {e}")
            return []

    @staticmethod
    async def _pending_trigger_allocations(db_session) -> Dict[int, tuple]:
        """Returns {trigger_id: (ticker, allocation_eur)} for all pending triggers."""
        res = await db_session.execute(
            select(OrderTrigger.id, OrderTrigger.ticker, OrderTrigger.allocation_eur).where(OrderTrigger.status == "PENDING")
        )
        return {tid: (ticker, alloc) for tid, ticker, alloc in res.all()}

    async def refresh_market_data_now(self) -> Dict[str, Any]:
        """
        Manual market data pull: drops cached quotes for held positions and pending triggers,
        syncs the account balance, resizes triggers, then runs the trigger and stop/target checks.
        """
        async with self._monitor_lock:
            async with AsyncSessionLocal() as db_session:
                pos_res = await db_session.execute(select(Position.ticker).where(Position.status == "OPEN"))
                allocs_before = await self._pending_trigger_allocations(db_session)
                tickers = set(pos_res.scalars().all()) | {ticker for ticker, _ in allocs_before.values()}

            market_data_provider.invalidate(tickers)
            get_active_broker()._last_pnl_refresh = 0.0

            # The balance sync resizes triggers as part of its portfolio summary
            summary = await self._sync_account_balance()
            async with AsyncSessionLocal() as db_session:
                allocs_after = await self._pending_trigger_allocations(db_session)
            resized = [tid for tid, alloc in allocs_after.items() if allocs_before.get(tid) != alloc]
            executed = await self._check_price_triggers()
            closed = await self._check_stops_and_targets()
            await shadow_portfolios.check_triggers()
            await shadow_portfolios.check_exits()

            logger.info(
                f"Manual market data refresh: {len(tickers)} tickers, {len(resized)} triggers resized, "
                f"{len(executed)} triggers executed, {len(closed)} positions closed."
            )
            return {
                "success": True,
                "tickers_refreshed": len(tickers),
                "triggers_resized": len(resized),
                "triggers_executed": len(executed),
                "positions_closed": len(closed),
                "cash_eur": summary.get("cash_eur"),
            }

    async def scan_market_opportunities(self):
        """
        Periodically scans active open markets during market hours.
        For any candidate achieving high conviction (score >= 80) that is not
        already held or pending, automatically arms an AI price trigger.
        """
        from src.data.market_hours import get_market_statuses
        try:
            m_status = get_market_statuses()
            active_markets = []
            if m_status["eu_market"]["is_open"]:
                active_markets.append("EU")
            if m_status["us_market"]["is_open"]:
                active_markets.append("US")

            if not active_markets:
                logger.debug("Opportunity scan skipped: all markets currently closed.")
                return

            trigger_min_score = settings.TRIGGER_MIN_SCORE
            async with AsyncSessionLocal() as db_session:
                cfg_trig = await db_session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
                trig_c = cfg_trig.scalars().first()
                if trig_c and trig_c.value:
                    try:
                        trigger_min_score = int(trig_c.value)
                    except ValueError:
                        pass

                pos_check = await db_session.execute(select(Position.ticker).where(Position.status == "OPEN"))
                open_tickers = set(t.upper() for t in pos_check.scalars().all())

                trig_check = await db_session.execute(select(OrderTrigger.ticker).where(OrderTrigger.status == "PENDING"))
                pending_tickers = set(t.upper() for t in trig_check.scalars().all())

            for mkt in active_markets:
                candidates = await asyncio.to_thread(market_scanner.scan_universe, market=mkt, limit=10)
                actionable = [c for c in candidates if c["ticker"].upper() not in open_tickers and c["ticker"].upper() not in pending_tickers]
                for cand in actionable[:3]:
                    tk = cand["ticker"].upper()
                    data = await asyncio.to_thread(market_data_provider.get_stock_data, tk)
                    if not data:
                        continue

                    summary = await paper_broker.get_portfolio_summary()
                    avail = summary.get("available_cash_eur", 0.0)
                    if avail < 20.0:
                        break

                    report = await investment_committee.evaluate_ticker(data, summary["total_value_eur"])
                    armed = False
                    if self._should_arm_trigger(report, trigger_min_score):
                        regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, tk, data.get("currency"))
                        if not regime_ok:
                            logger.info(f"Opportunity Scanner skipped {tk}: {regime_msg}")
                            continue
                        alloc = calculate_position_allocation(
                            available_cash=avail,
                            current_positions_count=len(open_tickers),
                            recommended_size_pct=report["risk"].get("recommended_size_pct"),
                            full_budget_utilization=settings.FULL_BUDGET_UTILIZATION
                        )

                        t_type, t_price, sl, tp = derive_trigger_levels(report, data["price"])

                        trig_res = await paper_broker.create_price_trigger(
                            ticker=tk,
                            name=data.get("name", tk),
                            currency=data["currency"],
                            trigger_type=t_type,
                            target_price=t_price,
                            current_price_at_creation=data["price"],
                            stop_loss=sl,
                            take_profit=tp,
                            allocation_eur=alloc,
                            consensus_score=report["consensus_score"],
                            ai_rationale=report.get("trigger_rationale", report["summary"])
                        )
                        if trig_res.get("success"):
                            pending_tickers.add(tk)
                            logger.info(f"Opportunity Scanner auto-armed trigger #{trig_res['trigger_id']} for {tk} (Score {report['consensus_score']}/100 >= {trigger_min_score})")
                            await telegram_bot.notify_trigger_armed(trig_res)
                            armed = True
                    await shadow_portfolios.follow_report(report, data, followed_main=armed)
        except Exception as e:
            logger.error(f"Error in scan_market_opportunities: {e}")

trading_scheduler = TradingScheduler()
