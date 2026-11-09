"""Resting Trading 212 orders: stop-limit client call, feature flag, booking, and the resting entry sync."""
import asyncio
import datetime
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, desc, select

from config.settings import settings
from src.db.models import OrderTrigger, PortfolioSnapshot, Position, Trade
from src.db.session import AsyncSessionLocal
from src.trading import live as live_module
from src.trading import trading212
from src.trading.fees import calculate_broker_fee
from src.trading.live import LiveBroker
from tests.test_trading212_broker import FakeT212, _cleanup, _held, _live, clean_snapshots  # noqa: F401


async def test_place_stop_limit_posts_expected_body():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": 5, "status": "NEW"})
    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    await client.place_stop_limit("AAPL_US_EQ", 0.5, 100.0, 100.5)
    path, body = seen[0]
    assert path.endswith("/equity/orders/stop_limit")
    assert body == {"ticker": "AAPL_US_EQ", "quantity": 0.5, "stopPrice": 100.0,
                    "limitPrice": 100.5, "timeValidity": "DAY"}


def test_resting_orders_flag(monkeypatch):
    assert LiveBroker.resting_orders_on() is False
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", True)
    monkeypatch.setattr(settings, "LIVE_BROKER", "alpaca")
    assert LiveBroker.resting_orders_on() is False
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    assert LiveBroker.resting_orders_on() is True


async def test_book_live_fill_records_position_trade_and_snapshot(monkeypatch, clean_snapshots):
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    async with AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=1000.0, equity_eur=0.0, total_value_eur=1000.0, is_live=True))
        await s.commit()
    try:
        res = await LiveBroker()._book_live_fill(
            ticker="ASML.AS", name="ASML", currency="EUR", shares=0.5, price_native=600.0,
            stop_loss_native=570.0, take_profit_native=660.0, rationale="r", consensus_score=85,
            broker_order_id="t212_9")
        fee = calculate_broker_fee(ticker="ASML.AS", shares=0.5, price_native=600.0, currency="EUR",
                                   broker="trading212", order_action="BUY")["fee_eur"]
        assert res["success"] and res["shares"] == 0.5 and res["broker_order_id"] == "t212_9"
        async with AsyncSessionLocal() as s:
            pos = (await s.execute(select(Position).where(Position.ticker == "ASML.AS"))).scalars().all()
            trades = (await s.execute(select(Trade).where(Trade.ticker == "ASML.AS"))).scalars().all()
            snap = (await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
                                    .order_by(desc(PortfolioSnapshot.timestamp)))).scalars().first()
        assert len(pos) == 1 and pos[0].status == "OPEN" and pos[0].is_live
        assert (pos[0].shares, pos[0].stop_loss_native, pos[0].take_profit_native) == (0.5, 570.0, 660.0)
        assert len(trades) == 1 and trades[0].order_type == "BUY" and trades[0].broker_order_id == "t212_9"
        assert snap.cash_eur == round(1000.0 - 300.0 - fee, 2)
    finally:
        await _cleanup("ASML.AS")


# --- Resting entry sync (Task 3) ---

class RestingFake(FakeT212):
    """FakeT212 plus the stop-limit call and a settable account cash split."""

    def __init__(self):
        super().__init__()
        self.cash = {"availableToTrade": 400.0, "reservedForOrders": 100.0, "inPies": 0}

    async def account_summary(self):
        return {"id": 123, "currency": "EUR", "totalValue": 1500.0, "cash": dict(self.cash),
                "investments": {"currentValue": 1000.0}}

    async def place_stop_limit(self, ticker, quantity, stop_price, limit_price, validity="DAY"):
        self.posts.append(("stop_limit", ticker, quantity, (stop_price, limit_price)))
        order = dict(self.limit_answer, ticker=ticker, quantity=quantity)
        self.order_state[order["id"]] = order
        return order


