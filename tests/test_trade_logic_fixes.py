import datetime
import time
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select, delete

from config.settings import settings
from src.agents.committee import investment_committee
from src.data.market import market_data_provider
from src.db.models import OrderTrigger, Position, SystemConfig
from src.db.session import init_db, AsyncSessionLocal
from src.scheduler.jobs import TradingScheduler
from src.trading.live import live_broker
from src.trading.paper import paper_broker


def _unique(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}"


async def _set_config(key: str, value: str):
    """Writes a SystemConfig value and returns the previous one (None if it was unset)."""
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
        cfg = res.scalars().first()
        previous = cfg.value if cfg else None
        if cfg:
            cfg.value = value
        else:
            session.add(SystemConfig(key=key, value=value))
        await session.commit()
    return previous


async def _restore_config(key: str, previous):
    if previous is not None:
        await _set_config(key, previous)
        return
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SystemConfig).where(SystemConfig.key == key))
        await session.commit()


# --- Committee verdict and trigger arming ----------------------------------------

@pytest.mark.asyncio
async def test_llm_buy_verdict_cannot_override_risk_officer_veto():
    """A vetoed trade must not keep a trigger-worthy score, whatever the LLM portfolio manager says."""
    stock = {"ticker": "VETO", "name": "Veto Corp", "price": 100.0, "currency": "USD"}
    tech = {"bias": "BULLISH", "confidence": 90, "analysis_summary": "Uptrend", "key_drivers": []}
    fund = {"valuation": "FAIR", "health": "STRONG", "confidence": 70, "analysis_summary": "Solid"}
    sent = {"mood": "POSITIVE", "score": 0.5, "sentiment_summary": "Upbeat"}
    risk = {
        "approved": False,
        "entry_trigger_price": 98.0,
        "trigger_type": "LIMIT_PULLBACK",
        "stop_loss_price": 95.0,
        "take_profit_price": 110.0,
        "recommended_size_pct": 0.08,
        "risk_reward_ratio": 2.1,
        "risk_summary": "Fee drag too high",
    }
    llm_verdict = {"action": "STRONG_BUY", "consensus_score": 88, "verdict_summary": "Buy it", "debate_transcript": "Debate"}

    with patch("src.agents.committee.TechnicalAnalystAgent.analyze", AsyncMock(return_value=tech)), \
         patch("src.agents.committee.FundamentalAnalystAgent.analyze", AsyncMock(return_value=fund)), \
         patch("src.agents.committee.SentimentAnalystAgent.analyze", AsyncMock(return_value=sent)), \
         patch("src.agents.committee.RiskManagerAgent.evaluate_risk", AsyncMock(return_value=risk)), \
         patch("src.agents.committee.llm_client.has_active_key", return_value=True), \
         patch("src.agents.committee.llm_client.generate_json", AsyncMock(return_value=llm_verdict)):
        report = await investment_committee.evaluate_ticker(stock, 10000.0)

    assert report["action"] == "HOLD"
    assert report["consensus_score"] <= 55


def test_trigger_is_armed_only_for_an_approved_buy_verdict():
    report = {"consensus_score": 85, "action": "BUY", "risk": {"approved": True}}
    assert TradingScheduler._should_arm_trigger(report, 80)
    assert TradingScheduler._should_arm_trigger({**report, "action": "STRONG_BUY"}, 80)
    assert not TradingScheduler._should_arm_trigger({**report, "consensus_score": 79}, 80)
    # Rule fallback with MIN_CONSENSUS_SCORE raised to 82 by the learner: 81 clears the trigger bar but is a HOLD
    assert not TradingScheduler._should_arm_trigger({**report, "consensus_score": 81, "action": "HOLD"}, 80)
    assert not TradingScheduler._should_arm_trigger({**report, "risk": {"approved": False}}, 80)


# --- Paper trigger execution guards ------------------------------------------------

async def _insert_paper_trigger(ticker, trigger_type, target, current, stop, take_profit, created_at=None):
    """Inserts a funded pending trigger directly, so the test does not depend on the shared cash balance."""
    async with AsyncSessionLocal() as session:
        trigger = OrderTrigger(
            ticker=ticker,
            name=f"{ticker} Corp",
            currency="USD",
            trigger_type=trigger_type,
            target_price=target,
            current_price_at_creation=current,
            stop_loss=stop,
            take_profit=take_profit,
            allocation_eur=300.0,
            consensus_score=90,
            ai_rationale="Test trigger",
            status="PENDING",
            is_live=False,
        )
        if created_at is not None:
            trigger.created_at = created_at
        session.add(trigger)
        await session.commit()
        return trigger.id


async def _trigger_status(trigger_id):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == trigger_id))
        return res.scalars().first().status


