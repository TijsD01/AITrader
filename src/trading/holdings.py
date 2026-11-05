"""User-owned Trading 212 holdings the bot never trades (live T212 only)."""
import asyncio

from sqlalchemy import select

from config.settings import settings
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.data.market_hours import check_market_hours
from src.db import session as db_session
from src.db.models import UserHolding


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
    # No gate: rows only exist from live T212 sync.
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
    await session.commit()
