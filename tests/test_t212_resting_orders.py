"""Resting-order groundwork: stop-limit client call and the feature flag."""
import json

import httpx
from sqlalchemy import delete, desc, select

from config.settings import settings
from src.db.models import PortfolioSnapshot, Position, Trade
from src.db.session import AsyncSessionLocal
from src.trading import trading212
from src.trading.fees import calculate_broker_fee
from src.trading.live import LiveBroker
from tests.test_trading212_broker import _cleanup, clean_snapshots  # noqa: F401


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
