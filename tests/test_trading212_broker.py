"""Trading 212 live broker: ticker mapping, order flow (fill / partial / unfilled / rejected / timeout), stops, exits and sync."""
import asyncio
import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import OrderTrigger, PortfolioSnapshot, Position, Trade
from src.db.session import AsyncSessionLocal, init_db
from src.trading import live as live_module
from src.trading import trading212
from src.trading.live import LiveBroker

INSTRUMENTS = [
    {"ticker": "ASMLa_EQ", "shortName": "ASML", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Amsterdam"},
    {"ticker": "ASMLd_EQ", "shortName": "ASML", "currencyCode": "EUR", "type": "STOCK", "exchange": "Deutsche Börse Xetra"},
    {"ticker": "ASML_US_EQ", "shortName": "ASML", "currencyCode": "USD", "type": "STOCK", "exchange": "NASDAQ"},
    {"ticker": "SAPd_EQ", "shortName": "SAP", "currencyCode": "EUR", "type": "STOCK", "exchange": "Deutsche Börse Xetra"},
    {"ticker": "MCp_EQ", "shortName": "MC", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Paris"},
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "currencyCode": "USD", "type": "STOCK", "exchange": "NASDAQ"},
    {"ticker": "SHELl_EQ", "shortName": "SHEL", "currencyCode": "GBX", "type": "STOCK", "exchange": "London Stock Exchange"},
    {"ticker": "FOOa_EQ", "shortName": "FOO", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Paris"},
    {"ticker": "FOOb_EQ", "shortName": "FOO", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Paris"},
    {"ticker": "T212UNF_EQ", "shortName": "T212UNF", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Amsterdam"},
]


class FakeT212(trading212.Trading212Client):
    """Real mapping logic, scripted broker answers, every write recorded."""

    def __init__(self):
        super().__init__("key", "secret")
        self.posts = []          # (kind, ticker, quantity, price)
        self.cancelled = []
        self.pending = []        # what GET /equity/orders returns
        self.history = []        # GET /equity/history/orders items
        self.positions_answers = [[]]  # successive GET /equity/positions answers; the last one repeats
        self.order_state = {}    # id -> pending order dict, removed once no longer pending
        self.limit_answer = {"id": 1, "status": "NEW"}
        self.limit_error = None
        self.fill_on_poll = None  # (qty, price): fill the order on the first poll
        self.vanish = False       # the order is gone (404) right after the POST, with no history yet
        self.market_fill = True   # market orders fill at once; False = queued (market closed)
        self.market_error = None
        self.stop_errors = 0      # this many stop POSTs fail before one succeeds
        self.positions_calls = 0

    async def instruments(self):
        return INSTRUMENTS

    async def account_summary(self):
        return {"id": 123, "currency": "EUR", "totalValue": 1500.0,
                "cash": {"availableToTrade": 400.0, "reservedForOrders": 100.0, "inPies": 0},
                "investments": {"currentValue": 1000.0}}

    async def place_limit(self, ticker, quantity, limit_price, validity="DAY"):
        self.posts.append(("limit", ticker, quantity, limit_price))
        if self.limit_error:
            raise self.limit_error
        order = dict(self.limit_answer, ticker=ticker, quantity=quantity)
        if order["status"] not in ("FILLED", "REJECTED") and not self.vanish:
            self.order_state[order["id"]] = order
        return order

    async def place_market(self, ticker, quantity):
        self.posts.append(("market", ticker, quantity, None))
        if self.market_error:
            raise self.market_error
        if self.market_fill:
            self.history.append({"order": {"id": 77, "status": "FILLED"}, "fill": {"quantity": quantity, "price": 101.0}})
            return {"id": 77, "status": "FILLED"}
        self.order_state[77] = {"id": 77, "status": "NEW"}
        return {"id": 77, "status": "NEW"}

    async def place_stop(self, ticker, quantity, stop_price, validity="GOOD_TILL_CANCEL"):
        self.posts.append(("stop", ticker, quantity, stop_price))
        if self.stop_errors:
            self.stop_errors -= 1
            raise trading212.Trading212Error(400, "Failed validation")
        return {"id": 88, "status": "NEW"}

    async def get_order(self, order_id):
        if self.fill_on_poll and order_id in self.order_state:
            qty, price = self.fill_on_poll
            self.order_state.pop(order_id)
            self.history.append({"order": {"id": order_id, "status": "FILLED"}, "fill": {"quantity": qty, "price": price}})
        return self.order_state.get(order_id)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.order_state.pop(order_id, None)
        self.pending = [o for o in self.pending if o["id"] != order_id]

    async def pending_orders(self):
        return list(self.pending)

    async def history_orders(self, ticker, limit=50):
        return list(self.history)

    async def positions(self, ticker=None):
        answer = self.positions_answers[min(self.positions_calls, len(self.positions_answers) - 1)]
        self.positions_calls += 1
        return [p for p in answer if ticker is None or p["instrument"]["ticker"] == ticker]


def _held(t212_ticker, qty, avg=100.0, currency="EUR", free=None):
    return {"instrument": {"ticker": t212_ticker, "name": t212_ticker, "currency": currency},
            "quantity": qty, "quantityAvailableForTrading": qty if free is None else free, "averagePricePaid": avg}


@pytest.fixture
def fake(monkeypatch):
    client = FakeT212()
    monkeypatch.setattr(trading212, "get_client", lambda: client)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(live_module, "FILL_WAIT_SECONDS", 0.05)
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay=0, *args, **kwargs):
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return client


@pytest.fixture
async def clean_snapshots():
    """Snapshots written here would read as a daily loss in later live tests: remove them afterwards."""
    await init_db()
    async with AsyncSessionLocal() as s:
        start = (await s.execute(select(PortfolioSnapshot.id).order_by(PortfolioSnapshot.id.desc()))).scalars().first() or 0
    yield
    async with AsyncSessionLocal() as s:
        await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id > start))
        await s.commit()


async def test_ticker_mapping(fake):
    assert await fake.resolve_ticker("ASML.AS", "EUR") == "ASMLa_EQ"
    assert await fake.resolve_ticker("ASML.DE", "EUR") == "ASMLd_EQ"
    assert await fake.resolve_ticker("ASML", "USD") == "ASML_US_EQ"
    assert await fake.resolve_ticker("SAP.DE", "EUR") == "SAPd_EQ"
    assert await fake.resolve_ticker("SHEL.L", "GBp") == "SHELl_EQ"  # Yahoo pence code vs T212 GBX
    assert await fake.resolve_ticker("AAPL", "USD") == "AAPL_US_EQ"
    assert await fake.resolve_ticker("FOO.PA", "EUR") is None        # two Paris listings: ambiguous
    assert await fake.resolve_ticker("ASML.MI", "EUR") is None       # two EUR listings, none in Milan
    assert await fake.resolve_ticker("XYZ", "USD") is None
    assert await fake.yahoo_ticker("ASMLa_EQ") == "ASML.AS"
    assert await fake.yahoo_ticker("SHELl_EQ") == "SHEL.L"
    assert await fake.yahoo_ticker("AAPL_US_EQ") == "AAPL"
    assert await fake.yahoo_ticker("UNKNOWN_EQ") == "UNKNOWN_EQ"


async def test_buy_fills_fully_and_places_stop(fake):
    fake.fill_on_poll = (1.5, 100.2)
    res = await LiveBroker()._dispatch_real_order("ASML.AS", 1.5, 100.237, "EUR", 95.0, 120.0)
    assert res == ("t212_1", 1.5, 100.2)
    assert fake.posts == [("limit", "ASMLa_EQ", 1.5, 100.24), ("stop", "ASMLa_EQ", -1.5, 95.0)]  # no take-profit order
    assert fake.cancelled == []


async def test_buy_partial_fill_cancels_rest_and_books_filled(fake):
    fake.history = [{"order": {"id": 1, "status": "CANCELLED"}, "fill": {"quantity": 0.5, "price": 99.0}}]
    res = await LiveBroker()._dispatch_real_order("SAP.DE", 2.0, 100.0, "EUR", None, None)
    assert res == ("t212_1", 0.5, 99.0)
    assert fake.cancelled == [1]
    assert fake.posts[-1] == ("stop", "SAPd_EQ", -0.5, 95.0)  # default stop 5% below


async def test_buy_unfilled_books_nothing(fake, monkeypatch, clean_snapshots):
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", False)
    monkeypatch.setattr(settings, "ENFORCE_MARKET_HOURS", False)
    async with AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=5000.0, equity_eur=0.0, total_value_eur=5000.0, is_live=True, open_positions_count=0))
        await s.commit()
    fake.history = [{"order": {"id": 1, "status": "CANCELLED"}, "fill": None}]
    broker = LiveBroker()
    with patch.object(broker, "enforce_daily_loss_limit", AsyncMock(return_value=None)):
        res = await broker.execute_buy("T212UNF.AS", "Unfilled NV", 50.0, "EUR", 47.0, 60.0, 500.0, 90, "test", is_manual=True)
    assert res["success"] is False and res.get("unfilled") is True, res
    assert fake.cancelled == [1]
    assert not [p for p in fake.posts if p[0] == "stop"]
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(Position).where(Position.ticker == "T212UNF.AS"))).scalars().first() is None