@pytest.fixture
def rest(monkeypatch, clean_snapshots):
    client = RestingFake()
    monkeypatch.setattr(trading212, "get_client", lambda: client)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", True)
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", False)  # Trading 212 default: fractional shares
    monkeypatch.setattr(live_module, "FILL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(LiveBroker, "enforce_daily_loss_limit", AsyncMock(return_value=None))
    _live(monkeypatch)
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay=0, *args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return client


async def _seed(cash=1000.0, order_id=None, created_days_ago=0, **kw):
    fields = dict(ticker="ASML.AS", name="ASML", currency="EUR", trigger_type="LIMIT_PULLBACK", target_price=600.0,
                  current_price_at_creation=610.0, stop_loss=570.0, take_profit=700.0, allocation_eur=300.0,
                  consensus_score=85, status="PENDING", is_live=True, broker_order_id=order_id,
                  created_at=datetime.datetime.utcnow() - datetime.timedelta(days=created_days_ago))
    fields.update(kw)
    async with AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=cash, equity_eur=0.0, total_value_eur=cash, is_live=True))
        t = OrderTrigger(**fields)
        s.add(t)
        await s.commit()
        return t.id


async def _trigger(tid):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(OrderTrigger).where(OrderTrigger.id == tid))).scalars().first()


async def _positions():
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(Position).where(Position.ticker == "ASML.AS", Position.status == "OPEN"))).scalars().all()


@pytest.fixture
async def asml():
    yield
    await _cleanup("ASML.AS")
    async with AsyncSessionLocal() as s:
        await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "ASML.AS"))
        await s.commit()


def _quote(price):
    return patch.object(live_module.market_data_provider, "get_stock_data", return_value={"price": price})


async def _sync(price=610.0, broker=None):
    with _quote(price), patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()):
        return await (broker or LiveBroker()).sync_resting_entries()


async def test_pullback_trigger_places_day_limit_at_target(rest, asml):
    tid = await _seed()
    await _sync()
    assert len(rest.posts) == 1 and rest.posts[0][:2] == ("limit", "ASMLa_EQ") and rest.posts[0][3] == 600.0
    assert rest.posts[0][2] > 0
    assert (await _trigger(tid)).broker_order_id == "t212_1"
    assert not await _positions()


async def test_breakout_trigger_places_stop_limit_with_buffer(rest, asml):
    tid = await _seed(trigger_type="BREAKOUT_BUY", target_price=100.0, current_price_at_creation=98.0,
                      stop_loss=95.0, take_profit=120.0)
    await _sync(price=99.0)
    assert len(rest.posts) == 1 and rest.posts[0][:2] == ("stop_limit", "ASMLa_EQ")
    assert rest.posts[0][3] == (100.0, 100.5)
    assert (await _trigger(tid)).broker_order_id == "t212_1"


async def test_no_placement_in_first_20_minutes(rest, asml, monkeypatch):
    opened = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
    monkeypatch.setattr(live_module, "check_market_hours", lambda t, now=None: (now is None or now >= opened, ""))
    tid = await _seed()
    await _sync()
    assert rest.posts == [] and (await _trigger(tid)).broker_order_id is None


async def test_filled_order_is_booked_with_stop(rest, asml):
    tid = await _seed(order_id="t212_1")
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    booked = await _sync()
    assert len(booked) == 1 and booked[0]["trigger_id"] == tid
    pos = await _positions()
    assert len(pos) == 1 and (pos[0].shares, pos[0].entry_price_native, pos[0].stop_loss_native) == (0.5, 598.0, 570.0)
    assert pos[0].trigger_id == tid
    async with AsyncSessionLocal() as s:
        trades = (await s.execute(select(Trade).where(Trade.ticker == "ASML.AS", Trade.order_type == "BUY"))).scalars().all()
    assert len(trades) == 1 and trades[0].broker_order_id == "t212_1"
    t = await _trigger(tid)
    assert t.status == "TRIGGERED" and t.triggered_at is not None
    assert ("stop", "ASMLa_EQ", -0.5, 570.0) in rest.posts
    assert rest.cancelled == []


async def test_execute_buy_links_position_to_its_trigger(rest, asml, monkeypatch):
    monkeypatch.setattr(LiveBroker, "_plan_live_buy", AsyncMock(return_value={"success": True, "shares": 0.5}))
    monkeypatch.setattr(LiveBroker, "_dispatch_real_order", AsyncMock(return_value=("t212_7", 0.5, 600.0)))
    monkeypatch.setattr(LiveBroker, "adjust_triggers_dynamically", AsyncMock())
    async with AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=1000.0, equity_eur=0.0, total_value_eur=1000.0, is_live=True))
        await s.commit()
    res = await LiveBroker().execute_buy("ASML.AS", "ASML", 600.0, "EUR", 570.0, 700.0, 300.0, 85, "test", trigger_id=42)
    assert res["success"]
    pos = await _positions()
    assert len(pos) == 1 and pos[0].trigger_id == 42


