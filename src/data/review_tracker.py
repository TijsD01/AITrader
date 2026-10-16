import logging
import asyncio
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional
from sqlalchemy import select, desc

from config.settings import settings
from src.db.session import AsyncSessionLocal
from src.db.models import MarketDigest, PortfolioSnapshot
from src.data.market import market_data_provider

logger = logging.getLogger(__name__)

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
        us_open_min = (settings.US_OPEN_HOUR * 60) + settings.US_OPEN_MINUTE      # 15:30 = 930
        eu_close_min = (settings.EU_CLOSE_HOUR * 60) + settings.EU_CLOSE_MINUTE    # 17:30 = 1050
        us_close_min = (settings.US_CLOSE_HOUR * 60) + settings.US_CLOSE_MINUTE    # 22:00 = 1320

        # On weekends (Sat/Sun) or Friday after US Close: Next session is Monday European Open!
        if weekday >= 5 or (weekday == 4 and time_minutes >= us_close_min):
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "European Market Open (Monday)",
                "time_str": f"Monday at {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 European Market Open Monday at {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
            }

        # Weekdays (Mon-Fri) chronological progression:
        # Strict < so a review sent at its own trigger time points to the following session
        if time_minutes < eu_open_min:
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "European Market Open",
                "time_str": f"{settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 European Market Open at {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
            }
        elif time_minutes < us_open_min:
            return {
                "market": "US",
                "session": "OPEN",
                "title": "US Market Open",
                "time_str": f"{settings.US_OPEN_HOUR:02d}:{settings.US_OPEN_MINUTE:02d} CET",
                "flag": "🇺🇸",
                "formatted": f"🇺🇸 US Market Open at {settings.US_OPEN_HOUR:02d}:{settings.US_OPEN_MINUTE:02d} CET"
            }
        elif time_minutes < eu_close_min:
            return {
                "market": "EU",
                "session": "CLOSE",
                "title": "European Market Close",
                "time_str": f"{settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 European Market Close at {settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET"
            }
        elif time_minutes < us_close_min:
            return {
                "market": "US",
                "session": "CLOSE",
                "title": "US Market Close",
                "time_str": f"{settings.US_CLOSE_HOUR:02d}:{settings.US_CLOSE_MINUTE:02d} CET",
                "flag": "🇺🇸",
                "formatted": f"🇺🇸 US Market Close at {settings.US_CLOSE_HOUR:02d}:{settings.US_CLOSE_MINUTE:02d} CET"
            }
        else:
            return {
                "market": "EU",
                "session": "OPEN",
                "title": "European Market Open (Tomorrow)",
                "time_str": f"tomorrow at {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET",
                "flag": "🇪🇺",
                "formatted": f"🇪🇺 European Market Open tomorrow at {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} CET"
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
                    "timestamp": d.timestamp.strftime("%Y-%m-%d %H:%M CET") if d.timestamp else ""
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
                "text_summary": "• <i>No previous session reviews recorded in the past 7 days yet.</i>"
            }

        # 1. Gather all unique recommended picks from past week
        tracked_picks = []
        seen_tickers = set()

        for d in digests:
            session_label = f"{d.market} {d.session} ({d.timestamp.strftime('%b %d') if d.timestamp else ''})"
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
                
                status_badge = "🟢 IN PROFIT" if pnl_pct > 0 else ("🔴 PULLING BACK" if pnl_pct < 0 else "⚪ EVEN")
                if tp and curr_price >= tp:
                    status_badge = "🎯 TARGET HIT"
                elif sl and curr_price <= sl:
                    status_badge = "🛑 STOP HIT"

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
        val_sign = "+" if delta_val >= 0 else ""

        # 4. Telegram Formatted Text Section
        pick_lines = []
        for p in tracked_picks[:4]:
            sign = "+" if p["pnl_pct"] >= 0 else ""
            pick_lines.append(
                f"   • <b>{p['ticker']}</b> ({p['rec_session']}): {p['rec_price']} ➔ <b>{p['current_price']} {p['currency']}</b> "
                f"({sign}{p['pnl_pct']}%) [{p['status_badge']}]"
            )

        picks_str = "\n".join(pick_lines) if pick_lines else "   • <i>No picks tracked yet.</i>"

        text_summary = (
            f"📜 <b>PAST-WEEK REVIEWS & PICK PERFORMANCE ({len(digests)} sessions tracked):</b>\n"
            f"• Recommendation Win Rate: <b>{win_rate}%</b> ({positive_count}/{total_tracked} picks in profit)\n"
            f"• Portfolio Change: <b>{val_sign}€{delta_val:.2f} ({val_sign}{delta_pct}%)</b> vs past week\n"
            f"• Pick Follow-Through:\n"
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
            "portfolio_trajectory": f"€{old_val:,.2f} ➔ €{curr_val:,.2f} ({val_sign}€{delta_val:.2f} / {val_sign}{delta_pct}%)",
            "best_performer": best_pick,
            "worst_performer": worst_pick,
            "tracked_picks": tracked_picks,
            "text_summary": text_summary
        }

review_tracker = ReviewTracker()