async def test_buy_rejected_returns_none(fake):
    fake.limit_answer = {"id": 5, "status": "REJECTED"}
    assert await LiveBroker()._dispatch_real_order("AAPL", 1.0, 200.0, "USD", 190.0, 220.0) is None
    assert [p[0] for p in fake.posts] == ["limit"]


async def test_buy_ambiguous_ticker_refused(fake):
    assert await LiveBroker()._dispatch_real_order("FOO.PA", 1.0, 10.0, "EUR", 9.0, 12.0) is None
    assert fake.posts == []


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timed out"), trading212.Trading212Error(503, "busy"), trading212.Trading212Error(408, "Timed-out")])
async def test_post_timeout_follows_existing_order_without_second_post(fake, error):
    fake.limit_error = error
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    fake.pending = [{"id": 9, "ticker": "AAPL_US_EQ", "quantity": 1.25, "status": "NEW", "createdAt": now}]
    fake.order_state[9] = fake.pending[0]
    fake.fill_on_poll = (1.25, 201.0)
    res = await LiveBroker()._dispatch_real_order("AAPL", 1.25, 200.0, "USD", 190.0, 220.0)
    assert res == ("t212_9", 1.25, 201.0)
    assert [p[0] for p in fake.posts] == ["limit", "stop"]  # exactly one BUY POST