async def _has_open_paper_position(ticker):
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Position).where(Position.ticker == ticker, Position.status == "OPEN", Position.is_live == False)
        )
        return res.scalars().first() is not None


async def _run_paper_triggers_at(ticker, price):
    quote = {"ticker": ticker, "price": price, "currency": "USD", "name": f"{ticker} Corp"}
    with patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
         patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.risk.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.paper.market_data_provider.get_stock_data",
               side_effect=lambda sym: quote if sym == ticker else None):
        return await paper_broker.check_and_execute_triggers()


async def _delete_paper_rows(ticker):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        await session.execute(delete(Position).where(Position.ticker == ticker))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_type,target,current,stop,take_profit,price", [
    ("LIMIT_PULLBACK", 50.0, 55.0, 45.0, 65.0, 44.0),   # gapped down through the stop
    ("BREAKOUT_BUY", 105.0, 100.0, 98.0, 112.0, 113.0),  # gapped up past the target
])
async def test_trigger_that_gaps_through_its_levels_is_cancelled_not_bought(trigger_type, target, current, stop, take_profit, price):
    await init_db()
    ticker = _unique("GAP")
    trigger_id = await _insert_paper_trigger(ticker, trigger_type, target, current, stop, take_profit)
    try:
        executed = await _run_paper_triggers_at(ticker, price)

        assert not any(t.get("trigger_id") == trigger_id for t in executed)
        assert await _trigger_status(trigger_id) == "CANCELLED"
        assert not await _has_open_paper_position(ticker)
    finally:
        await _delete_paper_rows(ticker)


@pytest.mark.asyncio
async def test_stale_trigger_expires_instead_of_buying():
    await init_db()
    ticker = _unique("STALE")
    old = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=settings.TRIGGER_EXPIRY_DAYS + 1)
    trigger_id = await _insert_paper_trigger(ticker, "LIMIT_PULLBACK", 50.0, 55.0, 45.0, 65.0, created_at=old)
    try:
        executed = await _run_paper_triggers_at(ticker, 49.0)

        assert not any(t.get("trigger_id") == trigger_id for t in executed)
        assert await _trigger_status(trigger_id) == "EXPIRED"
        assert not await _has_open_paper_position(ticker)
    finally:
        await _delete_paper_rows(ticker)


@pytest.mark.asyncio
async def test_triggers_wait_while_auto_buy_is_paused():
    await init_db()
    ticker = _unique("PAUSED")
    trigger_id = await _insert_paper_trigger(ticker, "LIMIT_PULLBACK", 50.0, 55.0, 45.0, 65.0)
    previous = await _set_config("AUTO_BUY_ENABLED", "false")
    try:
        executed = await _run_paper_triggers_at(ticker, 49.0)

        assert not any(t.get("trigger_id") == trigger_id for t in executed)
        assert await _trigger_status(trigger_id) == "PENDING"
        assert not await _has_open_paper_position(ticker)
    finally:
        await _restore_config("AUTO_BUY_ENABLED", previous)
        await _delete_paper_rows(ticker)


# --- Live IBKR exit ---------------------------------------------------------------

class _FakeTrade:
    """An open IBKR order. After a cancel request it settles (cancelled, or filled first) after `settle_polls` polls."""

    def __init__(self, contract, action, settle_polls=0, on_settle=None):
        self.contract = contract
        self.order = SimpleNamespace(action=action)
        self.cancel_requested = False
        self.done = False
        self._settle_polls = settle_polls
        self._on_settle = on_settle

    def isDone(self):
        if self.cancel_requested and not self.done:
            if self._settle_polls > 0:
                self._settle_polls -= 1
            else:
                self.done = True
                if self._on_settle:
                    self._on_settle()
        return self.done


class _FakeIB:
    """Minimal stand-in for an ib_async IB client holding one NVDA position."""

    def __init__(self, held: float):
        self.held = held
        self.trades = []
        self.placed = []

    def isConnected(self):
        return True

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    def openTrades(self):
        return [t for t in self.trades if not t.done]

    def cancelOrder(self, order):
        for t in self.trades:
            if t.order is order:
                t.cancel_requested = True

    async def reqPositionsAsync(self):
        contract = SimpleNamespace(symbol="NVDA", currency="USD")
        return [SimpleNamespace(contract=contract, position=self.held)] if self.held else []

    def positions(self):
        raise AssertionError("the exit must read fresh positions, not the cached list")

    def placeOrder(self, contract, order):
        self.placed.append(order)
        return SimpleNamespace(order=SimpleNamespace(orderId=77))


_NVDA = SimpleNamespace(symbol="NVDA", currency="USD")
_AAPL = SimpleNamespace(symbol="AAPL", currency="USD")


