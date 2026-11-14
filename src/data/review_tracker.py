import logging
import asyncio
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional
from sqlalchemy import select, desc

from config.settings import settings
from src.db.session import AsyncSessionLocal
from src.db.models import MarketDigest
from src.data.market import market_data_provider
from src.data.market_hours import us_session_local

logger = logging.getLogger(__name__)

NL_DAYS = ["ma", "di", "wo", "do", "vr", "za", "zo"]
NL_MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]


def fmt_num(value: Any, digits: int = 2) -> str:
    """Dutch number format: 1.234,56"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{v:,.{digits}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_eur(value: Any, signed: bool = False) -> str:
    """Euro amount in Dutch format, optionally with an explicit + or - sign."""
    v = float(value or 0.0)
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ("-" if v < 0 else "")
    return f"{sign}€{fmt_num(abs(v))}"


def fmt_pct(value: Any, digits: int = 1) -> str:
    """Signed percentage in Dutch format: +2,6%"""
    v = float(value or 0.0)
    return f"{'+' if v > 0 else ''}{fmt_num(v, digits)}%"


def fmt_digest_time(ts: Optional[datetime.datetime]) -> str:
    """Digest timestamps are stored as naive UTC; show them in Amsterdam time with its CET/CEST label."""
    if not ts:
        return ""
    return ts.replace(tzinfo=datetime.timezone.utc).astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M %Z")


def fmt_date_nl(dt: datetime.datetime, with_time: bool = True) -> str:
    """Short Dutch date like 'ma 9 nov 09:00'."""
    out = f"{NL_DAYS[dt.weekday()]} {dt.day} {NL_MONTHS[dt.month - 1]}"
    return f"{out} {dt.strftime('%H:%M')}" if with_time else out

SESSION_NL = {"OPEN": "opening", "CLOSE": "slot", "REVIEW": "review", "MANUAL_SCAN": "scan"}
PICK_STATUS_NL = {
    "TARGET": "🎯 Doel gehaald",
    "STOP": "🛑 Stop geraakt",
    "UP": "🟢 In de plus",
    "DOWN": "🔴 In de min",
    "FLAT": "⚪ Gelijk",
}


class ReviewTracker:
    """
    Tracks and archives daily market reviews (openings and closings)
    and compares past-week recommendations and portfolio evolution
    against newly arriving sessions.
    """

    @staticmethod
    def get_next_session_info(now_dt: Optional[datetime.datetime] = None) -> Dict[str, str]:
        """
        Calculates the exact chronological next scheduled session based on current CET time:
        - 09:00 CET: EU Market Open -> Next: 🇺🇸 US Market Open at 15:30 CET
        - 15:30 CET: US Market Open -> Next: 🇪🇺 European Market Close at 17:30 CET
        - 17:30 CET: EU Market Close -> Next: 🇺🇸 US Market Close at 22:00 CET
        - 22:00 CET: US Market Close -> Next: 🇪🇺 European Market Open tomorrow at 09:00 CET
        """
        tz = ZoneInfo(settings.TIMEZONE)
        if not now_dt:
            now_dt = datetime.datetime.now(tz)
        elif now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=tz)

        hour = now_dt.hour
        minute = now_dt.minute
        time_minutes = (hour * 60) + minute
        weekday = now_dt.weekday()  # Monday=0, Friday=4, Saturday=5, Sunday=6

        eu_open_min = (settings.EU_OPEN_HOUR * 60) + settings.EU_OPEN_MINUTE      # 09:00 = 540
        # US times follow New York, so they shift by 1h in the DST mismatch weeks (March / late October)
        us_open_dt, us_close_dt = us_session_local(now_dt)
        us_open_str = us_open_dt.strftime("%H:%M")
        us_close_str = us_close_dt.strftime("%H:%M")
        us_open_min = us_open_dt.hour * 60 + us_open_dt.minute                     # 15:30 = 930 normally
        eu_close_min = (settings.EU_CLOSE_HOUR * 60) + settings.EU_CLOSE_MINUTE    # 17:30 = 1050
        us_close_min = us_close_dt.hour * 60 + us_close_dt.minute                  # 22:00 = 1320 normally

        # On weekends (Sat/Sun) or Friday after US Close: Next session is Monday European Open!
        if weekday >= 5 or (weekday == 4 and time_minutes >= us_close_min):
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "EU-opening (maandag)",
                "time_str": f"maandag om {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 EU-opening maandag om {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
            }

        # Weekdays (Mon-Fri) chronological progression:
        # Strict < so a review sent at its own trigger time points to the following session
        if time_minutes < eu_open_min:
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "EU-opening",
                "time_str": f"{settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 EU-opening om {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
            }
        elif time_minutes < us_open_min:
            return {
                "market": "US",
                "session": "OPEN",
                "title": "US-opening",
                "time_str": f"{us_open_str} CET",
                "flag": "🇺🇸",
                "formatted": f"🇺🇸 US-opening om {us_open_str} CET"
            }
        elif time_minutes < eu_close_min:
            return {
                "market": "EU",
                "session": "CLOSE",
                "title": "EU-slot",
                "time_str": f"{settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 EU-slot om {settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET"
            }
        elif time_minutes < us_close_min:
            return {
                "market": "US",
                "session": "CLOSE",
                "title": "US-slot",
                "time_str": f"{us_close_str} CET",
                "flag": "🇺🇸",
                "formatted": f"🇺🇸 US-slot om {us_close_str} CET"
            }
        else:
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "EU-opening (morgen)",
                "time_str": f"morgen om {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 EU-opening morgen om {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
            }

    async def save_session_review(
        self,
        market: str,
        session_type: str,
        headline: str,
        content: str,
        top_picks: List[Dict[str, Any]],
        portfolio_state: Optional[Dict[str, Any]] = None,
        review_text: Optional[str] = None
    ) -> int:
        """Saves an archival record of the full market session review."""
        async with AsyncSessionLocal() as session:
            digest = MarketDigest(
                market=market.upper(),
                session=session_type.upper(),
                headline=headline,
                content=content,
                top_picks=top_picks,
                portfolio_state=portfolio_state,
                review_text=review_text,
                timestamp=datetime.datetime.utcnow()
            )
            session.add(digest)
            await session.commit()
            await session.refresh(digest)
            logger.info(f"Archived {market} {session_type} review (ID: #{digest.id})")
            return digest.id

    async def get_weekly_reviews(self, days: int = 7) -> List[Dict[str, Any]]:
        """Retrieves all market digests recorded within the past N days."""
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(MarketDigest)
                .where(MarketDigest.timestamp >= cutoff)
                .order_by(desc(MarketDigest.timestamp))
            )
            digests = res.scalars().all()
            return [
                {
                    "id": d.id,
                    "market": d.market,
                    "session": d.session,
                    "headline": d.headline,
                    "content": d.content,
                    "top_picks": d.top_picks or [],
                    "portfolio_state": d.portfolio_state or {},
                    "review_text": d.review_text or "",
                    "timestamp": fmt_digest_time(d.timestamp)
                }
                for d in digests
            ]

    async def compare_past_week_reviews(
        self,
        current_market: str = "EU",
        current_session: str = "OPEN",
        days: int = 7
    ) -> Dict[str, Any]:
        """
        Compares all recommendations and portfolio trajectory from past-week reviews
        against current live prices and portfolio value.
        """
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=days)
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(MarketDigest)
                .where(MarketDigest.timestamp >= cutoff)
                .order_by(desc(MarketDigest.timestamp))
            )
            digests = res.scalars().all()

        if not digests:
            return {
                "has_history": False,
                "message": "No historical reviews found in the past 7 days for comparison.",
                "total_reviews": 0,
                "tracked_picks": [],
                "win_rate_pct": 0.0,
                "text_summary": "• <i>Nog geen eerdere reviews in de afgelopen 7 dagen.</i>"
            }

        # 1. Gather all unique recommended picks from past week
        tracked_picks = []
        seen_tickers = set()

        for d in digests:
            session_label = f"{d.market} {SESSION_NL.get(d.session, d.session.lower())} {fmt_date_nl(d.timestamp, with_time=False) if d.timestamp else ''}".strip()
            picks = d.top_picks or []
            for p in picks:
                ticker = p.get("ticker")
                if not ticker or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)

                rec_price = p.get("price") or p.get("entry_trigger_price") or 0.0
                if rec_price <= 0:
                    continue

                # Live quote
                quote = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
                curr_price = quote["price"] if quote else rec_price

                pnl_pct = round(((curr_price / rec_price) - 1.0) * 100, 2)
                
                # Check target and stop
                tp = p.get("target_price") or p.get("risk", {}).get("take_profit_price")
                sl = p.get("stop_loss") or p.get("risk", {}).get("stop_loss_price")
                
                status = "UP" if pnl_pct > 0 else ("DOWN" if pnl_pct < 0 else "FLAT")
                if tp and curr_price >= tp:
                    status = "TARGET"
                elif sl and curr_price <= sl:
                    status = "STOP"
                status_badge = PICK_STATUS_NL[status]

                tracked_picks.append({
                    "ticker": ticker,
                    "name": p.get("name", ticker),
                    "action": p.get("action", "BUY"),
                    "score": p.get("consensus_score", 75),
                    "rec_session": session_label,
                    "rec_price": rec_price,
                    "current_price": curr_price,
                    "currency": p.get("currency", "EUR"),
                    "pnl_pct": pnl_pct,
                    "status": status,
                    "status_badge": status_badge,
                    "target_price": tp,
                    "stop_loss": sl
                })

        # 2. Performance Stats
        total_tracked = len(tracked_picks)
        positive_count = sum(1 for p in tracked_picks if p["pnl_pct"] > 0)
        win_rate = round((positive_count / total_tracked * 100), 1) if total_tracked > 0 else 0.0

        best_pick = max(tracked_picks, key=lambda x: x["pnl_pct"]) if tracked_picks else None
        worst_pick = min(tracked_picks, key=lambda x: x["pnl_pct"]) if tracked_picks else None

        # 3. Dynamic Mode-Aware Portfolio Evolution
        from src.trading.broker import active_broker
        curr_summary = await active_broker.get_portfolio_summary()
        curr_val = curr_summary.get("total_value_eur", 0.0)
        curr_mode = curr_summary.get("trading_mode", "PAPER")

        matching_digests = [
            d for d in digests
            if (d.portfolio_state or {}).get("trading_mode", "PAPER") == curr_mode
        ]

        # Deposits and withdrawals in the period are money moved, not performance
        if matching_digests:
            oldest_matching = matching_digests[-1]
            old_val = (oldest_matching.portfolio_state or {}).get("total_value_eur") or curr_val
            flows = 0.0
            if curr_mode == "LIVE" and oldest_matching.timestamp:
                from src.trading.cash_flows import net_cash_flows_eur
                flows = await net_cash_flows_eur(is_live=True, since=oldest_matching.timestamp)
            old_val += flows
        else:
            old_val = curr_summary.get("invested_capital_eur") or curr_summary.get("initial_capital_eur") or curr_val

        delta_val = round(curr_val - old_val, 2)
        delta_pct = round(((curr_val / old_val) - 1.0) * 100, 2) if old_val > 0 else 0.0

        # 4. Telegram text: plain Dutch, one line per pick
        pick_lines = [
            f"   • <b>{p['ticker']}</b> ({p['rec_session']}): {fmt_num(p['rec_price'])} ➔ {fmt_num(p['current_price'])} {p['currency']} "
            f"(<b>{fmt_pct(p['pnl_pct'], 2)}</b>) · {p['status_badge']}"
            for p in tracked_picks[:4]
        ]
        picks_str = "\n".join(pick_lines) if pick_lines else "   • <i>Nog geen adviezen om te volgen.</i>"

        text_summary = (
            f"📜 <b>Hoe deden eerdere adviezen het?</b> (afgelopen 7 dagen, {len(digests)} reviews)\n"
            f"• <b>{positive_count} van {total_tracked}</b> adviezen staan nu in de plus ({fmt_num(win_rate, 0)}%)\n"
            f"• Portefeuille deze week: <b>{fmt_eur(delta_val, signed=True)} ({fmt_pct(delta_pct, 2)})</b>\n"
            f"• Koers bij advies ➔ koers nu:\n"
            f"{picks_str}"
        )

        return {
            "has_history": True,
            "total_reviews": len(digests),
            "total_picks_tracked": total_tracked,
            "positive_picks_count": positive_count,
            "win_rate_pct": win_rate,
            "portfolio_delta_eur": delta_val,
            "portfolio_delta_pct": delta_pct,
            "portfolio_trajectory": f"{fmt_eur(old_val)} ➔ {fmt_eur(curr_val)}",
            "best_performer": best_pick,
            "worst_performer": worst_pick,
            "tracked_picks": tracked_picks,
            "text_summary": text_summary
        }

review_tracker = ReviewTracker()
