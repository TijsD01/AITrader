import pytest
from unittest.mock import AsyncMock
from httpx import AsyncClient, ASGITransport
from src.web.app import app
from src.web import routes
from src.trading.broker import live_broker


async def _call(method, url, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.request(method, url, **kw)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(routes, "holdings_enabled", lambda: True)


async def test_get_holdings_empty_in_paper_mode():
    resp = await _call("GET", "/api/holdings")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_owner_mine_calls_release(monkeypatch, enabled):
    mock = AsyncMock(return_value={"success": True, "message": "NVDA is yours."})
    monkeypatch.setattr(live_broker, "release_to_user", mock)
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "mine"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "message": "NVDA is yours."}
    mock.assert_awaited_once_with("NVDA")


async def test_owner_bot_calls_take(monkeypatch, enabled):
    mock = AsyncMock(return_value={"success": True, "message": "Bot has NVDA."})
    monkeypatch.setattr(live_broker, "take_from_user", mock)
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "bot"})
    assert resp.status_code == 200
    mock.assert_awaited_once_with("NVDA")


async def test_owner_second_tap_returns_409(monkeypatch, enabled):
    mock = AsyncMock(return_value={"success": False, "message": "No bot position in NVDA."})
    monkeypatch.setattr(live_broker, "release_to_user", mock)
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "mine"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "No bot position in NVDA."}


async def test_owner_broker_exception_returns_409(monkeypatch, enabled):
    monkeypatch.setattr(live_broker, "release_to_user", AsyncMock(side_effect=ValueError("bad T212")))
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "mine"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Something went wrong. Nothing was confirmed; check the Home tab."}


async def test_owner_feature_off_returns_409():
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "mine"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Only for live Trading 212."}


async def test_owner_rejects_bad_value():
    resp = await _call("POST", "/api/holdings/NVDA/owner", json={"owner": "x"})
    assert resp.status_code == 422
