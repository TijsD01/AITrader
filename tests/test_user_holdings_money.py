"""Money math for My holdings: out of bot sizing, inside the daily loss check."""
import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import CashFlow, OrderTrigger, PortfolioSnapshot, Position, UserHolding
from src.trading.live import LiveBroker
from tests.test_trading212_broker import _cleanup, _held, _live, _with_stop, clean_snapshots, fake  # noqa: F401

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


async def test_leftover_holdings_ignored_after_switch_to_alpaca(monkeypatch, clean_money):
    _live(monkeypatch)
    monkeypatch.setattr(settings, "LIVE_BROKER", "alpaca")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    broker = LiveBroker()
    async with db_session.AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=2000.0, equity_eur=0.0, total_value_eur=2000.0, is_live=True,
                                broker="alpaca", timestamp=now - datetime.timedelta(days=1)))
        s.add(PortfolioSnapshot(cash_eur=1900.0, equity_eur=0.0, total_value_eur=1900.0, is_live=True,
                                broker="alpaca", timestamp=now))
        s.add(_bot_position())
        await s.commit()
    loss_before = await broker.get_daily_loss_eur()
    summary_before = await _summary(broker, RuntimeError("not called for alpaca"))
    async with db_session.AsyncSessionLocal() as s:
        s.add(_mine_row(150.0))  # left over from the Trading 212 days
        await s.commit()
    assert await broker.get_daily_loss_eur() == loss_before == 0.0  # €1,900 cash + €300 bot vs €2,000
    summary = await _summary(broker, RuntimeError("not called for alpaca"))
    assert summary["user_holdings_eur"] == 0.0
    for key in ("total_value_eur", "equity_eur", "bot_equity_eur", "daily_loss_eur", "usable_capital_eur"):
        assert summary[key] == summary_before[key], key


async def test_sell_snapshot_holds_mine_and_daily_loss_stays_flat(fake, monkeypatch, clean_money):
    _live(monkeypatch)
    _with_stop(fake)
    fake.positions_answers = [[_held("ASMLa_EQ", 3.0)]]
    async with db_session.AsyncSessionLocal() as s:
        s.add(Position(ticker="ASML.AS", name="ASML.AS", currency="EUR", shares=3.0, entry_price_native=100.0,
                       current_price_native=100.0, highest_price_native=100.0, entry_price_eur=100.0,
                       current_price_eur=100.0, cost_basis_eur=300.0, status="OPEN", is_live=True))
        s.add(_bot_position())      # €300 stays open
        s.add(_mine_row(150.0))     # €1,500
        await s.commit()
    try:
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value={"price": 105.0}), \
             patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()):
            assert (await LiveBroker().execute_sell("ASML.AS"))["success"]
        async with db_session.AsyncSessionLocal() as s:
            snap = (await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
                                    .order_by(PortfolioSnapshot.id.desc()))).scalars().first()
            assert snap.equity_eur == 1800.0
            assert snap.total_value_eur == round(snap.cash_eur + 300.0 + 1500.0, 2)
            # The sell snapshot becomes yesterday's base: flat prices are no loss
            snap.timestamp = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=1)
            await s.commit()
        assert await LiveBroker().get_daily_loss_eur() == 0.0
    finally:
        await _cleanup("ASML.AS")


async def _cash_sync_after_own_buy(fake, monkeypatch, cash, total, invested, held):
    """Yesterday €1,500 cash, nothing held. Today the 3-minute cash sync sees T212 after a buy in its own app."""
    _live(monkeypatch)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    async with db_session.AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=1500.0, equity_eur=0.0, total_value_eur=1500.0, is_live=True,
                                broker="trading212", timestamp=now - datetime.timedelta(days=1)))
        await s.commit()

    async def summary():
        return {"currency": "EUR", "totalValue": total,
                "cash": {"availableToTrade": cash, "reservedForOrders": 0.0}, "investments": {"currentValue": invested}}
    monkeypatch.setattr(fake, "account_summary", summary)
    monkeypatch.setattr("src.db.session.set_kill_switch", AsyncMock())
    fake.positions_answers = [held]
    broker = LiveBroker()
    with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()):
        await broker.sync_cash_balance()
        return await broker.enforce_daily_loss_limit()


async def test_own_buy_in_t212_app_does_not_trip_kill_switch(fake, monkeypatch, clean_money):
    # €1,000 of SAP bought by hand: cash fell, the account did not
    sap = dict(_held("SAPd_EQ", 10.0, avg=100.0), currentPrice=100.0)
    try:
        assert await _cash_sync_after_own_buy(fake, monkeypatch, 500.0, 1500.0, 1000.0, [sap]) is None
        async with db_session.AsyncSessionLocal() as s:
            rows = (await s.execute(select(UserHolding).where(UserHolding.ticker == "SAP.DE"))).scalars().all()
        assert [(r.shares, r.current_price_eur) for r in rows] == [(10.0, 100.0)]
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(UserHolding).where(UserHolding.ticker == "SAP.DE"))
            await s.commit()


async def test_real_cash_loss_still_trips_kill_switch_on_cash_sync(fake, monkeypatch, clean_money):
    # Nothing held, €600 gone: a real loss
    assert await _cash_sync_after_own_buy(fake, monkeypatch, 900.0, 900.0, 0.0, []) is not None


async def _live_snapshot_count():
    async with db_session.AsyncSessionLocal() as s:
        return len((await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))).scalars().all())


async def test_resting_entry_fill_writes_no_cash_only_snapshot(fake, monkeypatch, clean_money):
    # The bot's own resting buy filled: sync_resting_entries books it, so the cash sync must not record the cash drop alone
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", True)
    async with db_session.AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker="SAP.DE", target_price=100.0, current_price_at_creation=101.0, stop_loss=95.0,
                           take_profit=110.0, allocation_eur=1000.0, currency="EUR", trigger_type="PULLBACK_BUY",
                           status="PENDING", is_live=True, broker_order_id="t212_1"))
        await s.commit()
    sap = dict(_held("SAPd_EQ", 10.0, avg=100.0), currentPrice=100.0)
    try:
        assert await _cash_sync_after_own_buy(fake, monkeypatch, 500.0, 1500.0, 1000.0, [sap]) is None
        assert await _live_snapshot_count() == 1  # only yesterday's
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "SAP.DE"))
            await s.commit()


async def test_unbookable_holding_gets_one_full_sync_then_cash_syncs(fake, monkeypatch, clean_money):
    _live(monkeypatch)
    fake.positions_answers = [[_held("SAPd_EQ", 10.0, avg=100.0)]]
    broker = LiveBroker()
    full_sync = AsyncMock()  # books nothing, like a holding the reconcile skips by design
    monkeypatch.setattr(broker, "_sync_t212_account", full_sync)
    await broker.sync_cash_balance()
    await broker.sync_cash_balance()
    assert full_sync.await_count == 1
    assert await _live_snapshot_count() == 1  # the second run took the cash-only path


async def test_cash_sync_survives_failing_positions_call(fake, monkeypatch, clean_money):
    _live(monkeypatch)
    monkeypatch.setattr(fake, "positions", AsyncMock(side_effect=RuntimeError("429")))
    await LiveBroker().sync_cash_balance()
    assert await _live_snapshot_count() == 1
