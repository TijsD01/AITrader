"""Money math for My holdings: out of bot sizing, inside the daily loss check."""
import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import CashFlow, PortfolioSnapshot, Position, UserHolding
from src.trading.live import LiveBroker
from tests.test_trading212_broker import _live, clean_snapshots  # noqa: F401

BOT = "MONEYBOT"
MINE = "MONEYMINE"


@pytest.fixture
async def clean_money(clean_snapshots):
    """No stray live snapshots or cash flows may move the numbers; remove this test's rows afterwards."""
    async def wipe():
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await s.execute(delete(CashFlow).where(CashFlow.is_live == True))
            await s.execute(delete(Position).where(Position.ticker == BOT))
            await s.execute(delete(UserHolding).where(UserHolding.ticker == MINE))
            await s.commit()
    await wipe()
    async with db_session.AsyncSessionLocal() as s:
        stray = (await s.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))).scalars().all()
        assert not stray, f"stray open live positions: {[p.ticker for p in stray]}"
        assert not (await s.execute(select(UserHolding))).scalars().all(), "stray user holdings"
    try:
        yield
    finally:
        await wipe()


def _mine_row(price_eur, shares=10.0, cost=1500.0):
    return UserHolding(ticker=MINE, t212_ticker=MINE + "_US_EQ", name=MINE, currency="EUR", shares=shares,
                       avg_price_native=cost / shares, current_price_native=price_eur,
                       current_price_eur=price_eur, cost_basis_eur=cost)


def _bot_position(price_eur=100.0, shares=3.0):
    return Position(ticker=BOT, name=BOT, currency="EUR", shares=shares, entry_price_native=price_eur,
                    current_price_native=price_eur, highest_price_native=price_eur, entry_price_eur=price_eur,
                    current_price_eur=price_eur, cost_basis_eur=shares * price_eur, unrealized_pnl_eur=0.0,
                    status="OPEN", is_live=True)


async def _daily_loss_with_mine_at(monkeypatch, price_eur):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    async with db_session.AsyncSessionLocal() as s:
        # Yesterday: €500 cash + Mine worth €1,500 = €2,000 (snapshots hold the whole account)
        s.add(PortfolioSnapshot(cash_eur=500.0, equity_eur=1500.0, total_value_eur=2000.0, is_live=True,
                                broker="trading212", timestamp=now - datetime.timedelta(days=1)))
        s.add(PortfolioSnapshot(cash_eur=500.0, equity_eur=10 * price_eur, total_value_eur=500.0 + 10 * price_eur,
                                is_live=True, broker="trading212", timestamp=now))
        s.add(_mine_row(price_eur))
        await s.commit()
    return await LiveBroker().get_daily_loss_eur()


async def test_daily_loss_ignores_flat_mine_holdings(monkeypatch, clean_money):
    assert await _daily_loss_with_mine_at(monkeypatch, 150.0) == 0.0


async def test_daily_loss_counts_fall_in_mine_holdings(monkeypatch, clean_money):
    assert await _daily_loss_with_mine_at(monkeypatch, 140.0) == 100.0


async def _summary(broker, account_values):
    with patch.object(broker, "_fetch_t212_account_values", AsyncMock(side_effect=account_values)), \
         patch("src.trading.live.scan_live_cash_flows", AsyncMock(return_value=0)), \
         patch.object(broker, "refresh_positions_pnl", AsyncMock()), \
         patch.object(broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])):
        return await broker.get_portfolio_summary()


async def test_summary_reports_bot_and_mine_split(monkeypatch, clean_money):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    async with db_session.AsyncSessionLocal() as s:
        s.add(_bot_position())      # €300
        s.add(_mine_row(150.0))     # €1,500
        await s.commit()
    # T212 unreachable and no snapshot: the local fallback total must include Mine holdings
    summary = await _summary(LiveBroker(), RuntimeError("T212 down"))
    assert summary["bot_equity_eur"] == 300.0
    assert summary["user_holdings_eur"] == 1500.0
    assert summary["open_positions_count"] == 1
    assert summary["unrealized_pnl_eur"] == 0.0
    assert summary["value_source"] == "local"
    assert summary["total_value_eur"] == 1800.0  # €0 cash + €300 bot + €1,500 Mine


async def test_sizing_ignores_mine_holdings(monkeypatch, clean_money):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(settings, "MAX_DEPLOYABLE_CAPITAL_EUR", 1000.0)
    broker = LiveBroker()
    async with db_session.AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=2000.0, equity_eur=0.0, total_value_eur=2000.0, is_live=True, broker="trading212"))
        await s.commit()
    without = await _summary(broker, [(2000.0, 2000.0, 0.0)])
    async with db_session.AsyncSessionLocal() as s:
        s.add(_mine_row(500.0, cost=5000.0))
        await s.commit()
    with_mine = await _summary(broker, [(2000.0, 7000.0, 5000.0)])
    assert without["usable_capital_eur"] > 0
    assert with_mine["usable_capital_eur"] == without["usable_capital_eur"]
    assert with_mine["available_cash_eur"] == without["available_cash_eur"]
    assert with_mine["user_holdings_eur"] == 5000.0 and with_mine["bot_equity_eur"] == 0.0