async def _ibkr_close(ib, shares):
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"
    try:
        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=ib)):
            return await live_broker._dispatch_real_close("NVDA", shares=shares, currency="USD")
    finally:
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_live_exit_cancels_the_bracket_legs_then_sells_what_is_held():
    ib = _FakeIB(held=10.0)
    stop_leg, target_leg = _FakeTrade(_NVDA, "SELL"), _FakeTrade(_NVDA, "SELL")
    other_leg = _FakeTrade(_AAPL, "SELL")
    ib.trades = [stop_leg, target_leg, other_leg]

    order_id = await _ibkr_close(ib, shares=12.0)

    assert order_id == "ibkr_exit_77"
    assert stop_leg.cancel_requested and target_leg.cancel_requested
    assert not other_leg.cancel_requested
    assert len(ib.placed) == 1
    assert ib.placed[0].action == "SELL"
    assert ib.placed[0].totalQuantity == 10.0  # never more than the broker holds


@pytest.mark.asyncio
async def test_live_exit_sends_nothing_when_a_leg_fills_while_being_cancelled():
    """The stop leg fills during the cancel: selling the full size afterwards would open a short."""
    ib = _FakeIB(held=10.0)
    stop_leg = _FakeTrade(_NVDA, "SELL", settle_polls=3, on_settle=lambda: setattr(ib, "held", 0.0))
    ib.trades = [stop_leg]

    order_id = await _ibkr_close(ib, shares=10.0)

    assert order_id == "ibkr_exit_already_flat"
    assert ib.placed == []


# --- Live Alpaca exit -------------------------------------------------------------

class _FakeAlpacaClient:
    def __init__(self, has_position: bool):
        self.has_position = has_position
        self.cancelled = []
        self.closed = []

    def __call__(self, api_key, secret_key, paper):  # stands in for the TradingClient constructor
        return self

    def get_orders(self, request):
        return [SimpleNamespace(id="stop-leg"), SimpleNamespace(id="target-leg")]

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def get_open_position(self, symbol):
        if not self.has_position:
            from alpaca.common.exceptions import APIError
            not_found = SimpleNamespace(response=SimpleNamespace(status_code=404))
            raise APIError('{"code": 40410000, "message": "position does not exist"}', not_found)
        return SimpleNamespace(symbol=symbol, qty="10")

    def close_position(self, symbol):
        self.closed.append(symbol)
        return SimpleNamespace(id="close-1")


async def _alpaca_close(client):
    saved = (settings.LIVE_BROKER, settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY)
    settings.LIVE_BROKER, settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY = "alpaca", "key", "secret"
    try:
        with patch("alpaca.trading.client.TradingClient", client):
            return await live_broker._dispatch_real_close("NVDA", shares=10.0, currency="USD")
    finally:
        settings.LIVE_BROKER, settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY = saved


@pytest.mark.asyncio
async def test_alpaca_exit_reports_a_position_closed_by_its_bracket_leg():
    client = _FakeAlpacaClient(has_position=False)

    order_id = await _alpaca_close(client)

    assert order_id == "alpaca_exit_already_flat"
    assert client.cancelled == ["stop-leg", "target-leg"]
    assert client.closed == []


@pytest.mark.asyncio
async def test_alpaca_exit_closes_an_open_position():
    client = _FakeAlpacaClient(has_position=True)

    order_id = await _alpaca_close(client)

    assert order_id == "alpaca_exit_close-1"
    assert client.cancelled == ["stop-leg", "target-leg"]
    assert client.closed == ["NVDA"]


# --- Live position bookkeeping ----------------------------------------------------

class _LiveMode:
    """Arms live trading for one test and restores the settings afterwards."""

    def __enter__(self):
        self.saved = (
            settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE,
            settings.ENFORCE_MARKET_HOURS, settings.LIVE_BROKER,
        )
        settings.TRADING_MODE = "LIVE"
        settings.LIVE_TRADING_ENABLED = True
        settings.KILL_SWITCH_ACTIVE = False
        settings.ENFORCE_MARKET_HOURS = False
        settings.LIVE_BROKER = "ibkr"
        return self

    def __exit__(self, *exc):
        (
            settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE,
            settings.ENFORCE_MARKET_HOURS, settings.LIVE_BROKER,
        ) = self.saved


def _live_position(ticker, **overrides):
    values = dict(
        ticker=ticker, name=f"{ticker} Corp", currency="USD", shares=1.0,
        entry_price_native=100.0, current_price_native=100.0, highest_price_native=100.0,
        stop_loss_native=96.0, take_profit_native=120.0,
        entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=100.0,
        unrealized_pnl_eur=0.0, unrealized_pnl_pct=0.0, status="OPEN", is_live=True,
        broker_order_id="ibkr_1",
    )
    values.update(overrides)
    return Position(**values)


async def _delete_positions(*tickers):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Position).where(Position.ticker.in_(tickers)))
        await session.commit()


