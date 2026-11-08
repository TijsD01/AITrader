import pytest
from httpx import AsyncClient, ASGITransport
from config.settings import settings
from src.trading.paper import paper_broker
from src.trading.live import live_broker
from src.trading.broker import check_broker_status
from src.web.app import app

@pytest.mark.asyncio
async def test_paper_broker_connection_check():
    status = await paper_broker.check_connection()
    assert status["connected"] is True
    assert status["broker"] == "PAPER"
    assert status["status"] == "ONLINE"
    assert "checked_at" in status

@pytest.mark.asyncio
async def test_live_broker_connection_check_graceful_handling():
    # Probing live broker when host is unreachable or times out should return a clean dictionary, not raise an unhandled exception
    status = await live_broker.check_connection(force=True)
    assert isinstance(status, dict)
    assert "connected" in status
    assert "status" in status
    assert "broker" in status
    assert "message" in status
    assert status["broker"] in ("TRADING212", "ALPACA")

@pytest.mark.asyncio
async def test_api_broker_status_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/broker/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data
        assert "status" in data
        assert "broker" in data

@pytest.mark.asyncio
async def test_api_broker_ping_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/broker/ping")
        assert resp.status_code == 200
        data = resp.json()
        assert "connected" in data
        assert "status" in data
        assert "broker" in data


@pytest.mark.asyncio
async def test_cached_status_reports_current_kill_switch(monkeypatch):
    # The live status is cached for a few seconds; the kill switch shown to the user must not be stale
    import time
    monkeypatch.setattr(live_broker, "_last_connection_status", {
        "broker": "TRADING212", "connected": True, "status": "ONLINE", "message": "ok",
        "kill_switch_active": False, "live_trading_enabled": False,
    })
    monkeypatch.setattr(live_broker, "_last_connection_check", time.time())
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)

    status = await check_broker_status(probe_live=True)

    assert status["kill_switch_active"] is True
    assert status["live_trading_enabled"] is True
    assert live_broker._last_connection_status["kill_switch_active"] is False  # cache itself untouched