async def test_partial_fill_cancels_rest_and_books(rest, asml):
    tid = await _seed(order_id="t212_1")
    rest.order_state[1] = {"id": 1, "status": "NEW", "ticker": "ASMLa_EQ", "quantity": 0.5, "filledQuantity": 0.2}
    rest.history = [{"order": {"id": 1, "status": "CANCELLED"}, "fill": {"quantity": 0.2, "price": 600.0}}]
    await _sync()
    assert rest.cancelled == [1]
    pos = await _positions()
    assert len(pos) == 1 and pos[0].shares == 0.2
    assert (await _trigger(tid)).status == "TRIGGERED"


def _resting(rest, qty=0.5):
    rest.order_state[1] = {"id": 1, "status": "NEW", "ticker": "ASMLa_EQ", "quantity": qty, "limitPrice": 600.0, "filledQuantity": 0}
    rest.history = [{"order": {"id": 1, "status": "CANCELLED"}}]


async def test_expired_trigger_cancels_order(rest, asml):
    tid = await _seed(order_id="t212_1", created_days_ago=60)
    _resting(rest)
    await _sync()
    t = await _trigger(tid)
    assert rest.cancelled == [1] and t.status == "EXPIRED" and t.broker_order_id is None
    assert not await _positions()


async def test_price_through_stop_cancels_order(rest, asml):
    tid = await _seed(order_id="t212_1")
    _resting(rest)
    await _sync(price=560.0)
    t = await _trigger(tid)
    assert rest.cancelled == [1] and t.status == "CANCELLED" and t.broker_order_id is None


