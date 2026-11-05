"""UserHolding model and holdings helpers."""
import datetime

from sqlalchemy import delete, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import Position, SystemConfig, Trade, UserHolding
from src.trading import trading212
from src.data.currency import currency_service
from src.trading import holdings as holdings_mod
from src.trading.holdings import BACKFILL_KEY, backfill_user_holdings, holdings_value_eur, is_mine, list_holdings, mine_tickers, refresh_holding_prices
from tests.test_trading212_broker import FakeT212, _live

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


BF = ["SAP.DE", "MC.PA", "AAPL"]


def _pos(ticker, ccy, **kw):
    now = datetime.datetime.utcnow()
    return Position(ticker=ticker, name=ticker, currency=ccy, shares=4.0, entry_price_native=50.0,
                    current_price_native=55.0, highest_price_native=55.0, entry_price_eur=50.0,
                    current_price_eur=55.0, cost_basis_eur=200.0, status="OPEN", is_live=True,
                    opened_at=now, **kw)


async def _bf_clean():
    async with db_session.AsyncSessionLocal() as s:
        await s.execute(delete(Position).where(Position.ticker.in_(BF)))
        await s.execute(delete(Trade).where(Trade.ticker.in_(BF)))
        await s.execute(delete(UserHolding).where(UserHolding.ticker.in_(BF)))
        await s.execute(delete(SystemConfig).where(SystemConfig.key == BACKFILL_KEY))
        await s.commit()


async def _bf_seed():
    async with db_session.AsyncSessionLocal() as s:
        s.add_all([_pos("SAP.DE", "EUR"), _pos("MC.PA", "EUR"), _pos("AAPL", "USD", stop_loss_native=40.0)])
        s.add(Trade(ticker="MC.PA", order_type="BUY", shares=4.0, price_native=50.0, price_eur=50.0,
                    total_eur=200.0, is_live=True, executed_at=datetime.datetime.utcnow()))
        await s.commit()


async def _statuses():
    async with db_session.AsyncSessionLocal() as s:
        rows = (await s.execute(select(Position).where(Position.ticker.in_(BF)))).scalars().all()
        return {p.ticker: p.status for p in rows}


def _bf_live(monkeypatch):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(trading212, "get_client", lambda: FakeT212())


async def test_backfill_moves_only_planless_adopted_positions(monkeypatch):
    _bf_live(monkeypatch)
    try:
        await _bf_seed()
        assert await backfill_user_holdings() == ["SAP.DE"]
        assert await _statuses() == {"SAP.DE": "RELEASED", "MC.PA": "OPEN", "AAPL": "OPEN"}
        async with db_session.AsyncSessionLocal() as s:
            h = (await s.execute(select(UserHolding).where(UserHolding.ticker == "SAP.DE"))).scalars().one()
            assert (h.t212_ticker, h.shares, h.avg_price_native) == ("SAPd_EQ", 4.0, 50.0)
    finally:
        await _bf_clean()


async def test_backfill_runs_once(monkeypatch):
    _bf_live(monkeypatch)
    try:
        await _bf_seed()
        assert await backfill_user_holdings() == ["SAP.DE"]
        async with db_session.AsyncSessionLocal() as s:
            s.add(_pos("AAPL", "USD"))  # would move if the key were ignored
            await s.commit()
        assert await backfill_user_holdings() == []
        async with db_session.AsyncSessionLocal() as s:
            held = (await s.execute(select(UserHolding.ticker).where(UserHolding.ticker.in_(BF)))).scalars().all()
        assert held == ["SAP.DE"]
    finally:
        await _bf_clean()


async def test_backfill_skipped_in_paper_mode():
    try:
        await _bf_seed()
        assert await backfill_user_holdings() == []
        assert await _statuses() == {"SAP.DE": "OPEN", "MC.PA": "OPEN", "AAPL": "OPEN"}
        async with db_session.AsyncSessionLocal() as s:
            assert (await s.execute(select(SystemConfig).where(SystemConfig.key == BACKFILL_KEY))).first() is None
    finally:
        await _bf_clean()
