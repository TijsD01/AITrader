"""User-owned Trading 212 holdings the bot never trades (live T212 only)."""
import asyncio
import datetime
import logging

from sqlalchemy import select

from config.settings import settings
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.data.market_hours import check_market_hours
from src.db import session as db_session
from src.db.models import Position, SystemConfig, Trade, UserHolding
from src.trading import trading212

logger = logging.getLogger(__name__)

BACKFILL_KEY = "user_holdings_backfill_done"


def holdings_enabled() -> bool:
    return (settings.TRADING_MODE.upper() == "LIVE" and bool(settings.LIVE_TRADING_ENABLED)
            and settings.LIVE_BROKER.lower() == "trading212")


async def mine_tickers(session) -> set[str]:
    if not holdings_enabled():
        return set()
    res = await session.execute(select(UserHolding.ticker))
    return {t.upper() for t in res.scalars().all()}


async def is_mine(session, ticker: str) -> bool:
    return ticker.upper() in await mine_tickers(session)


def _value_eur(h: UserHolding) -> float:
    return h.shares * (h.current_price_eur or 0.0)


async def holdings_value_eur(session) -> float:
    """EUR value of My holdings; 0 when the feature is off, so leftover rows after a broker switch never count."""
    if not holdings_enabled():
        return 0.0
    res = await session.execute(select(UserHolding))
    return sum(_value_eur(h) for h in res.scalars().all())


def holding_dict(h: UserHolding) -> dict:
    value = _value_eur(h)
    pnl = value - h.cost_basis_eur
    return {
        "ticker": h.ticker,
        "name": h.name,
        "currency": h.currency,
        "shares": h.shares,
        "avg_price_native": round(h.avg_price_native, 2),
        "current_price_native": round(h.current_price_native or 0.0, 2),
        "current_value_eur": round(value, 2),
        "cost_basis_eur": round(h.cost_basis_eur, 2),
        "unrealized_pnl_eur": round(pnl, 2),
        "unrealized_pnl_pct": round(pnl / h.cost_basis_eur * 100, 2) if h.cost_basis_eur else 0.0,
    }


async def list_holdings() -> list[dict]:
    if not holdings_enabled():
        return []
    async with db_session.AsyncSessionLocal() as session:
        res = await session.execute(select(UserHolding))
        rows = sorted(res.scalars().all(), key=_value_eur, reverse=True)
        return [holding_dict(h) for h in rows]


async def refresh_holding_prices(session) -> None:
    """Update row prices in place; the caller commits."""
    res = await session.execute(select(UserHolding))
    for h in res.scalars().all():
        is_open, _ = check_market_hours(h.ticker)
        if not is_open and h.current_price_native and h.current_price_native > 0:
            continue
        data = await asyncio.to_thread(market_data_provider.get_stock_data, h.ticker)
        price = data["price"] if data else None
        if price:
            h.current_price_native = float(price)
            h.current_price_eur = currency_service.to_eur(float(price), h.currency)


async def backfill_user_holdings() -> list[str]:
    """Once: move plan-less live positions the old sync adopted to My holdings. Touches no broker orders."""
    if not holdings_enabled():
        return []
    client = trading212.get_client()
    if not client:
        return []  # key stays unset, so it runs again on the next start
    moved: list[str] = []
    async with db_session.AsyncSessionLocal() as session:
        done = await session.execute(select(SystemConfig).where(SystemConfig.key == BACKFILL_KEY))
        if done.scalars().first():
            return []
        res = await session.execute(select(Position).where(
            Position.status == "OPEN", Position.is_live == True, Position.stop_loss_native.is_(None),
            Position.take_profit_native.is_(None), Position.broker_order_id.is_(None)))
        for pos in res.scalars().all():
            since = (pos.opened_at or datetime.datetime.utcnow()) - datetime.timedelta(minutes=10)
            bought = await session.execute(select(Trade.id).where(
                Trade.ticker == pos.ticker, Trade.order_type == "BUY", Trade.is_live == True,
                Trade.executed_at >= since).limit(1))
            if bought.first():
                continue
            # A lookup error propagates before the commit, so nothing is written and the next start retries.
            t212_ticker = await client.resolve_ticker(pos.ticker, pos.currency)
            if not t212_ticker:
                logger.warning(f"Backfill skipped {pos.ticker}: no unique Trading 212 instrument.")
                continue
            session.add(UserHolding(
                ticker=pos.ticker, t212_ticker=t212_ticker, name=pos.name, currency=pos.currency, shares=pos.shares,
                avg_price_native=pos.entry_price_native, current_price_native=pos.current_price_native,
                current_price_eur=pos.current_price_eur, cost_basis_eur=pos.cost_basis_eur))
            pos.status = "RELEASED"
            moved.append(pos.ticker)
        session.add(SystemConfig(key=BACKFILL_KEY, value="true"))
        await session.commit()
    if moved:
        try:
            from src.telegram.bot import telegram_bot
            await telegram_bot.send_message(f"Moved to My holdings: {', '.join(moved)}. Flip any back to Bot on the Home tab.")
        except Exception as e:
            logger.error(f"Failed to send backfill alert: {e}")
    return moved
