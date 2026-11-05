"""Bot -> Mine flip: release_to_user cancels the bot's T212 stop and pending buys, then moves the position to My holdings."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import OrderTrigger, Position, Trade, UserHolding
from src.db.session import AsyncSessionLocal, init_db
from src.trading import trading212
from src.trading.live import LiveBroker
from tests.test_trading212_broker import FakeT212, _live, clean_snapshots, fake  # noqa: F401

TICKER = "ASML.AS"
STOP = {"id": 88, "type": "STOP", "ticker": "ASMLa_EQ", "quantity": -1.0, "stopPrice": 570.0}
STOP_ERROR = "The stop at Trading 212 could not be cancelled. Nothing changed. Try again."


async def _add_trigger(status="PENDING"):
    async with AsyncSessionLocal() as s:
        t = OrderTrigger(ticker=TICKER, name="ASML", currency="EUR", target_price=590.0, current_price_at_creation=600.0,
                         stop_loss=560.0, take_profit=660.0, allocation_eur=600.0, status=status, is_live=True)
        s.add(t)
        await s.commit()
        return t.id


@pytest.fixture
async def bot_position(fake, monkeypatch):
    """Live T212 mode, one bot position ASML.AS (1 share) with a broker stop and a pending trigger."""
    await init_db()
    _live(monkeypatch)
    fake.pending = [dict(STOP)]
    async with AsyncSessionLocal() as s:
        s.add(Position(ticker=TICKER, name="ASML", currency="EUR", shares=1.0, entry_price_native=600.0,
                       current_price_native=610.0, highest_price_native=610.0, entry_price_eur=600.0,
                       current_price_eur=610.0, cost_basis_eur=600.0, stop_loss_native=570.0, status="OPEN", is_live=True))
        await s.commit()
    trigger_id = await _add_trigger()
    try:
        yield trigger_id
    finally:
        async with AsyncSessionLocal() as s:
            for model in (Position, UserHolding, Trade, OrderTrigger):
                await s.execute(delete(model).where(model.ticker == TICKER))
            await s.commit()


async def _state():
    async with AsyncSessionLocal() as s:
        pos = (await s.execute(select(Position).where(Position.ticker == TICKER))).scalars().one()
        holding = (await s.execute(select(UserHolding).where(UserHolding.ticker == TICKER))).scalars().first()
        sells = (await s.execute(select(Trade).where(Trade.ticker == TICKER, Trade.order_type == "SELL"))).scalars().all()
        triggers = (await s.execute(select(OrderTrigger.status).where(OrderTrigger.ticker == TICKER))).scalars().all()
        return pos, holding, sells, triggers


async def _assert_unchanged():
    pos, holding, sells, triggers = await _state()
    assert pos.status == "OPEN" and holding is None and sells == []
    assert triggers == ["PENDING"]


async def test_cancel_price_trigger_unchanged_after_split(fake, monkeypatch):
    await init_db()
    _live(monkeypatch)
    pending_id, done_id = await _add_trigger(), await _add_trigger(status="TRIGGERED")
    try:
        res = await LiveBroker().cancel_price_trigger(pending_id)
        assert res == {"success": True, "message": f"Trigger #{pending_id} for {TICKER} cancelled."}
        async with AsyncSessionLocal() as s:
            assert (await s.get(OrderTrigger, pending_id)).status == "CANCELLED"
        res = await LiveBroker().cancel_price_trigger(done_id)
        assert res == {"success": False, "message": f"Trigger #{done_id} is already TRIGGERED."}
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == TICKER))
            await s.commit()


async def test_release_cancels_stop_and_moves_position(fake, bot_position):
    res = await LiveBroker().release_to_user("asml.as")
    assert res == {"success": True, "message": "ASML.AS is now yours."}
    assert 88 in fake.cancelled
    pos, holding, sells, triggers = await _state()
    assert pos.status == "RELEASED"
    assert holding.shares == 1.0 and holding.t212_ticker == "ASMLa_EQ"
    assert holding.avg_price_native == 600.0 and holding.current_price_eur == 610.0 and holding.cost_basis_eur == 600.0
    assert sells == []
    assert triggers == ["CANCELLED"]


async def test_release_aborts_when_stop_stays_pending(fake, bot_position):
    with patch.object(LiveBroker, "_t212_wait_gone", AsyncMock(return_value=False)):
        res = await LiveBroker().release_to_user(TICKER)
    assert res == {"success": False, "message": STOP_ERROR}
    pos, holding, _, _ = await _state()
    assert pos.status == "OPEN" and holding is None


async def test_release_aborts_when_resting_buy_cannot_be_cancelled(fake, bot_position):
    failed = AsyncMock(return_value={"success": False, "message": "still pending"})
    with patch.object(LiveBroker, "_cancel_trigger_locked", failed):
        res = await LiveBroker().release_to_user(TICKER)
    assert res == {"success": False, "message": "A pending buy for ASML.AS could not be cancelled. Nothing changed."}
    assert fake.cancelled == []
    await _assert_unchanged()


async def test_release_without_client_changes_nothing(fake, bot_position, monkeypatch):
    monkeypatch.setattr(trading212, "get_client", lambda: None)
    res = await LiveBroker().release_to_user(TICKER)
    assert res == {"success": False, "message": "ASML.AS has no unique Trading 212 instrument. Nothing changed."}
    await _assert_unchanged()


async def test_release_aborts_when_pending_orders_call_fails(fake, bot_position):
    fake.pending_orders = AsyncMock(side_effect=trading212.Trading212Error(503, "busy"))
    res = await LiveBroker().release_to_user(TICKER)
    assert res == {"success": False, "message": STOP_ERROR}
    assert fake.cancelled == []
    pos, holding, _, _ = await _state()
    assert pos.status == "OPEN" and holding is None


async def test_release_without_bot_position(fake, monkeypatch):
    _live(monkeypatch)
    res = await LiveBroker().release_to_user("NOPOS.AS")
    assert res == {"success": False, "message": "No bot position in NOPOS.AS."}


async def test_release_waits_for_sync_lock(fake, bot_position):
    broker = LiveBroker()
    await broker._t212_lock.acquire()
    task = asyncio.create_task(broker.release_to_user(TICKER))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        assert fake.cancelled == []
    finally:
        broker._t212_lock.release()
    res = await task
    assert res["success"] is True


async def test_release_refused_in_paper_mode():
    res = await LiveBroker().release_to_user(TICKER)
    assert res == {"success": False, "message": "Only for live Trading 212."}