async def test_post_timeout_without_order_returns_none(fake):
    fake.limit_error = httpx.ConnectTimeout("timed out")
    assert await LiveBroker()._dispatch_real_order("AAPL", 1.0, 200.0, "USD", 190.0, 220.0) is None
    assert [p[0] for p in fake.posts] == ["limit"]


def _with_stop(fake):
    fake.pending = [
        {"id": 20, "ticker": "ASMLa_EQ", "type": "STOP", "side": "SELL", "quantity": -3.0, "stopPrice": 90.0},
        {"id": 21, "ticker": "SAPd_EQ", "type": "STOP", "side": "SELL", "quantity": -1.0, "stopPrice": 90.0},
    ]


async def test_close_cancels_sells_then_sells_and_waits_for_fill(fake):
    _with_stop(fake)
    fake.positions_answers = [[_held("ASMLa_EQ", 5.0)]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") == "t212_exit_77"
    assert fake.cancelled == [20]
    assert fake.posts == [("market", "ASMLa_EQ", -3.0, None)]  # local shares, not the bigger broker holding


async def test_close_short_of_free_shares_refuses_and_restores_stop(fake):
    _with_stop(fake)
    fake.positions_answers = [[_held("ASMLa_EQ", 2.0)]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") is None
    assert fake.posts == [("stop", "ASMLa_EQ", -2.0, 90.0)]


async def test_close_queued_sell_is_cancelled_and_stop_restored(fake):
    _with_stop(fake)
    fake.market_fill = False  # market closed: T212 queues the order
    fake.positions_answers = [[_held("ASMLa_EQ", 3.0)]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") is None
    assert fake.cancelled == [20, 77]
    assert fake.posts == [("market", "ASMLa_EQ", -3.0, None), ("stop", "ASMLa_EQ", -3.0, 90.0)]


async def test_close_post_rejected_restores_stop(fake):
    _with_stop(fake)
    fake.market_error = trading212.Trading212Error(400, "Failed validation")
    fake.positions_answers = [[_held("ASMLa_EQ", 3.0)]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") is None
    assert fake.posts[-1] == ("stop", "ASMLa_EQ", -3.0, 90.0)


async def test_close_post_timeout_follows_filled_order_without_second_post(fake):
    _with_stop(fake)
    fake.market_error = httpx.ReadTimeout("timed out")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    fake.history = [{"order": {"id": 78, "ticker": "ASMLa_EQ", "quantity": -3.0, "status": "FILLED", "createdAt": now},
                     "fill": {"quantity": -3.0, "price": 101.0}}]
    fake.positions_answers = [[_held("ASMLa_EQ", 3.0)]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") == "t212_exit_78"
    assert [p[0] for p in fake.posts] == ["market"]


async def test_close_already_flat(fake):
    fake.positions_answers = [[]]
    assert await LiveBroker()._dispatch_real_close("ASML.AS", 3.0, "EUR") == "t212_exit_already_flat"
    assert fake.positions_calls == 2  # flat is confirmed by a second call
    assert fake.posts == []


async def test_move_stop_replaces_stop_and_keeps_take_profit_rule(fake):
    broker = LiveBroker()
    fake.pending = [{"id": 30, "ticker": "SAPd_EQ", "type": "STOP", "side": "SELL", "quantity": -2.0, "stopPrice": 95.0}]
    await broker._move_broker_stop("SAP.DE", "EUR", 2.0, 95.0)
    assert fake.posts == [] and fake.cancelled == []  # same price: nothing to do
    await broker._move_broker_stop("SAP.DE", "EUR", 2.0, 97.5)
    assert fake.cancelled == [30] and fake.posts == [("stop", "SAPd_EQ", -2.0, 97.5)]
    fake.posts.clear()
    fake.pending = [{"id": 31, "ticker": "SAPd_EQ", "type": "LIMIT", "side": "SELL", "quantity": -2.0}]
    await broker._move_broker_stop("SAP.DE", "EUR", 2.0, 98.0)
    assert fake.posts == []  # a non-stop SELL is resting: no extra stop


async def test_sync_adopts_untracked_and_closes_only_when_confirmed(fake, clean_snapshots):
    tickers = ["SAP.DE", "MC.PA", "ASML.AS"]
    async with AsyncSessionLocal() as s:
        for t in ("SAP.DE", "MC.PA"):
            s.add(Position(ticker=t, name=t, currency="EUR", shares=2.0, entry_price_native=100.0, current_price_native=100.0,
                           highest_price_native=100.0, entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=200.0,
                           status="OPEN", is_live=True))
        s.add(OrderTrigger(ticker="ASML.AS", target_price=600.0, current_price_at_creation=610.0, stop_loss=570.0,
                           take_profit=700.0, allocation_eur=600.0, status="TRIGGERED", is_live=True))
        await s.commit()
    broker = LiveBroker()
    base = [_held("SAPd_EQ", 2.0), _held("ASMLa_EQ", 1.0, avg=600.0)]
    fake.history = [{"order": {"id": 50, "side": "SELL", "quantity": -2.0}, "fill": {"quantity": -2.0, "price": 104.0, "filledAt": "2026-10-31T10:00:00Z"}}]
    try:
        with patch.object(broker, "refresh_positions_pnl", AsyncMock()), \
             patch.object(broker, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0})), \
             patch("src.trading.live.market_data_provider.get_stock_data", return_value=None), \
             patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()) as notify:
            # First answer misses MC, the confirming second answer still holds it: nothing is closed
            fake.positions_answers = [base, base + [_held("MCp_EQ", 2.0)]]
            await broker.sync_account_state()
            async with AsyncSessionLocal() as s:
                open_ = {p.ticker: p for p in (await s.execute(select(Position).where(
                    Position.ticker.in_(tickers), Position.status == "OPEN", Position.is_live == True))).scalars().all()}
            assert set(open_) == {"SAP.DE", "MC.PA", "ASML.AS"}
            assert (open_["ASML.AS"].shares, open_["ASML.AS"].stop_loss_native) == (1.0, 570.0)
            assert ("stop", "ASMLa_EQ", -1.0, 570.0) in fake.posts
            assert notify.await_count == 1

            # Both answers miss MC: it is booked as closed at the history fill price
            fake.positions_calls = 0
            fake.positions_answers = [base]
            await broker.sync_account_state()
            async with AsyncSessionLocal() as s:
                mc = (await s.execute(select(Position).where(Position.ticker == "MC.PA", Position.is_live == True))).scalars().first()
                assert mc.status == "CLOSED" and mc.current_price_native == 104.0
                assert len((await s.execute(select(Position).where(Position.ticker == "ASML.AS", Position.is_live == True))).scalars().all()) == 1
                snap = (await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(PortfolioSnapshot.id.desc()))).scalars().first()
                assert (snap.cash_eur, snap.total_value_eur, snap.equity_eur) == (500.0, 1500.0, 1000.0)
            assert broker._available_funds_eur == 400.0
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Position).where(Position.ticker.in_(tickers)))
            await s.execute(delete(Trade).where(Trade.ticker.in_(tickers)))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "ASML.AS"))
            await s.commit()


