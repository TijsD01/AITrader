import logging
import asyncio
import datetime
import html
import math
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from typing import Optional, Union, Dict, Any, List
from zoneinfo import ZoneInfo

from config.settings import settings
from src.screener.scanner import market_scanner
from src.data.market import market_data_provider
from src.agents.committee import investment_committee
from src.trading.broker import active_broker as paper_broker, get_active_broker, active_broker, live_broker
from src.trading.risk import derive_trigger_levels, entry_allocation_from_summary, plan_buy
from src.trading.shadow import shadow_portfolios
from src.trading.shadow_learner import schedule_learn_due
from src.telegram.bot import telegram_bot
from sqlalchemy import select, desc
from src.db.session import AsyncSessionLocal
from src.db.models import AnalysisReport, MarketDigest, Position, OrderTrigger, SystemConfig
from src.data.review_tracker import review_tracker
from src.trading.risk import check_market_regime
from src.trading.trigger_gate import evaluate_trigger_verdict, get_trigger_min_score
from src.trading.main_strategy import get_main_rules, is_default_shape
from src.trading.levels import plan_entry
from src.trading.holdings import mine_tickers
from src.trading.trading212 import broker_can_buy

logger = logging.getLogger(__name__)


def digest_headline(market: str, session: str) -> str:
    """Dashboard headline for an archived session review. A manual scan (market ALL from the Scan button) is not an exchange open."""
    m, s = market.upper(), session.upper()
    flag = {"EU": "🇪🇺", "US": "🇺🇸"}.get(m, "🌍")
    if s == "MANUAL_SCAN":
        return f"{flag} {m}-scan"
    return f"{flag} {m}-beurs {'open' if s in ('OPEN', 'REVIEW') else 'dicht'}"


async def _tickers_in_use(db_session) -> tuple:
    """Open-position and pending-trigger tickers of the active mode only, so stale paper rows never block live buys."""
    live = get_active_broker() is live_broker
    pos = await db_session.execute(select(Position.ticker).where(Position.status == "OPEN", Position.is_live == live))
    trig = await db_session.execute(
        select(OrderTrigger.ticker).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == live)
    )
    return {t.upper() for t in pos.scalars().all()}, {t.upper() for t in trig.scalars().all()}

