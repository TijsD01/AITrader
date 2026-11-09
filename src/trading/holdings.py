"""User-owned Trading 212 holdings the bot never trades (live T212 only)."""
import asyncio
import datetime
import logging
import math
from typing import Optional

from sqlalchemy import desc, select

from config.settings import settings
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.data.market_hours import quotes_moving
from src.db import session as db_session
from src.db.models import PortfolioSnapshot, Position, SystemConfig, Trade, UserHolding
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
    if not holdings_enabled():
        return
    # Trading 212's own price first: it holds the shares and quotes them in its extended hours
    t212_prices = {}
    client = trading212.get_client()
    if client:
        try:
            t212_prices = {bp["instrument"]["ticker"]: bp.get("currentPrice") for bp in await client.positions()}
        except Exception as e:
            logger.warning(f"Trading 212 positions for holding prices failed, using Yahoo: {e}")
    res = await session.execute(select(UserHolding))
    for h in res.scalars().all():
        price = float(t212_prices.get(h.t212_ticker) or 0)
        if price > 0:
            h.current_price_native = price
            h.current_price_eur = currency_service.to_eur(price, h.currency)
            continue
        if not quotes_moving(h.ticker) and h.current_price_native and h.current_price_native > 0:
            continue
        data = await asyncio.to_thread(market_data_provider.get_stock_data, h.ticker)
        price = data["price"] if data else None
        if price:
            h.current_price_native = float(price)
            h.current_price_eur = currency_service.to_eur(float(price), h.currency)


def _reanchor(report: dict, base: float, label: str) -> Optional[tuple]:
    """(score, stop, target, label) with the report's stop distance and R:R moved from its entry trigger to base."""
    risk = report.get("risk") or {}
    try:
        entry, sl, rr = float(risk["entry_trigger_price"]), float(risk["stop_loss_price"]), float(risk["risk_reward_ratio"])
        score = float(report["consensus_score"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 < sl < entry or rr <= 0:
        return None
    stop = base * sl / entry
    return score, stop, base + (base - stop) * rr, label


def _price(p: float) -> float:
    return round(p, 2 if p >= 1 else 4)  # same rule as LiveBroker._t212_price, so the broker stop matches


def pick_bot_levels(candidates: list[tuple], current_price: float) -> tuple[float, float, str]:
    """
    Highest-score (score, stop, target, label) set with stop < price < target after rounding; else the default %.
    A score of inf (a buy plan) wins whenever it is valid.
    """
    rounded = [(c[0], _price(c[1]), _price(c[2]), c[3]) for c in candidates if c[1] and c[2]]
    valid = [c for c in rounded if c[1] < current_price < c[2]]
    if valid:
        score, stop, target, label = max(valid, key=lambda c: c[0])
        return stop, target, label if math.isinf(score) else f"{label}, score {score:g}"
    return (_price(current_price * (1 - settings.DEFAULT_STOP_LOSS_PCT)),
            _price(current_price * (1 + settings.DEFAULT_TAKE_PROFIT_PCT)), "default")


async def committee_levels(ticker: str, avg_price: float, currency: str,
                           run_committee: bool = True) -> tuple[list[tuple], Optional[float]]:
    """
    Committee stop/target sets for a holding the bot takes over: one anchored on the buy price, one on the market price.
    Returns (candidates, market price); run_committee=False only fetches the price.
    Never raises: on any failure the caller falls back to default levels.
    """
    from src.agents.committee import investment_committee
    try:
        data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
        # Levels in another currency than the holding would put the broker stop at a wrong price.
        if not data or not data.get("price") or trading212._ccy(data.get("currency")) != trading212._ccy(currency):
            return [], None
        market = float(data["price"])
        if not run_committee:
            return [], market
        bases = [(market, "committee, current price")]
        if avg_price and avg_price > 0:
            bases.append((float(avg_price), "committee, buy price"))
        async with db_session.AsyncSessionLocal() as session:
            equity = (await session.execute(select(PortfolioSnapshot.total_value_eur).where(PortfolioSnapshot.is_live == True)
                                            .order_by(desc(PortfolioSnapshot.timestamp)).limit(1))).scalar() or 10000.0
        reports = await asyncio.gather(*(investment_committee.evaluate_ticker(dict(data, price=b), equity) for b, _ in bases),
                                       return_exceptions=True)
    except Exception as e:
        logger.error(f"Committee levels for {ticker} failed: {e}")
        return [], None
    out = []
    for (base, label), rep in zip(bases, reports):
        if isinstance(rep, BaseException):
            logger.error(f"Committee run for {ticker} ({label}) failed: {rep}")
        elif isinstance(rep, dict) and (c := _reanchor(rep, base, label)):
            out.append(c)
    return out, market


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
