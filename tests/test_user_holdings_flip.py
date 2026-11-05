"""Bot -> Mine flip: release_to_user cancels the bot's T212 stop and pending buys, then moves the position to My holdings."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import OrderTrigger, Position, Trade, UserHolding
from src.agents.committee import investment_committee
from src.data.market import market_data_provider
from src.db.session import AsyncSessionLocal, init_db
from src.trading import trading212
from src.trading.holdings import pick_bot_levels
from src.trading.live import LiveBroker
from tests.test_trading212_broker import FakeT212, _held, _live, clean_snapshots, fake  # noqa: F401

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


async def test_trailing_stop_skips_position_released_mid_refresh(fake, bot_position, monkeypatch):
    """The flip lands while refresh_positions_pnl fetches a quote: no new stop may go on the user's shares."""
    monkeypatch.setattr(settings, "TRAILING_STOP_ATR_MULT", 0.0)
    broker = LiveBroker()
    released = []

    async def quote_then_release(fn, *args, **kwargs):
        if not released:
            released.append(await broker.release_to_user(TICKER))
        return {"price": 700.0, "technicals": {}}
    monkeypatch.setattr(asyncio, "to_thread", quote_then_release)
    await broker.refresh_positions_pnl()
    assert released[0]["success"] is True
    assert 88 in fake.cancelled
    assert [p for p in fake.posts if p[0] == "stop"] == []
    pos, holding, _, _ = await _state()
    assert pos.status == "RELEASED" and holding is not None


