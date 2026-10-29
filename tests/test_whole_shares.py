"""Whole-share sizing: IBKR's API rejects fractional quantities, so buys are sized in whole shares."""
import time
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest
from sqlalchemy import select, delete

from config.settings import settings
from src.db.models import OrderTrigger
from src.db.session import init_db, AsyncSessionLocal
from src.scheduler.jobs import TradingScheduler
from src.trading.live import LiveBroker
from src.trading.paper import paper_broker
from src.trading.risk import whole_share_count


def test_whole_share_count_picks_the_closest_affordable_count():
    # €1,250 slot: 2 x €700 would be 12% over, so 1 share; 2 x €650 is 4% over and closer
    assert whole_share_count(1250.0, 700.0, round_up_pct=10.0) == 1
    assert whole_share_count(1250.0, 650.0, round_up_pct=10.0) == 2
    # Rounding up must also be closer than rounding down
    assert whole_share_count(1250.0, 100.0, round_up_pct=10.0) == 12
    assert whole_share_count(1000.0, 260.0, round_up_pct=10.0) == 4
    assert whole_share_count(1000.0, 300.0, round_up_pct=10.0) == 3


def test_whole_share_count_respects_cash_and_unaffordable_prices():
    assert whole_share_count(1000.0, 260.0, max_eur=1000.0, round_up_pct=10.0) == 3
    assert whole_share_count(1250.0, 2200.0, round_up_pct=10.0) == 0
    # One share up to the round-up tolerance above the allocation still fits
    assert whole_share_count(1000.0, 1080.0, round_up_pct=10.0) == 1
    assert whole_share_count(0.0, 100.0) == 0


def test_trigger_arming_checks_one_share_fits(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    assert TradingScheduler._fits_whole_share(1000.0, 260.0, "EUR")
    assert not TradingScheduler._fits_whole_share(1000.0, 5000.0, "EUR")
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", False)
    assert TradingScheduler._fits_whole_share(1000.0, 5000.0, "EUR")


@pytest.mark.asyncio
async def test_unaffordable_trigger_is_cancelled_to_release_its_cash(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    ticker = f"PRICEY_{int(time.time() * 1000)}"
    async with AsyncSessionLocal() as session:
        trigger = OrderTrigger(
            ticker=ticker, name="Pricey Corp", currency="EUR", trigger_type="LIMIT_PULLBACK",
            target_price=5000.0, current_price_at_creation=5050.0, stop_loss=4800.0, take_profit=5500.0,
            allocation_eur=300.0, consensus_score=90, ai_rationale="Test", status="PENDING", is_live=False,
        )
        session.add(trigger)
        await session.commit()
        trigger_id = trigger.id

    quote = {"ticker": ticker, "price": 5000.0, "currency": "EUR", "name": "Pricey Corp"}
    with patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
         patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=lambda s: quote if s == ticker else None), \
         patch("src.trading.paper.risk_guardrail.validate_buy_order", return_value=(True, "Approved")):
        executed = await paper_broker.check_and_execute_triggers()

    assert not any(e.get("ticker") == ticker for e in executed)
    async with AsyncSessionLocal() as session:
        status = (await session.execute(select(OrderTrigger.status).where(OrderTrigger.id == trigger_id))).scalar()
        await session.execute(delete(OrderTrigger).where(OrderTrigger.id == trigger_id))
        await session.commit()
    assert status == "CANCELLED"


@pytest.mark.asyncio
async def test_ibkr_rejection_is_detected_before_recording_a_position():
    rejected = SimpleNamespace(
        orderStatus=SimpleNamespace(status="Inactive"),
        log=[SimpleNamespace(message="Fractional-sized order cannot be placed via API.")],
    )
    accepted = SimpleNamespace(orderStatus=SimpleNamespace(status="Submitted"), log=[])
    pending = SimpleNamespace(orderStatus=SimpleNamespace(status="PendingSubmit"), log=[])
    assert "Fractional" in await LiveBroker._ibkr_order_rejection(rejected)
    assert await LiveBroker._ibkr_order_rejection(accepted) is None
    assert await LiveBroker._ibkr_order_rejection(pending, timeout=0.3) is None


@pytest.mark.asyncio
async def test_ibkr_info_10349_is_not_a_rejection():
    # ib_async marks the order Cancelled on 10349 (TIF preset notice) although IBKR keeps working it
    trade = SimpleNamespace(
        orderStatus=SimpleNamespace(status="Cancelled"),
        log=[SimpleNamespace(message="Order TIF was set to DAY based on order preset.", errorCode=10349)],
    )
    assert await LiveBroker._ibkr_order_rejection(trade, timeout=0.3) is None


@pytest.mark.asyncio
async def test_ibkr_bracket_tif_explicit():
    placed = []
    bracket = [SimpleNamespace(action=a, orderId=i, tif="") for i, a in enumerate(["BUY", "SELL", "SELL"])]
    ib = SimpleNamespace(
        isConnected=lambda: True,
        qualifyContractsAsync=AsyncMock(),
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(validExchanges="SMART", marketRuleIds="26")]),
        reqMarketRuleAsync=AsyncMock(return_value=[SimpleNamespace(lowEdge=0.0, increment=0.01)]),
        bracketOrder=lambda **kw: bracket,
        placeOrder=lambda c, o: placed.append(o) or SimpleNamespace(
            order=o, orderStatus=SimpleNamespace(status="Filled", filled=1.0, avgFillPrice=220.4), log=[]),
    )
    broker = LiveBroker()
    with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(broker, "_get_ib", AsyncMock(return_value=ib)):
        assert await broker._dispatch_real_order("NVDA", 1, 220.5, "USD", 215.0, 230.0) == ("ibkr_0", 1.0, 220.4)
    assert [o.tif for o in placed] == ["DAY", "GTC", "GTC"]


