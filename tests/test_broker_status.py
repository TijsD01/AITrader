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
    assert status["broker"] in ("IBKR", "ALPACA")

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
async def test_live_broker_persistent_client_reuse_and_disconnect():
    from unittest.mock import MagicMock
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    mock_ib.managedAccounts.return_value = ["U1234567"]
    mock_ib.client.serverVersion.return_value = 176

    live_broker._ib = mock_ib
    try:
        # 1. _get_ib should reuse existing connected client
        retrieved = await live_broker._get_ib()
        assert retrieved is mock_ib

        # 2. check_connection should report ONLINE using persistent client
        status = await live_broker.check_connection(force=True)
        assert status["connected"] is True
        assert status["status"] == "ONLINE"
        assert status["server_version"] == 176
        assert status["accounts"] == ["U1234567"]

        # 3. Disconnect event should update status to OFFLINE
        live_broker._on_ib_disconnected()
        assert live_broker._last_connection_status["connected"] is False
        assert live_broker._last_connection_status["status"] == "OFFLINE"
    finally:
        await live_broker.disconnect()
        assert live_broker._ib is None