async def test_exit_waits_for_flip_and_skips_released_position(fake, bot_position):
    """A sell that starts while a flip holds the lock runs after it and finds no bot position to sell."""
    fake.positions_answers = [[_held("ASMLa_EQ", 1.0)]]
    broker = LiveBroker()
    await broker._t212_lock.acquire()
    try:
        release = asyncio.create_task(broker.release_to_user(TICKER))
        await asyncio.sleep(0)
        sell = asyncio.create_task(broker.execute_sell(TICKER, reason="test exit"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not sell.done()
        assert fake.posts == []
    finally:
        broker._t212_lock.release()
    assert (await release)["success"] is True
    res = await sell
    assert res == {"success": False, "message": f"No open live position found for {TICKER}."}
    assert fake.posts == []
    pos, _, sells, _ = await _state()
    assert pos.status == "RELEASED" and sells == []


class FakeCommittee:
    """Market data at 610 EUR; each report sets entry 2% under its price, stop 5% under entry, R:R 2."""

    def __init__(self):
        self.prices = []
        self.scores = {600.0: 60, 610.0: 72}
        self.error = None
        self.lock = None

    async def evaluate(self, data, equity):
        assert self.lock is None or not self.lock.locked()  # the slow committee must not block the sync
        self.prices.append(data["price"])
        if self.error:
            raise self.error
        entry = data["price"] * 0.98
        return {"consensus_score": self.scores[data["price"]],
                "risk": {"entry_trigger_price": entry, "stop_loss_price": entry * 0.95, "risk_reward_ratio": 2.0}}


@pytest.fixture
def committee(monkeypatch):
    c = FakeCommittee()
    monkeypatch.setattr(investment_committee, "evaluate_ticker", c.evaluate)
    monkeypatch.setattr(market_data_provider, "get_stock_data",
                        lambda t: {"ticker": t, "price": 610.0, "currency": "EUR"})
    return c


@pytest.fixture
async def mine_holding(fake, monkeypatch, committee):
    """Live T212 mode, ASML.AS in My holdings and held at T212 (1 share, avg 600)."""
    await init_db()
    _live(monkeypatch)
    fake.positions_answers = [[_held("ASMLa_EQ", 1.0, avg=600.0)]]
    async with AsyncSessionLocal() as s:
        s.add(UserHolding(ticker=TICKER, t212_ticker="ASMLa_EQ", name="ASML", currency="EUR", shares=1.0,
                          avg_price_native=600.0, current_price_native=610.0, current_price_eur=610.0, cost_basis_eur=600.0))
        await s.commit()
    try:
        with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()):
            yield
    finally:
        async with AsyncSessionLocal() as s:
            for model in (Position, UserHolding, Trade, OrderTrigger):
                await s.execute(delete(model).where(model.ticker == TICKER))
            await s.commit()


async def _take_state():
    async with AsyncSessionLocal() as s:
        pos = (await s.execute(select(Position).where(Position.ticker == TICKER))).scalars().first()
        holding = (await s.execute(select(UserHolding).where(UserHolding.ticker == TICKER))).scalars().first()
        return pos, holding


def test_pick_bot_levels_higher_score_wins():
    sets = [(60, 570.0, 660.0, "committee, buy price"), (72, 579.5, 671.0, "committee, current price")]
    assert pick_bot_levels(sets, 615.0) == (579.5, 671.0, "committee, current price, score 72")


def test_pick_bot_levels_drops_set_that_would_sell_at_once():
    sets = [(90, 570.0, 615.0, "target at price"), (80, 615.0, 700.0, "stop at price"), (50, 590.0, 640.0, "ok")]
    assert pick_bot_levels(sets, 615.0) == (590.0, 640.0, "ok, score 50")


def test_pick_bot_levels_default_when_none_valid(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_STOP_LOSS_PCT", 0.04)
    monkeypatch.setattr(settings, "DEFAULT_TAKE_PROFIT_PCT", 0.08)
    assert pick_bot_levels([(90, 620.0, 700.0, "x")], 615.0) == (590.4, 664.2, "default")
    assert pick_bot_levels([], 615.0) == (590.4, 664.2, "default")


async def test_take_adopts_with_plan_and_places_stop(fake, mine_holding, committee):
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=TICKER, target_price=600.0, current_price_at_creation=610.0, stop_loss=570.0,
                           take_profit=700.0, allocation_eur=600.0, status="PENDING", is_live=True))
        await s.commit()
    res = await LiveBroker().take_from_user(TICKER.lower())
    assert res == {"success": True, "message": "The bot now trades ASML.AS. Stop 570.00, target 700.00 (buy plan)."}
    pos, holding = await _take_state()
    assert holding is None
    assert (pos.status, pos.is_live, pos.shares, pos.stop_loss_native, pos.take_profit_native) == ("OPEN", True, 1.0, 570.0, 700.0)
    assert ("stop", "ASMLa_EQ", -1.0, 570.0) in fake.posts
    assert committee.prices == []


async def test_take_without_plan_gets_committee_levels(fake, mine_holding, committee):
    fake.positions_answers = [[dict(_held("ASMLa_EQ", 1.0, avg=600.0), currentPrice=615.0)]]
    broker = LiveBroker()
    committee.lock = broker._t212_lock
    res = await broker.take_from_user(TICKER)
    assert res == {"success": True,
                   "message": "The bot now trades ASML.AS. Stop 579.50, target 671.00 (committee, current price, score 72)."}
    assert sorted(committee.prices) == [600.0, 610.0]
    pos, holding = await _take_state()
    assert holding is None
    assert (pos.status, pos.stop_loss_native, pos.take_profit_native) == ("OPEN", 579.5, 671.0)
    assert ("stop", "ASMLa_EQ", -1.0, 579.5) in fake.posts


async def test_take_uses_default_when_every_committee_set_would_sell_at_once(fake, mine_holding, committee):
    """At 672 both committee targets (671, 660) are under the price, so the default levels apply."""
    fake.positions_answers = [[dict(_held("ASMLa_EQ", 1.0, avg=600.0), currentPrice=672.0)]]
    res = await LiveBroker().take_from_user(TICKER)
    assert res["message"] == "The bot now trades ASML.AS. Stop 645.12, target 725.76 (default)."
    pos, _ = await _take_state()
    assert (pos.stop_loss_native, pos.take_profit_native) == (645.12, 725.76)