async def test_kill_switch_blocks_trading212_buy(fake, monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    res = await LiveBroker().execute_buy("ASML.AS", "ASML", 600.0, "EUR", 570.0, 700.0, 600.0, 90, "test", is_manual=True)
    assert res["success"] is False and "kill switch" in res["message"].lower()
    assert fake.posts == []


async def test_check_connection_reports_demo_without_account_id(fake, monkeypatch):
    monkeypatch.setattr(settings, "TRADING212_DEMO", True)
    status = await LiveBroker().check_connection(force=True)
    assert status["connected"] is True and status["environment"] == "DEMO"
    assert "account_id" not in status and "accounts" not in status


async def test_client_retries_get_on_429_but_never_post(monkeypatch):
    monkeypatch.setattr(trading212, "MAX_RATE_WAIT", 0.01)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/equity/orders/7"):
            return httpx.Response(404, text="Order not found")
        if len(calls) == 1 or request.method == "POST":
            return httpx.Response(429, headers={"x-ratelimit-reset": "0"}, text="Limited")
        return httpx.Response(200, json=[])
    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    assert await client.pending_orders() == []
    assert calls == [("GET", "/api/v0/equity/orders")] * 2
    with pytest.raises(trading212.Trading212Error) as err:
        await client.place_market("AAPL_US_EQ", 1.0)
    assert err.value.status == 429
    assert [c for c in calls if c[0] == "POST"] == [("POST", "/api/v0/equity/orders/market")]
    assert await client.get_order(7) is None


async def test_resolve_refuses_symbol_on_wrong_exchange(fake):
    # ASML.PA does not exist; the Amsterdam/Xetra listings must not be picked for it
    assert await fake.resolve_ticker("ASML.PA", "EUR") is None


async def test_buy_unknown_outcome_books_position_growth(fake):
    fake.vanish = True  # 404 right after the POST and nothing in history yet
    fake.positions_answers = [[_held("SAPd_EQ", 1.5, avg=99.5)]]
    res = await LiveBroker()._dispatch_real_order("SAP.DE", 1.5, 100.0, "EUR", 95.0, 110.0)
    assert res == ("t212_1", 1.5, 99.5)
    assert fake.posts[-1] == ("stop", "SAPd_EQ", -1.5, 95.0)


async def test_buy_unknown_outcome_without_new_shares_books_nothing(fake):
    fake.vanish = True
    fake.positions_answers = [[]]
    assert await LiveBroker()._dispatch_real_order("SAP.DE", 1.5, 100.0, "EUR", 95.0, 110.0) == ("t212_1", 0.0, 100.0)
    assert [p[0] for p in fake.posts] == ["limit"]


async def test_buy_post_validation_error_is_not_looked_up(fake):
    fake.limit_error = trading212.Trading212Error(400, "Failed validation")
    fake.pending = [{"id": 9, "ticker": "AAPL_US_EQ", "quantity": 1.0, "status": "NEW",
                     "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat()}]
    assert await LiveBroker()._dispatch_real_order("AAPL", 1.0, 200.0, "USD", 190.0, 220.0) is None


async def test_stop_failure_retries_once_then_alerts(fake):
    with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()) as notify:
        fake.fill_on_poll, fake.stop_errors = (1.0, 100.0), 1
        assert (await LiveBroker()._dispatch_real_order("SAP.DE", 1.0, 100.0, "EUR", 95.0, 110.0))[1] == 1.0
        assert [p[0] for p in fake.posts] == ["limit", "stop", "stop"] and notify.await_count == 0
        fake.posts.clear()
        fake.order_state.clear()
        fake.stop_errors = 2
        await LiveBroker()._move_broker_stop("SAP.DE", "EUR", 1.0, 97.0)
        assert [p[0] for p in fake.posts] == ["stop", "stop"] and notify.await_count == 1


async def test_sync_waits_for_inflight_buy(fake, clean_snapshots):
    broker = LiveBroker()
    with patch.object(broker, "refresh_positions_pnl", AsyncMock()), \
         patch.object(broker, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0})):
        await broker._t212_lock.acquire()  # a buy is in flight
        task = asyncio.create_task(broker.sync_account_state())
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done() and fake.positions_calls == 0
        broker._t212_lock.release()
        await task
    assert fake.positions_calls == 1


async def test_instruments_fetched_once_and_stale_cache_served(monkeypatch):
    calls = []
    fail = []

    def handler(request):
        calls.append(request.url.path)
        if fail:
            return httpx.Response(503, text="down")
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json=[{"ticker": "SAPd_EQ", "shortName": "SAP", "workingScheduleId": 1}])
        return httpx.Response(200, json=[{"name": "Deutsche Börse Xetra", "workingSchedules": [{"id": 1}]}])
    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    first, second = await asyncio.gather(client.instruments(), client.instruments())
    assert first is second and len(calls) == 2 and first[0]["exchange"] == "Deutsche Börse Xetra"
    client._instruments_at -= trading212.INSTRUMENTS_TTL + 1
    fail.append(True)
    assert (await client.instruments())[0]["ticker"] == "SAPd_EQ"