async def test_kill_switch_cancels_but_keeps_trigger_pending(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    tid = await _seed(order_id="t212_1")
    _resting(rest)
    await _sync()
    t = await _trigger(tid)
    assert rest.cancelled == [1] and t.status == "PENDING" and t.broker_order_id is None
    assert rest.posts == []


async def test_sync_takes_over_existing_pending_buy(rest, asml):
    tid = await _seed()
    rest.pending = [{"id": 5, "ticker": "ASMLa_EQ", "quantity": 0.5, "status": "NEW", "limitPrice": 600.0}]
    rest.order_state[5] = dict(rest.pending[0])
    await _sync()
    assert rest.posts == [] and (await _trigger(tid)).broker_order_id == "t212_5"


async def test_sync_leaves_user_buy_at_other_price_alone(rest, asml):
    tid = await _seed()
    rest.pending = [{"id": 5, "ticker": "ASMLa_EQ", "quantity": 0.5, "status": "NEW", "limitPrice": 590.0}]
    rest.order_state[5] = dict(rest.pending[0])
    await _sync()
    assert len(rest.posts) == 1 and rest.posts[0][0] == "limit"
    assert (await _trigger(tid)).broker_order_id == "t212_1"


def _breakout():
    return _seed(trigger_type="BREAKOUT_BUY", target_price=100.0, current_price_at_creation=98.0,
                 stop_loss=95.0, take_profit=120.0)


async def test_breakout_takes_over_stop_limit_at_trigger_price(rest, asml):
    tid = await _breakout()
    rest.pending = [{"id": 6, "ticker": "ASMLa_EQ", "quantity": 0.5, "status": "NEW", "stopPrice": 100.0, "limitPrice": 100.5}]
    await _sync(price=99.0)
    assert rest.posts == [] and (await _trigger(tid)).broker_order_id == "t212_6"


async def test_breakout_ignores_limit_buy_at_trigger_price(rest, asml):
    tid = await _breakout()
    rest.pending = [{"id": 5, "ticker": "ASMLa_EQ", "quantity": 0.5, "status": "NEW", "limitPrice": 100.0}]
    await _sync(price=99.0)
    assert len(rest.posts) == 1 and rest.posts[0][0] == "stop_limit"
    assert (await _trigger(tid)).broker_order_id == "t212_1"


async def test_sync_does_not_book_a_fill_twice(rest, asml):
    tid = await _seed(order_id="t212_1")
    async with AsyncSessionLocal() as s:
        s.add(Trade(ticker="ASML.AS", name="ASML", order_type="BUY", shares=0.5, currency="EUR", price_native=598.0,
                    price_eur=598.0, total_eur=299.0, is_live=True, broker_order_id="t212_1"))
        await s.commit()
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    assert await _sync() == []
    assert not await _positions()
    assert not any(p[0] == "stop" for p in rest.posts)
    async with AsyncSessionLocal() as s:
        trades = (await s.execute(select(Trade).where(Trade.ticker == "ASML.AS", Trade.order_type == "BUY"))).scalars().all()
    assert len(trades) == 1
    assert (await _trigger(tid)).status == "TRIGGERED"


async def test_placement_timeout_takes_over_the_order_it_created(rest, asml):
    tid = await _seed()
    place = rest.place_limit

    async def place_then_timeout(ticker, quantity, limit_price, validity="DAY"):
        order = await place(ticker, quantity, limit_price, validity)
        rest.pending.append(dict(order, limitPrice=limit_price, createdAt=datetime.datetime.now(
            datetime.timezone.utc).isoformat().replace("+00:00", "Z")))
        raise httpx.ReadTimeout("timed out")
    rest.place_limit = place_then_timeout
    await _sync()
    assert len(rest.posts) == 1
    assert (await _trigger(tid)).broker_order_id == "t212_1"


async def test_sync_cancel_reuses_the_order_it_read(rest, asml):
    await _seed(order_id="t212_1", created_days_ago=60)
    _resting(rest)
    calls = []
    get_order, cancel_order = rest.get_order, rest.cancel_order

    async def counting_get(oid):
        calls.append("get")
        return await get_order(oid)

    async def counting_cancel(oid):
        calls.append("cancel")
        return await cancel_order(oid)
    rest.get_order, rest.cancel_order = counting_get, counting_cancel
    await _sync()
    assert calls.index("cancel") == 1  # one read, then the cancel


async def test_resting_order_cash_does_not_defund_its_trigger(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSITIONS", 1)
    rest.cash = {"availableToTrade": 0.0, "reservedForOrders": 300.0, "inPies": 0}
    tid = await _seed(cash=300.0, order_id="t212_1")
    _resting(rest)
    broker = LiveBroker()
    await broker.sync_cash_balance()
    await _sync(broker=broker)
    await _sync(broker=broker)
    t = await _trigger(tid)
    assert rest.cancelled == [] and rest.posts == []
    assert t.status == "PENDING" and t.broker_order_id == "t212_1" and t.allocation_eur > 0


async def test_account_sync_skips_ticker_with_resting_entry(rest, asml):
    await _seed(order_id="t212_1")
    rest.positions_answers = [[_held("ASMLa_EQ", 0.2, avg=600.0)]]
    with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()):
        await LiveBroker()._sync_t212_locked()
    assert not await _positions()


async def test_flag_off_keeps_bot_trigger_path(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", False)
    tid = await _seed()
    assert await _sync() == []
    assert rest.posts == [] and (await _trigger(tid)).broker_order_id is None


# --- Cancel resting buys at once (Task 4) ---

async def _drain_kill_switch_tasks():
    from src.db import session as db_session
    await asyncio.gather(*list(db_session._cancel_tasks))


async def test_kill_switch_on_cancels_resting_buys(rest, asml, monkeypatch):
    from src.db.session import set_kill_switch
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    a = await _seed(order_id="t212_1")
    b = await _seed(order_id="t212_2")
    rest.order_state[1] = {"id": 1, "status": "NEW", "ticker": "ASMLa_EQ", "quantity": 0.5, "filledQuantity": 0}
    rest.order_state[2] = {"id": 2, "status": "NEW", "ticker": "ASMLa_EQ", "quantity": 0.5, "filledQuantity": 0}
    rest.history = [{"order": {"id": 1, "status": "CANCELLED"}}, {"order": {"id": 2, "status": "CANCELLED"}}]
    try:
        await set_kill_switch(True)
        await _drain_kill_switch_tasks()
    finally:
        await set_kill_switch(False)
    assert sorted(rest.cancelled) == [1, 2]
    for tid in (a, b):
        t = await _trigger(tid)
        assert t.status == "PENDING" and t.broker_order_id is None


async def test_kill_switch_does_not_deadlock_on_t212_lock(rest, monkeypatch):
    from src.db.session import set_kill_switch
    from src.trading.broker import live_broker as broker
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    try:
        async with broker._t212_lock:
            await asyncio.wait_for(set_kill_switch(True), 1)
        await _drain_kill_switch_tasks()
    finally:
        await set_kill_switch(False)


async def test_cancel_trigger_cancels_broker_order(rest, asml):
    tid = await _seed(order_id="t212_1")
    _resting(rest)
    res = await LiveBroker().cancel_price_trigger(tid)
    assert res["success"] is True and rest.cancelled == [1]
    assert (await _trigger(tid)).status == "CANCELLED"


async def test_cancel_during_sync_placement_cancels_the_new_order(rest, asml):
    tid = await _seed()
    rest.history = [{"order": {"id": 1, "status": "CANCELLED"}}]
    entered, release = asyncio.Event(), asyncio.Event()
    place = rest.place_limit

    async def slow_place(*a, **kw):
        entered.set()
        await release.wait()
        return await place(*a, **kw)
    rest.place_limit = slow_place
    broker = LiveBroker()
    with _quote(610.0), patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()):
        sync = asyncio.create_task(broker.sync_resting_entries())
        await entered.wait()
        cancel = asyncio.create_task(broker.cancel_price_trigger(tid))
        await asyncio.wait({cancel}, timeout=0.5)  # old code: the cancel finishes here, before the order exists
        release.set()
        await sync
        res = await cancel
    t = await _trigger(tid)
    assert res["success"] is True and t.status == "CANCELLED"
    assert rest.cancelled == [1] and t.broker_order_id is None


async def test_cancel_resting_entries_with_flag_off_books_and_alerts_fills(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", False)
    a = await _seed(order_id="t212_1")
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    broker = LiveBroker()
    with patch("src.telegram.bot.telegram_bot.notify_trigger_executed", new=AsyncMock()) as notify, \
         patch.object(broker, "adjust_triggers_dynamically", new=AsyncMock(return_value=[])) as adjust:
        booked = await broker.cancel_resting_entries("test")
    assert len(booked) == 1 and booked[0]["trigger_id"] == a
    notify.assert_awaited_once_with(booked[0])
    adjust.assert_awaited_once()
    assert (await _trigger(a)).status == "TRIGGERED" and len(await _positions()) == 1


async def test_flag_off_trigger_path_cancels_leftover_resting_order_first(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", False)
    monkeypatch.setattr(live_module, "check_market_hours", lambda t, now=None: (True, ""))
    tid = await _seed(order_id="t212_1")
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    with _quote(599.0):
        executed = await LiveBroker().check_and_execute_triggers()
    assert len(executed) == 1 and executed[0]["trigger_id"] == tid
    assert (await _trigger(tid)).status == "TRIGGERED"
    assert len(await _positions()) == 1
    assert not any(p[0] in ("limit", "market") for p in rest.posts)


async def test_settings_disarm_cancels_resting_orders(rest, asml, monkeypatch):
    from src.db.models import SystemConfig
    from src.web import routes
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(routes, "update_env_file", lambda updates: None)
    tid = await _seed(order_id="t212_1")
    _resting(rest)
    try:
        await routes.update_settings(routes.SettingsUpdateRequest(live_trading_enabled=False))
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(SystemConfig).where(SystemConfig.key == "LIVE_TRADING_ENABLED"))
            await s.commit()
    assert rest.cancelled == [1] and (await _trigger(tid)).broker_order_id is None


async def test_startup_cancels_resting_orders_when_not_synced():
    from src.scheduler.jobs import TradingScheduler, live_broker
    sched = TradingScheduler()
    with patch.object(live_broker, "cancel_resting_entries", new=AsyncMock(return_value=[])) as cancel, \
         patch.object(sched, "monitor_resting_orders", new=AsyncMock(side_effect=RuntimeError("stop here"))):
        await sched.check_and_run_startup_sessions()
    cancel.assert_awaited_once()


async def test_cancel_trigger_with_fill_books_position(rest, asml):
    tid = await _seed(order_id="t212_1")
    rest.order_state[1] = {"id": 1, "status": "NEW", "ticker": "ASMLa_EQ", "quantity": 0.5, "filledQuantity": 0.5}
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    res = await LiveBroker().cancel_price_trigger(tid)
    assert res["success"] is False and "filled" in res["message"]
    assert (await _trigger(tid)).status == "TRIGGERED"
    assert len(await _positions()) == 1


# --- Take-profit on live T212 price (Task 5) ---

async def _tp_position(entry=600.0, stop=570.0, tp=660.0, shares=0.5):
    async with AsyncSessionLocal() as s:
        # highest above every test price, so the trailing stop never moves
        s.add(Position(ticker="ASML.AS", name="ASML", currency="EUR", shares=shares, entry_price_native=entry,
                       current_price_native=entry, highest_price_native=1000.0, entry_price_eur=entry,
                       current_price_eur=entry, cost_basis_eur=shares * entry, stop_loss_native=stop,
                       take_profit_native=tp, status="OPEN", is_live=True))
        await s.commit()


def _t212_held(rest, price, qty=0.5, stop=570.0):
    rest.positions_answers = [[dict(_held("ASMLa_EQ", qty, avg=600.0), currentPrice=price)]]
    rest.pending = [{"id": 20, "ticker": "ASMLa_EQ", "type": "STOP", "side": "SELL", "quantity": -qty, "stopPrice": stop}]


async def _check(yahoo):
    with _quote(yahoo), patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()), \
         patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()):
        return await LiveBroker().check_stops_and_targets()