async def test_take_when_committee_fails_uses_default_levels(fake, mine_holding, committee):
    committee.error = RuntimeError("LLM down")
    fake.positions_answers = [[dict(_held("ASMLa_EQ", 1.0, avg=600.0), currentPrice=615.0)]]
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": True, "message": "The bot now trades ASML.AS. Stop 590.40, target 664.20 (default)."}
    pos, holding = await _take_state()
    assert holding is None
    assert (pos.stop_loss_native, pos.take_profit_native) == (590.4, 664.2)
    assert ("stop", "ASMLa_EQ", -1.0, 590.4) in fake.posts


async def test_take_without_t212_price_uses_market_price(fake, mine_holding, committee):
    res = await LiveBroker().take_from_user(TICKER)
    assert res["message"] == "The bot now trades ASML.AS. Stop 579.50, target 671.00 (committee, current price, score 72)."
    pos, _ = await _take_state()
    assert pos.stop_loss_native == 579.5


async def test_take_without_any_price_tracks_without_stop(fake, mine_holding, committee, monkeypatch):
    monkeypatch.setattr(market_data_provider, "get_stock_data", lambda t: None)
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": True, "message": "The bot now trades ASML.AS."}
    pos, _ = await _take_state()
    assert pos.status == "OPEN" and pos.stop_loss_native is None
    assert not [p for p in fake.posts if p[0] == "stop"]


async def test_take_skips_committee_on_currency_mismatch(fake, mine_holding, committee, monkeypatch):
    monkeypatch.setattr(market_data_provider, "get_stock_data", lambda t: {"ticker": t, "price": 6.1, "currency": "USD"})
    fake.positions_answers = [[dict(_held("ASMLa_EQ", 1.0, avg=600.0), currentPrice=615.0)]]
    res = await LiveBroker().take_from_user(TICKER)
    assert res["message"] == "The bot now trades ASML.AS. Stop 590.40, target 664.20 (default)."
    assert committee.prices == []


async def test_take_with_only_old_triggered_trigger_ignores_its_levels(fake, mine_holding):
    """A TRIGGERED trigger is an already-booked fill: its stale levels must not arm a stop; the committee's do."""
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=TICKER, target_price=600.0, current_price_at_creation=610.0, stop_loss=570.0,
                           take_profit=700.0, allocation_eur=600.0, status="TRIGGERED", is_live=True))
        await s.commit()
    res = await LiveBroker().take_from_user(TICKER)
    assert res["success"] is True
    pos, holding = await _take_state()
    assert holding is None
    assert (pos.status, pos.stop_loss_native, pos.take_profit_native) == ("OPEN", 579.5, 671.0)
    assert [p for p in fake.posts if p[0] == "stop"] == [("stop", "ASMLa_EQ", -1.0, 579.5)]


async def test_take_when_broker_no_longer_holds(fake, mine_holding):
    fake.positions_answers = [[]]
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": False, "message": "Trading 212 no longer holds ASML.AS."}
    assert await _take_state() == (None, None)


async def test_take_when_t212_errors_changes_nothing(fake, mine_holding):
    fake.positions = AsyncMock(side_effect=trading212.Trading212Error(503, "busy"))
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": False, "message": "Could not reach Trading 212. Nothing changed."}
    pos, holding = await _take_state()
    assert pos is None and holding is not None


async def test_take_without_client_changes_nothing(fake, mine_holding, monkeypatch):
    monkeypatch.setattr(trading212, "get_client", lambda: None)
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": False, "message": "Could not reach Trading 212. Nothing changed."}
    pos, holding = await _take_state()
    assert pos is None and holding is not None


async def test_take_not_in_holdings(fake, monkeypatch):
    _live(monkeypatch)
    res = await LiveBroker().take_from_user("NOPOS.AS")
    assert res == {"success": False, "message": "NOPOS.AS is not in your holdings."}


async def test_take_refused_in_paper_mode():
    res = await LiveBroker().take_from_user(TICKER)
    assert res == {"success": False, "message": "Only for live Trading 212."}