class TradingScheduler:
    def __init__(self):
        # APScheduler's default grace is 1 s: a short event-loop stall would silently skip a stop or trigger check
        self.scheduler = AsyncIOScheduler(
            timezone=settings.TIMEZONE, job_defaults={"misfire_grace_time": 60, "coalesce": True}
        )
        # Serializes trigger/stop checks so a manual refresh cannot overlap a scheduled run
        self._monitor_lock = asyncio.Lock()

    @staticmethod
    def _should_arm_trigger(report: Dict[str, Any], trigger_min_score: int, rules=None) -> bool:
        """
        Arms only a buy verdict that the Risk Officer approved and that scores at or above the threshold.
        The verdict matters on its own: when the learner raises MIN_CONSENSUS_SCORE above TRIGGER_MIN_SCORE,
        a score can clear the trigger threshold while the committee still says HOLD.
        rules: the main rule set (accept_hold / ignore_risk_veto); None or defaults give today's gate.
        """
        return evaluate_trigger_verdict(report, trigger_min_score, rules=rules).allowed

    @staticmethod
    def _entry_levels(ticker: str, report: Dict[str, Any], price: float, rules) -> Optional[tuple]:
        """
        (trigger_type, entry, stop, target) for an automated entry. A promoted rule set reshapes the levels
        (plan_entry; trigger_type may become MARKET). Default rules skip plan_entry, so levels match today exactly.
        None when the shaped entry has no usable stop (missing, not finite, <= 0 or not below the entry): no entry then.
        """
        levels = derive_trigger_levels(report, price)
        if rules is None or is_default_shape(rules):
            return levels
        plan = plan_entry(rules.as_strategy(), *levels, price)
        entry, stop = plan["target_price"], plan["stop_loss"]
        if stop is None or not math.isfinite(stop) or not math.isfinite(entry) or not 0 < stop < entry:
            logger.info(f"No entry for {ticker}: main rules v{rules.version} leave no usable stop (entry {entry}, stop {stop}).")
            return None
        return plan["trigger_type"], entry, stop, plan["take_profit"]

    @staticmethod
    async def _buyable(candidates: list) -> list:
        """Drops candidates the broker cannot buy before the top ones are picked, so they do not use up a slot."""
        # No currency on the candidate: keep it, the check after the quote decides
        return [c for c in candidates
                if not c.get("currency") or (await broker_can_buy(c["ticker"].upper(), c["currency"]))[0]]

    @staticmethod
    def _rules_kwargs(rules) -> Dict[str, Any]:
        """Tags a trigger with the rule set version only when non-default rules planned it."""
        return {} if rules is None or is_default_shape(rules) else {"rules_version": rules.version}

    async def _market_entry(
        self, ticker: str, data: Dict[str, Any], stop_loss: Optional[float], take_profit: Optional[float],
        allocation_eur: float, report: Dict[str, Any], rules
    ) -> Dict[str, Any]:
        """
        Buys now (entry_mode "market") through the broker's execute_buy, the same call a fired trigger makes,
        so sizing, validate_buy_order, fee viability, whole shares, market hours and the kill switch all decide.
        """
        fresh = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
        price = (fresh or {}).get("price")
        # Same invalidation as a pending trigger, and never without a stop: no quote, or the price through the stop or target
        if not price or price <= 0 or not stop_loss or price <= stop_loss or (take_profit and price >= take_profit):
            return {"success": False, "message": f"No market entry for {ticker}: price {price} vs stop {stop_loss} / target {take_profit}."}
        # The trigger monitor's lock: a market entry never overlaps a trigger fill or a stop check
        async with self._monitor_lock:
            try:
                # Same as monitor_price_triggers before it fires: size the buy on the broker's current cash
                await paper_broker.sync_cash_balance()
            except Exception as e:
                logger.error(f"Cash balance sync failed, continuing with last known balance: {e}")
            res = await paper_broker.execute_buy(
                ticker=ticker,
                name=data.get("name", ticker),
                price_native=price,
                currency=data["currency"],
                stop_loss_native=stop_loss,
                take_profit_native=take_profit,
                allocation_eur=allocation_eur,
                consensus_score=report["consensus_score"],
                rationale=f"Market entry (main rules v{rules.version}): {report.get('trigger_rationale') or report['summary']}",
                is_manual=False,
                rules_version=rules.version
            )
        if res.get("success"):
            mode = "LIVE" if res.get("is_live") else "PAPER"
            await telegram_bot.send_message(
                f"🚀 <b>MARKET ENTRY EXECUTED ({mode})</b>\n\n"
                f"<b>Asset:</b> <code>{html.escape(ticker)}</code>\n"
                f"<b>Price:</b> {res.get('price_native')}\n"
                f"<b>Shares Bought:</b> {res.get('shares')}\n"
                f"<b>Total Cost:</b> €{res.get('total_eur') or 0:,.2f}\n"
                f"<b>Stop Loss:</b> {stop_loss} | <b>Take Profit:</b> {take_profit}\n\n"
                f"<i>Bought at market under main rules v{rules.version}; the position is open in the {mode} portfolio.</i>"
            )
        return res

    @staticmethod
    async def _save_report(ticker: str, report: Dict[str, Any]) -> AnalysisReport:
        """Persists a committee report as an AnalysisReport row."""
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
            return rep_entry

    async def _cancel_downgraded_triggers(self, ticker: str, report: Dict[str, Any], trigger_min_score: int, rules=None) -> bool:
        """
        Cancels the pending trigger(s) of the active mode for `ticker` when a fresh debate would no longer arm one
        (lower score, non-buy verdict or risk veto). An old high score must not buy on a stale verdict.
        Only a trigger the user forced against a soft block (HOLD, low score) survives a soft block, and only while
        the score has not fallen below its armed score. Hard blocks (veto, SELL, below minimum) always cancel.
        A trigger armed under promoted main rules (rules_version set) is judged by those rules; others by today's gate.
        Returns True when at least one trigger was cancelled.
        """
        verdict = evaluate_trigger_verdict(report, trigger_min_score)
        if verdict.allowed:
            return False  # rules only relax the gate, so a rules-armed trigger passes too
        ruled_ok = rules is not None and evaluate_trigger_verdict(report, trigger_min_score, rules=rules).allowed
        live = get_active_broker() is live_broker
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                select(OrderTrigger.id, OrderTrigger.forced, OrderTrigger.consensus_score, OrderTrigger.rules_version).where(
                    OrderTrigger.ticker == ticker, OrderTrigger.status == "PENDING", OrderTrigger.is_live == live
                )
            )
            trigger_ids = [
                trigger_id for trigger_id, forced, armed_score, rules_version in res.all()
                if not (forced and verdict.overridable and armed_score is not None
                        and verdict.consensus_score >= armed_score)
                and not (rules_version is not None and ruled_ok)
            ]
        return await self._cancel_triggers(
            ticker, f"New debate no longer supports this buy: {verdict.message}", trigger_ids,
            log_note=f"{verdict.code}, score {verdict.consensus_score}"
        )

    async def _cancel_triggers(self, ticker: str, reason: str, trigger_ids: Optional[List[int]] = None, log_note: str = "") -> bool:
        """Cancels the given pending triggers (default: all of the active mode for `ticker`). True when one was cancelled."""
        if trigger_ids is None:
            live = get_active_broker() is live_broker
            async with AsyncSessionLocal() as db_session:
                res = await db_session.execute(select(OrderTrigger.id).where(
                    OrderTrigger.ticker == ticker, OrderTrigger.status == "PENDING", OrderTrigger.is_live == live
                ))
                trigger_ids = list(res.scalars().all())
        cancelled = False
        # Same lock as the trigger monitor, so a fill check cannot run mid-cancel
        async with self._monitor_lock:
            for trigger_id in trigger_ids:
                res = await active_broker.cancel_price_trigger(trigger_id, reason=reason)
                if res.get("success"):
                    cancelled = True
                    logger.info(f"Cancelled trigger #{trigger_id} for {ticker} after re-debate ({log_note or reason}).")
                else:
                    logger.warning(f"Could not cancel trigger #{trigger_id} for {ticker} after re-debate: {res.get('message')}")
        return cancelled

    async def refresh_pending_triggers(self) -> Dict[str, List[str]]:
        """
        Daily job: re-debates every pending trigger of the active mode, so none buys on a stale verdict.
        A trigger that no longer passes the arming gate is cancelled; one that still passes gets its new score
        (funding priority). A ticker debated in the last TRIGGER_REDEBATE_SKIP_HOURS reuses that report instead of
        a new LLM call, so a recent manual analysis still counts.
        """
        result: Dict[str, List[str]] = {"kept": [], "cancelled": [], "failed": []}
        trigger_min_score = await get_trigger_min_score()
        rules = await get_main_rules()
        live = get_active_broker() is live_broker
        since = datetime.datetime.utcnow() - datetime.timedelta(hours=settings.TRIGGER_REDEBATE_SKIP_HOURS)
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(
                select(OrderTrigger.ticker, OrderTrigger.currency).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == live)
            )
            currencies = {t.upper(): ccy for t, ccy in res.all()}
            tickers = sorted(currencies)
            recent = await db_session.execute(
                select(AnalysisReport).where(AnalysisReport.created_at >= since).order_by(desc(AnalysisReport.created_at))
            )
            recent_reports: Dict[str, Dict[str, Any]] = {}
            for row in recent.scalars().all():
                recent_reports.setdefault(row.ticker.upper(), {
                    **(row.raw_signals or {}), "action": row.action, "consensus_score": row.consensus_score
                })

        for ticker in tickers:
            try:
                on_broker, why = await broker_can_buy(ticker, currencies[ticker])
                if not on_broker:
                    if await self._cancel_triggers(ticker, why):
                        result["cancelled"].append(f"{ticker} (not on Trading 212)")
                    continue
                report = recent_reports.get(ticker)
                if report is None:
                    data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
                    if not data:
                        result["failed"].append(ticker)
                        continue
                    summary = await paper_broker.get_portfolio_summary()
                    report = await asyncio.wait_for(
                        investment_committee.evaluate_ticker(data, summary["total_value_eur"]), timeout=300.0
                    )
                    await self._save_report(ticker, report)
                if await self._cancel_downgraded_triggers(ticker, report, trigger_min_score, rules):
                    result["cancelled"].append(f"{ticker} ({report['consensus_score']}/100)")
                    continue
                result["kept"].append(f"{ticker} ({report['consensus_score']}/100)")
                # Only a passing verdict refreshes the score; a kept forced trigger keeps its armed score
                if not evaluate_trigger_verdict(report, trigger_min_score).allowed:
                    continue
                async with AsyncSessionLocal() as db_session:
                    res = await db_session.execute(
                        select(OrderTrigger).where(
                            OrderTrigger.ticker == ticker, OrderTrigger.status == "PENDING", OrderTrigger.is_live == live
                        )
                    )
                    for trig in res.scalars().all():
                        trig.consensus_score = report["consensus_score"]
                    await db_session.commit()
            except Exception as e:
                # A failed debate (LLM timeout, data error) never cancels: the trigger keeps its last verdict
                logger.error(f"Trigger re-debate failed for {ticker}: {e}", exc_info=True)
                result["failed"].append(ticker)

        logger.info(f"Trigger re-debate done: {result}")
        if result["cancelled"]:
            await telegram_bot.send_message(
                "🛑 <b>Koopopdrachten geannuleerd na nieuwe beoordeling</b>\n"
                + "\n".join(f"• {t}" for t in result["cancelled"])
            )
        return result

    @staticmethod
    def _fits_whole_share(
        allocation_eur: float, price_native: float, currency: str,
        stop_loss_native: Optional[float] = None, portfolio_value_eur: float = 0.0, max_eur: Optional[float] = None
    ) -> bool:
        """False when whole-share sizing is on and not even one share fits the allocation after the risk cap."""
        if not settings.WHOLE_SHARES_ONLY:
            return True
        return plan_buy(allocation_eur, price_native, currency, stop_loss_native, portfolio_value_eur, max_eur=max_eur)["shares"] >= 1

    @staticmethod
    def _is_affordable(price_native: Optional[float], currency: str, summary: Dict[str, Any]) -> bool:
        """True when one share fits both the free unreserved cash and the max position size (whole-share mode only)."""
        if not settings.WHOLE_SHARES_ONLY or not price_native:
            return True
        from src.data.currency import currency_service
        price_eur = currency_service.to_eur(price_native, currency or "EUR")
        limit = min(summary.get("available_cash_eur", 0.0),
                    summary.get("total_value_eur", 0.0) * settings.MAX_POSITION_PCT / 100.0)
        return price_eur <= limit

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

        # 8. Trading 212 resting-order sync (no-op unless T212_RESTING_ORDERS is on)
        self.scheduler.add_job(
            self.monitor_resting_orders,
            IntervalTrigger(minutes=1),
            id="t212_order_sync",
            max_instances=1,
            coalesce=True
        )

        # 9. Learner idle decay (08:00, Mon-Fri): lowers raised score thresholds after calendar days without a buy
        self.scheduler.add_job(
            self.decay_idle_thresholds,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=settings.TIMEZONE),
            id="learner_idle_decay",
            misfire_grace_time=3600,
            coalesce=True
        )

        # 10. Daily trigger re-debate (before EU open, Mon-Fri): cancels pending triggers the committee no longer backs
        self.scheduler.add_job(
            self.refresh_pending_triggers,
            CronTrigger(day_of_week="mon-fri", hour=settings.TRIGGER_REDEBATE_HOUR,
                        minute=settings.TRIGGER_REDEBATE_MINUTE, timezone=settings.TIMEZONE),
            id="trigger_redebate",
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1
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
            if get_active_broker() is not live_broker or not live_broker.resting_orders_on():
                # Resting T212 entry orders left from an earlier run are no longer synced: cancel them
                try:
                    await live_broker.cancel_resting_entries("resting orders off or live disarmed at startup")
                except Exception as e:
                    logger.error(f"Startup cancel of resting T212 orders failed: {e}")
            await self.monitor_resting_orders()
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
                from src.data.market_hours import us_session_local
                us_open, us_close = us_session_local(now)
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

    async def run_market_session(self, market: str, session: str, target_chat_id: Optional[Union[int, str]] = None):
        """
        Runs candidate scanning, invokes agent committee, executes auto-buys if high conviction,
        arms limit triggers if pending pullback, and broadcasts market digest.
        """
        try:
            logger.info(f"Triggering scheduled session: {market} {session}")
            
            # 1. Scan market universe (expanded candidate pool)
            extra = await market_scanner.load_watchlist_tickers()
            candidates = await asyncio.to_thread(market_scanner.scan_universe, market=market, limit=12, extra_tickers=extra)
            summary = await paper_broker.get_portfolio_summary()
            reports = []
            executed_trades = []
            armed_triggers = []

            # Fetch existing open positions and pending triggers to enforce anti-overlap
            trigger_min_score = settings.TRIGGER_MIN_SCORE
            rules = await get_main_rules()  # once per session: entry rules for every candidate below
            async with AsyncSessionLocal() as db_session:
                open_tickers, pending_tickers = await _tickers_in_use(db_session)
                # The user's own holdings are never evaluated, shadowed or reported
                mine = await mine_tickers(db_session)
                candidates = [c for c in candidates if c["ticker"].upper() not in mine]

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

            actionable_cands = await self._buyable(actionable_cands)
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
                    on_broker, why = await broker_can_buy(ticker, data.get("currency"))
                    if not on_broker:
                        logger.info(f"Skipping {ticker} in {market} session: {why}")
                        continue

                    summary = await paper_broker.get_portfolio_summary()
                    report = await asyncio.wait_for(
                        investment_committee.evaluate_ticker(data, summary["total_value_eur"]),
                        timeout=300.0  # 3 sequential agy LLM calls x 90s worst case
                    )

                    rep_entry = await self._save_report(ticker, report)

                    # 3. Automatically Arm AI Triggers for Scores >= 80 with Anti-Overlap Guardrails
                    if ticker in open_tickers:
                        logger.info(f"Skipping trigger for {ticker}: position already open (anti-overlap).")
                        report["status_type"] = "ALREADY_HELD"
                    elif ticker in pending_tickers:
                        if await self._cancel_downgraded_triggers(ticker, report, trigger_min_score, rules):
                            pending_tickers.discard(ticker)
                            report["status_type"] = "TRIGGER_CANCELLED"
                        else:
                            logger.info(f"Skipping trigger for {ticker}: pending trigger already active (anti-overlap).")
                            report["status_type"] = "TRIGGER_PENDING"
                    elif self._should_arm_trigger(report, trigger_min_score, rules):
                        # Automatically arm trigger for approved buy setups scoring >= 80
                        curr_summary = await paper_broker.get_portfolio_summary()
                        available_cash = curr_summary.get("available_cash_eur", 0.0)
                        regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, ticker, data.get("currency"))

                        if not regime_ok:
                            logger.info(f"Skipping trigger for {ticker}: {regime_msg}")
                            report["status_type"] = "REGIME_FILTER"
                        elif not self._is_affordable(data.get("price"), data.get("currency"), curr_summary):
                            logger.info(f"Skipping trigger for {ticker}: one share is above free cash or the max position size.")
                            report["status_type"] = "SHARE_TOO_EXPENSIVE"
                        elif available_cash < 20.0:
                            logger.info(f"Skipping trigger for {ticker}: insufficient available cash (€{available_cash:.2f}).")
                            report["status_type"] = "INSUFFICIENT_CASH"
                        else:
                            # Same slot the dashboard shows once the trigger is pending
                            alloc_eur = entry_allocation_from_summary(curr_summary, ticker, data["currency"])

                            levels = self._entry_levels(ticker, report, data["price"], rules)
                            trigger_type, trigger_price, sl, tp = levels or (None, None, None, None)

                            if levels is None:
                                report["status_type"] = "ON_RADAR"
                            elif alloc_eur < 20.0:
                                logger.info(f"Skipping trigger for {ticker}: no free position slot or unreserved cash left.")
                                report["status_type"] = "INSUFFICIENT_CASH"
                            elif not self._fits_whole_share(alloc_eur, trigger_price, data["currency"], sl,
                                                            curr_summary.get("total_value_eur", 0.0), available_cash):
                                logger.info(f"Skipping trigger for {ticker}: one share costs more than the €{alloc_eur:.2f} allocation after the risk cap.")
                                report["status_type"] = "SHARE_TOO_EXPENSIVE"
                            elif trigger_type == "MARKET":
                                buy_res = await self._market_entry(ticker, data, sl, tp, alloc_eur, report, rules)
                                if buy_res.get("success"):
                                    open_tickers.add(ticker)
                                    report["status_type"] = "BOUGHT"
                                    if session.upper() in ("OPEN", "MANUAL_SCAN", "REVIEW"):  # the close digest lists sells only
                                        executed_trades.append(buy_res)
                                else:
                                    logger.info(f"Market entry for {ticker} not placed: {buy_res.get('message')}")
                                    report["status_type"] = "ON_RADAR"
                            else:
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
                                    ai_rationale=report.get("trigger_rationale", report["summary"]),
                                    report_id=rep_entry.id,
                                    **self._rules_kwargs(rules)
                                )
                                if trig_res.get("success"):
                                    pending_tickers.add(ticker)
                                    report["status_type"] = "TRIGGER_ARMED"
                                    report["entry_trigger_price"] = trigger_price
                                    armed_triggers.append(trig_res)
                                    await telegram_bot.notify_trigger_armed(trig_res)
                                else:
                                    logger.warning(f"Could not auto-arm trigger for {ticker}: {trig_res.get('message')}")
                                    report["status_type"] = "ON_RADAR"
                    else:
                        report["status_type"] = "ON_RADAR"

                    await shadow_portfolios.follow_report(
                        report, data, followed_main=report.get("status_type") in ("TRIGGER_ARMED", "BOUGHT")
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
            headline = digest_headline(market, session)
            armed = [t.get("ticker") for t in armed_triggers]
            content = f"{len(reports)} aandelen bekeken. " + (
                f"Koopopdracht klaargezet voor {', '.join(armed)}." if armed else "Geen nieuwe koopopdrachten."
            )
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
            if settings.TRADING_MODE == "LIVE":
                try:
                    # Adopts fills the bot never booked and books exits the broker already made (stop/target legs)
                    await paper_broker.sync_account_state()
                except Exception as e:
                    logger.error(f"Periodic broker adoption failed: {e}")
            closed = await self._check_stops_and_targets()
            if await shadow_portfolios.check_exits():
                schedule_learn_due()
            await shadow_portfolios.record_snapshots(main_summary_fn=get_active_broker().get_portfolio_summary)
            return closed

    async def monitor_resting_orders(self) -> List[Dict[str, Any]]:
        """Books fills of resting Trading 212 entry orders and re-checks stops. Returns booked fills."""
        broker = get_active_broker()
        if not getattr(broker, "resting_orders_on", lambda: False)():
            return []
        async with self._monitor_lock:
            try:
                try:
                    await broker.sync_cash_balance()
                except Exception as e:
                    logger.error(f"Cash balance sync failed before resting order sync: {e}")
                fills = await broker.sync_resting_entries()
                for t in fills:
                    await telegram_bot.notify_trigger_executed(t)
                await self._check_stops_and_targets()
                return fills
            except Exception as e:
                logger.error(f"Resting order sync failed: {e}", exc_info=True)
                return []

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

    async def decay_idle_thresholds(self):
        """Daily job: lets the learner lower raised score thresholds after a stretch without buys."""
        try:
            from src.trading.learner import trade_learner
            await trade_learner.decay_idle_thresholds()
        except Exception as e:
            logger.error(f"Learner idle decay failed: {e}")

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
            if await shadow_portfolios.check_exits():
                schedule_learn_due()

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

                open_tickers, pending_tickers = await _tickers_in_use(db_session)
                mine = await mine_tickers(db_session)
            rules = await get_main_rules()  # once per scan

            extra = await market_scanner.load_watchlist_tickers()
            for mkt in active_markets:
                candidates = await asyncio.to_thread(market_scanner.scan_universe, market=mkt, limit=10, extra_tickers=extra)
                # The user's own holdings are never evaluated, shadowed or reported
                candidates = [c for c in candidates if c["ticker"].upper() not in mine]
                actionable = [c for c in candidates if c["ticker"].upper() not in open_tickers and c["ticker"].upper() not in pending_tickers]
                # No pre-committee affordability filter: _fits_whole_share checks it after the verdict, and the shadow still sees it.
                for cand in (await self._buyable(actionable))[:3]:
                    tk = cand["ticker"].upper()
                    data = await asyncio.to_thread(market_data_provider.get_stock_data, tk)
                    if not data:
                        continue
                    on_broker, why = await broker_can_buy(tk, data.get("currency"))
                    if not on_broker:
                        logger.info(f"Opportunity Scanner skipped {tk}: {why}")
                        continue

                    summary = await paper_broker.get_portfolio_summary()
                    avail = summary.get("available_cash_eur", 0.0)
                    if avail < 20.0:
                        break

                    # This scan runs every 30 minutes: a ticker debated a moment ago keeps its verdict, without a new LLM call
                    reused = investment_committee.cached_report(tk, settings.SCAN_REPORT_REUSE_MINUTES * 60)
                    report = reused or await investment_committee.evaluate_ticker(data, summary["total_value_eur"])
                    armed = False
                    if self._should_arm_trigger(report, trigger_min_score, rules):
                        regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, tk, data.get("currency"))
                        alloc = entry_allocation_from_summary(summary, tk, data["currency"])
                        levels = self._entry_levels(tk, report, data["price"], rules)
                        t_type, t_price, sl, tp = levels or (None, None, None, None)

                        # Skip arming, but still let the shadow portfolios see the verdict below
                        if not regime_ok:
                            logger.info(f"Opportunity Scanner skipped {tk}: {regime_msg}")
                        elif levels is None:
                            pass  # _entry_levels logged why
                        elif alloc < 20.0:
                            logger.info(f"Opportunity Scanner skipped {tk}: no free position slot or unreserved cash left.")
                        elif not self._fits_whole_share(alloc, t_price, data["currency"], sl,
                                                        summary.get("total_value_eur", 0.0), avail):
                            logger.info(f"Opportunity Scanner skipped {tk}: one share costs more than the €{alloc:.2f} allocation after the risk cap.")
                        elif t_type == "MARKET":
                            buy_res = await self._market_entry(tk, data, sl, tp, alloc, report, rules)
                            if buy_res.get("success"):
                                open_tickers.add(tk)
                                logger.info(f"Opportunity Scanner bought {tk} at market (Score {report['consensus_score']}/100, main rules v{rules.version})")
                                armed = True
                            else:
                                logger.info(f"Opportunity Scanner market entry for {tk} not placed: {buy_res.get('message')}")
                        else:
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
                                ai_rationale=report.get("trigger_rationale", report["summary"]),
                                **self._rules_kwargs(rules)
                            )
                            if trig_res.get("success"):
                                pending_tickers.add(tk)
                                logger.info(f"Opportunity Scanner auto-armed trigger #{trig_res['trigger_id']} for {tk} (Score {report['consensus_score']}/100 >= {trigger_min_score})")
                                await telegram_bot.notify_trigger_armed(trig_res)
                                armed = True
                    if not reused:  # the shadows followed this report when it was new
                        await shadow_portfolios.follow_report(report, data, followed_main=armed)
        except Exception as e:
            logger.error(f"Error in scan_market_opportunities: {e}")

trading_scheduler = TradingScheduler()