async def test_refresh_uses_t212_current_price(rest, asml):
    await _tp_position(entry=100.0, stop=90.0, tp=200.0)
    _t212_held(rest, 110.0, stop=90.0)
    with _quote(100.0):
        await LiveBroker().refresh_positions_pnl()
    assert (await _positions())[0].current_price_native == 110.0


async def test_take_profit_sends_limit_sell_at_target(rest, asml):
    await _tp_position()
    _t212_held(rest, 665.0)
    rest.fill_on_poll = (0.5, 660.0)
    await _check(650.0)
    assert ("limit", "ASMLa_EQ", -0.5, 660.0) in rest.posts
    assert not any(p[0] == "market" for p in rest.posts)
    assert not await _positions()


async def test_tp_limit_sell_unfilled_restores_stop(rest, asml):
    await _tp_position()
    _t212_held(rest, 665.0)
    rest.history = [{"order": {"id": 1, "status": "CANCELLED"}}]
    await _check(650.0)
    assert ("limit", "ASMLa_EQ", -0.5, 660.0) in rest.posts
    assert 1 in rest.cancelled
    assert ("stop", "ASMLa_EQ", -0.5, 570.0) in rest.posts
    assert len(await _positions()) == 1


async def test_stop_loss_branch_still_market(rest, asml):
    await _tp_position()
    _t212_held(rest, 560.0)
    await _check(650.0)
    assert any(p[0] == "market" for p in rest.posts)
    assert not any(p[0] == "limit" for p in rest.posts)


