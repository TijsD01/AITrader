"""Trading 212 sync: plan-less holdings go to My holdings, and the bot never books them."""
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete, select

from src.db.models import OrderTrigger, PortfolioSnapshot, Position, Trade, UserHolding
from src.db.session import AsyncSessionLocal
from src.trading.live import LiveBroker
from tests.test_trading212_broker import FakeT212, _held, _live, clean_snapshots, fake  # noqa: F401

TICKERS = ["SAP.DE", "ASML.AS"]


def _patches(broker):
    """The patch set of test_sync_adopts_untracked_and_closes_only_when_confirmed."""
    return (
        patch.object(broker, "refresh_positions_pnl", AsyncMock()),
        patch.object(broker, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0})),
        patch("src.trading.live.market_data_provider.get_stock_data", return_value=None),
        patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()),
    )


async def _sync(broker):
    p1, p2, p3, p4 = _patches(broker)
    with p1, p2, p3, p4 as notify:
        await broker.sync_account_state()
    return notify


async def _rows(model, ticker):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(model).where(model.ticker == ticker))).scalars().all()


async def _add_mine(ticker="SAP.DE", t212="SAPd_EQ", shares=3.0, avg=120.0):
    async with AsyncSessionLocal() as s:
        s.add(UserHolding(ticker=ticker, t212_ticker=t212, name=ticker, currency="EUR", shares=shares,
                          avg_price_native=avg, current_price_native=avg, current_price_eur=avg, cost_basis_eur=avg * shares))
        await s.commit()


async def _add_plan(ticker="ASML.AS"):
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=ticker, target_price=600.0, current_price_at_creation=610.0, stop_loss=570.0,
                           take_profit=700.0, allocation_eur=600.0, status="TRIGGERED", is_live=True))
        await s.commit()


async def _cleanup():
    async with AsyncSessionLocal() as s:
        for model in (Position, UserHolding, Trade, OrderTrigger):
            await s.execute(delete(model).where(model.ticker.in_(TICKERS)))
        await s.commit()


async def test_sync_planless_holding_becomes_mine(fake, clean_snapshots, monkeypatch):
    _live(monkeypatch)
    held = _held("SAPd_EQ", 3.0, avg=120.0)
    held["currentPrice"] = 125.0
    fake.positions_answers = [[held]]
    try:
        notify = await _sync(LiveBroker())
        rows = await _rows(UserHolding, "SAP.DE")
        assert [(r.t212_ticker, r.shares, r.current_price_native, r.current_price_eur, r.cost_basis_eur) for r in rows] == \
            [("SAPd_EQ", 3.0, 125.0, 125.0, 360.0)]
        assert await _rows(Position, "SAP.DE") == []
        assert not [p for p in fake.posts if p[0] == "stop"]
        texts = [c.args[0] for c in notify.await_args_list]
        assert any("New holding marked as yours: 3 SAP.DE" in t for t in texts)
    finally:
        await _cleanup()


async def test_sync_holding_with_plan_still_adopted_as_bot(fake, clean_snapshots, monkeypatch):
    _live(monkeypatch)
    await _add_plan()
    fake.positions_answers = [[_held("ASMLa_EQ", 1.0, avg=600.0)]]
    try:
        await _sync(LiveBroker())
        pos = await _rows(Position, "ASML.AS")
        assert [(p.shares, p.stop_loss_native) for p in pos] == [(1.0, 570.0)]
        assert await _rows(UserHolding, "ASML.AS") == []
    finally:
        await _cleanup()


async def test_sync_updates_mine_shares_on_partial_sale(fake, clean_snapshots, monkeypatch):
    _live(monkeypatch)
    await _add_mine()
    fake.positions_answers = [[_held("SAPd_EQ", 1.0, avg=120.0)]]
    try:
        await _sync(LiveBroker())
        rows = await _rows(UserHolding, "SAP.DE")
        assert [(r.shares, r.cost_basis_eur, r.current_price_native) for r in rows] == [(1.0, 120.0, 120.0)]
        assert await _rows(Trade, "SAP.DE") == []
        assert await _rows(Position, "SAP.DE") == []
    finally:
        await _cleanup()


async def test_sync_removes_mine_only_when_confirmed_gone(fake, clean_snapshots, monkeypatch):
    _live(monkeypatch)
    await _add_mine()
    broker = LiveBroker()
    try:
        # First answer misses it, the confirming second answer still holds it: row kept
        fake.positions_answers = [[], [_held("SAPd_EQ", 3.0, avg=120.0)]]
        await _sync(broker)
        assert len(await _rows(UserHolding, "SAP.DE")) == 1

        # Both answers miss it: row deleted, no bot trade booked
        fake.positions_calls = 0
        fake.positions_answers = [[]]
        await _sync(broker)
        assert await _rows(UserHolding, "SAP.DE") == []
        assert await _rows(Trade, "SAP.DE") == []
    finally:
        await _cleanup()


async def test_snapshot_counts_bot_positions_only(fake, clean_snapshots, monkeypatch):
    _live(monkeypatch)
    await _add_mine()
    await _add_plan()
    fake.positions_answers = [[_held("SAPd_EQ", 3.0, avg=120.0), _held("ASMLa_EQ", 1.0, avg=600.0)]]
    try:
        await _sync(LiveBroker())
        async with AsyncSessionLocal() as s:
            snap = (await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
                                    .order_by(PortfolioSnapshot.id.desc()))).scalars().first()
        assert snap.open_positions_count == 1
    finally:
        await _cleanup()


async def test_refresh_positions_pnl_refreshes_holdings_without_bot_positions(fake, monkeypatch):
    _live(monkeypatch)
    await _add_mine()
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(UserHolding).where(UserHolding.ticker == "SAP.DE"))).scalars().one()
        row.current_price_native = None
        await s.commit()
    try:
        with patch("src.trading.holdings.market_data_provider.get_stock_data", return_value={"price": 130.0}):
            await LiveBroker().refresh_positions_pnl()
        rows = await _rows(UserHolding, "SAP.DE")
        assert (rows[0].current_price_native, rows[0].current_price_eur) == (130.0, 130.0)
    finally:
        await _cleanup()