@pytest.mark.asyncio
async def test_live_sell_that_never_reaches_the_broker_keeps_the_position_open():
    await init_db()
    ticker = _unique("NOEXIT")
    async with AsyncSessionLocal() as session:
        session.add(_live_position(ticker))
        await session.commit()
    try:
        with _LiveMode(), \
             patch("src.trading.live.check_market_hours", return_value=(True, "Market is OPEN")), \
             patch("src.trading.live.market_data_provider.get_stock_data", return_value={"price": 95.0, "currency": "USD"}), \
             patch.object(live_broker, "_dispatch_real_close", AsyncMock(return_value=None)):
            res = await live_broker.execute_sell(ticker, reason="Stop-Loss")

        assert res["success"] is False
        assert "could not be placed" in res["message"]
        async with AsyncSessionLocal() as session:
            pos_res = await session.execute(select(Position).where(Position.ticker == ticker))
            assert pos_res.scalars().first().status == "OPEN"
    finally:
        await _delete_positions(ticker)


@pytest.mark.asyncio
async def test_live_positions_get_the_breakeven_stop_like_paper():
    await init_db()
    gainer, synced, small = _unique("LIVEBE"), _unique("SYNCED"), _unique("SMALL")
    async with AsyncSessionLocal() as session:
        session.add(_live_position(gainer))
        session.add(_live_position(synced, stop_loss_native=None, take_profit_native=None))  # holding synced from IBKR
        session.add(_live_position(small))
        await session.commit()
    previous = await _set_config("BREAKEVEN_TRIGGER_PCT", "0.025")
    try:
        live_broker._last_pnl_refresh = 0.0
        prices = {gainer: 106.0, synced: 106.0, small: 102.0}
        with _LiveMode(), \
             patch("src.trading.live.check_market_hours", return_value=(True, "Market is OPEN")), \
             patch.object(live_broker, "_fetch_ibkr_prices", AsyncMock(return_value=prices)), \
             patch("src.trading.live.market_data_provider.get_stock_data", return_value=None):
            await live_broker.refresh_positions_pnl()

        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.ticker.in_([gainer, synced, small])))
            by_ticker = {p.ticker: p for p in res.scalars().all()}
        assert by_ticker[gainer].highest_price_native == 106.0
        assert by_ticker[gainer].stop_loss_native == 101.0  # breakeven + 1%
        assert by_ticker[synced].highest_price_native == 106.0
        assert by_ticker[synced].stop_loss_native is None  # no bot stop is invented for a synced holding
        assert by_ticker[small].highest_price_native == 102.0
        assert by_ticker[small].stop_loss_native == 96.0  # +2% is below the 2.5% trigger
    finally:
        await _restore_config("BREAKEVEN_TRIGGER_PCT", previous)
        await _delete_positions(gainer, synced, small)
        live_broker._last_pnl_refresh = 0.0


# --- Market data -------------------------------------------------------------------

def test_market_data_fetches_enough_history_for_a_200_day_sma():
    periods = []
    closes = np.linspace(50.0, 150.0, 250)

    class FakeTicker:
        fast_info = {"lastPrice": 150.0, "currency": "USD"}
        info = {}

        def __init__(self, sym):
            pass

        def history(self, period):
            periods.append(period)
            return pd.DataFrame(
                {"Close": closes, "High": closes + 1.0, "Low": closes - 1.0, "Volume": 1000.0},
                index=pd.date_range("2025-10-01", periods=len(closes)),
            )

    with patch("src.data.market.yf.Ticker", FakeTicker):
        market_data_provider.invalidate(["SMATEST"])
        data = market_data_provider.get_stock_data("SMATEST")
        market_data_provider.invalidate(["SMATEST"])

    assert periods == ["1y"]
    assert data["technicals"]["sma200"] == round(float(closes[-200:].mean()), 2)


@pytest.mark.asyncio
async def test_trigger_distance_uses_the_last_quote_while_the_market_is_closed():
    """Outside market hours the dashboard showed the price at creation, so a trigger already past its limit looked 'away'."""
    await init_db()
    ticker = _unique("DIST")
    trigger_id = await _insert_paper_trigger(ticker, "LIMIT_PULLBACK", 100.0, 101.2, 95.0, 110.0)
    try:
        quote = {"ticker": ticker, "price": 99.0, "currency": "USD"}
        with patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
             patch("src.trading.paper.check_market_hours", return_value=(False, "Market is CLOSED")), \
             patch("src.trading.paper.market_data_provider.get_stock_data",
                   side_effect=lambda sym: quote if sym == ticker else None):
            triggers = await paper_broker.get_triggers()
        found = next(t for t in triggers if t["id"] == trigger_id)
        assert found["current_price"] == 99.0
        assert found["distance_pct"] > 0  # price is below the pullback limit
    finally:
        await _delete_paper_rows(ticker)