def _fake_broker(on, fills=()):
    from types import SimpleNamespace
    return SimpleNamespace(
        resting_orders_on=lambda: on,
        sync_cash_balance=AsyncMock(),
        sync_resting_entries=AsyncMock(return_value=list(fills)),
    )


async def test_monitor_resting_orders_noop_when_flag_off():
    from src.scheduler.jobs import TradingScheduler
    broker = _fake_broker(False)
    sched = TradingScheduler()
    with patch("src.scheduler.jobs.get_active_broker", return_value=broker):
        assert await sched.monitor_resting_orders() == []
    broker.sync_resting_entries.assert_not_awaited()


async def test_monitor_resting_orders_books_and_notifies():
    from src.scheduler.jobs import TradingScheduler
    fill = {"ticker": "ASML"}
    broker = _fake_broker(True, [fill])
    sched = TradingScheduler()
    with patch("src.scheduler.jobs.get_active_broker", return_value=broker), \
         patch("src.scheduler.jobs.telegram_bot.notify_trigger_executed", new=AsyncMock()) as notify, \
         patch.object(sched, "_check_stops_and_targets", new=AsyncMock(return_value=[])) as stops:
        assert await sched.monitor_resting_orders() == [fill]
    broker.sync_cash_balance.assert_awaited_once()
    notify.assert_awaited_once_with(fill)
    stops.assert_awaited_once()


async def test_monitor_resting_orders_swallows_errors():
    from src.scheduler.jobs import TradingScheduler
    broker = _fake_broker(True)
    broker.sync_resting_entries.side_effect = RuntimeError("boom")
    with patch("src.scheduler.jobs.get_active_broker", return_value=broker):
        assert await TradingScheduler().monitor_resting_orders() == []


def test_scheduler_registers_t212_order_sync():
    from src.scheduler.jobs import TradingScheduler
    sched = TradingScheduler()
    with patch.object(sched.scheduler, "add_job") as add_job, patch.object(sched.scheduler, "start"):
        sched.start()
    kwargs = next(c.kwargs for c in add_job.call_args_list if c.kwargs.get("id") == "t212_order_sync")
    assert kwargs["max_instances"] == 1 and kwargs["coalesce"] is True