@pytest.mark.asyncio
async def test_unfilled_entry_is_cancelled_and_partial_fill_kept():
    status = SimpleNamespace(status="Submitted", filled=0.0, avgFillPrice=0.0)
    trade = SimpleNamespace(order=SimpleNamespace(orderId=7), orderStatus=status)

    def cancel(order):
        status.status, status.filled, status.avgFillPrice = "Cancelled", 2.0, 99.5

    cancelled = []
    ib = SimpleNamespace(cancelOrder=lambda o: cancelled.append(o) or cancel(o))
    assert await LiveBroker._await_entry_fill(ib, trade, timeout=0.3) == (2.0, 99.5)
    assert cancelled == [trade.order]


def test_round_to_tick_follows_price_band():
    # Euronext rule 1874 as IBKR returns it: TTE.PA at 80.735 was rejected with Error 110
    from src.trading.live import round_to_tick
    euronext = [(0.0, 0.0001), (1.0, 0.0002), (5.0, 0.001), (10.0, 0.002), (20.0, 0.005), (50.0, 0.01), (100.0, 0.02), (200.0, 0.05)]
    assert round_to_tick(80.735, euronext) in (80.73, 80.74)
    assert round_to_tick(870.77, euronext) == 870.75
    assert round_to_tick(150.03, euronext) in (150.02, 150.04)
    assert round_to_tick(12.3456, euronext) == 12.346
    assert round_to_tick(220.5, []) == 220.5


@pytest.mark.asyncio
async def test_raised_stop_moves_the_ibkr_stop_order():
    placed = []
    contract = SimpleNamespace(symbol="NVDA", currency="USD", primaryExchange="NASDAQ")
    stop_leg = SimpleNamespace(contract=contract, order=SimpleNamespace(action="SELL", orderType="STP", auxPrice=95.0))
    tp_leg = SimpleNamespace(contract=contract, order=SimpleNamespace(action="SELL", orderType="LMT", auxPrice=0.0))
    ib = SimpleNamespace(
        isConnected=lambda: True,
        openTrades=lambda: [tp_leg, stop_leg],
        reqContractDetailsAsync=AsyncMock(return_value=[SimpleNamespace(validExchanges="SMART", marketRuleIds="26")]),
        reqMarketRuleAsync=AsyncMock(return_value=[SimpleNamespace(lowEdge=0.0, increment=0.01)]),
        placeOrder=lambda c, o: placed.append(o),
    )
    broker = LiveBroker()
    with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(broker, "_get_ib", AsyncMock(return_value=ib)):
        await broker._move_broker_stop("NVDA", "USD", 2, 101.004)
        assert placed == [stop_leg.order] and stop_leg.order.auxPrice == 101.0
        # A lone take-profit leg: no separate stop, it would sit outside the OCA group
        ib.openTrades = lambda: [tp_leg]
        await broker._move_broker_stop("NVDA", "USD", 2, 102.0)
        assert len(placed) == 1
