"""UserHolding model and holdings helpers."""
from sqlalchemy import delete, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import UserHolding
from src.data.currency import currency_service
from src.trading import holdings as holdings_mod
from src.trading.holdings import holdings_value_eur, is_mine, list_holdings, mine_tickers, refresh_holding_prices
from tests.test_trading212_broker import _live

TICKERS = ["KOHOLD", "VWHOLD.AS"]


def _row(ticker, shares, price_eur, cost):
    return UserHolding(ticker=ticker, t212_ticker=ticker + "_T212", name=ticker, currency="EUR",
                       shares=shares, avg_price_native=cost / shares, current_price_native=price_eur,
                       current_price_eur=price_eur, cost_basis_eur=cost)


async def _clean():
    async with db_session.AsyncSessionLocal() as s:
        await s.execute(delete(UserHolding).where(UserHolding.ticker.in_(TICKERS)))
        await s.commit()


async def test_mine_tickers_empty_when_feature_off():
    try:
        async with db_session.AsyncSessionLocal() as s:
            s.add(_row("KOHOLD", 24, 62.10, 1390.0))
            await s.commit()
            assert await mine_tickers(s) == set()
        assert await list_holdings() == []
    finally:
        await _clean()


async def test_mine_tickers_and_value_when_live_t212(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    try:
        async with db_session.AsyncSessionLocal() as s:
            s.add_all([_row("KOHOLD", 24, 62.10, 1390.0), _row("VWHOLD.AS", 5, 100.0, 450.0)])
            await s.commit()
            assert await mine_tickers(s) == {"KOHOLD", "VWHOLD.AS"}
            assert await is_mine(s, "kohold") is True
            assert await holdings_value_eur(s) == 24 * 62.10 + 5 * 100.0
        rows = await list_holdings()
        assert rows[0]["ticker"] == "KOHOLD"
        assert rows[0]["unrealized_pnl_eur"] == round(24 * 62.10 - 1390.00, 2)
    finally:
        await _clean()


async def _refresh(monkeypatch, is_open):
    monkeypatch.setattr(holdings_mod, "check_market_hours", lambda t: (is_open, ""))
    monkeypatch.setattr(holdings_mod.market_data_provider, "get_stock_data", lambda t: {"price": 70.0})
    async with db_session.AsyncSessionLocal() as s:
        s.add(_row("KOHOLD", 24, 62.10, 1390.0))
        await s.commit()
    async with db_session.AsyncSessionLocal() as s:
        await refresh_holding_prices(s)
        await s.commit()
    async with db_session.AsyncSessionLocal() as s:
        return (await s.execute(select(UserHolding).where(UserHolding.ticker == "KOHOLD"))).scalars().one()


async def test_refresh_holding_prices_updates_when_market_open(monkeypatch):
    try:
        h = await _refresh(monkeypatch, True)
        assert h.current_price_native == 70.0
        assert h.current_price_eur == currency_service.to_eur(70.0, h.currency)
    finally:
        await _clean()


async def test_refresh_holding_prices_skips_when_market_closed(monkeypatch):
    try:
        h = await _refresh(monkeypatch, False)
        assert (h.current_price_native, h.current_price_eur) == (62.10, 62.10)
    finally:
        await _clean()
