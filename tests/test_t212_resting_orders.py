"""Resting-order groundwork: stop-limit client call and the feature flag."""
import json

import httpx

from config.settings import settings
from src.trading import trading212
from src.trading.live import LiveBroker


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
