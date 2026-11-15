import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from src.web import routes
from src.web.security import install_web_security

LAN = ("192.168.1.20", 5000)
LOOPBACK = ("127.0.0.1", 5000)


def _app():
    app = FastAPI()
    install_web_security(app)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.post("/api/killswitch")
    async def kill(active: bool):
        return {"ok": True}

    @app.post("/api/buy")
    async def buy():
        return {"ok": True}

    return app


def _settings_app():
    """The real settings route behind the real middleware, without the app's scheduler."""
    app = FastAPI()
    install_web_security(app)
    app.include_router(routes.router)
    return app


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PASSWORD", None)
    monkeypatch.setattr(settings, "WEB_ALLOW_PRIVATE_NETWORK_ONLY", True)
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "")
    monkeypatch.setattr(settings, "WEB_ALLOWED_HOSTS", "")
    monkeypatch.setattr(settings, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)


def _arm(monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)


# --- 1a: arming live trading needs loopback or a password ---

@pytest.mark.parametrize("payload", [
    {"trading_mode": "LIVE"},
    {"trading_mode": " live "},
    {"live_trading_enabled": True},
    {"trading_mode": "PAPER", "live_trading_enabled": True},
])
def test_lan_client_without_password_cannot_arm_live(payload, monkeypatch):
    written = []
    monkeypatch.setattr(routes, "update_env_file", written.append)
    # A harmless field rides along: nothing of a refused request may be applied
    before = settings.AUTO_BUY_ENABLED
    r = TestClient(_settings_app(), client=LAN).post("/api/settings", json={**payload, "auto_buy_enabled": not before})
    assert r.status_code == 403
    assert "WEB_PASSWORD" in r.json()["detail"]
    assert settings.TRADING_MODE == "PAPER" and settings.LIVE_TRADING_ENABLED is False
    assert settings.AUTO_BUY_ENABLED is before
    assert written == []


def test_lan_client_without_password_can_still_disarm_and_use_paper(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)  # PAPER mode, flag left on
    r = TestClient(_settings_app(), client=LAN).post(
        "/api/settings", json={"trading_mode": "PAPER", "live_trading_enabled": False})
    assert r.status_code == 200
    assert settings.LIVE_TRADING_ENABLED is False


@pytest.fixture
def _no_save(monkeypatch):
    """Stop the route right after the arming check so no test really arms live trading."""
    def boom():
        raise RuntimeError("reached save")
    monkeypatch.setattr(routes, "get_active_broker", boom)


def test_loopback_and_authenticated_clients_can_arm_live(monkeypatch, _no_save):
    with pytest.raises(RuntimeError, match="reached save"):
        TestClient(_settings_app(), client=LOOPBACK).post("/api/settings", json={"trading_mode": "LIVE"})
    with pytest.raises(RuntimeError, match="reached save"):
        TestClient(_settings_app(), client=("::1", 5000)).post("/api/settings", json={"trading_mode": "LIVE"})
    monkeypatch.setattr(settings, "WEB_PASSWORD", "s3cret")
    good = base64.b64encode(b"admin:s3cret").decode()
    with pytest.raises(RuntimeError, match="reached save"):
        TestClient(_settings_app(), client=LAN).post(
            "/api/settings", json={"trading_mode": "LIVE"}, headers={"Authorization": f"Basic {good}"})


# --- 1b: no remote control of an armed live bot without a password ---

def test_lan_client_without_password_is_read_only_while_live_is_armed(monkeypatch):
    _arm(monkeypatch)
    c = TestClient(_app(), client=LAN)
    assert c.get("/api/ping").status_code == 200
    r = c.post("/api/buy")
    assert r.status_code == 403
    assert "set WEB_PASSWORD to control the bot remotely in LIVE mode" in r.text
    # The emergency stop stays reachable, turning it off does not
    assert c.post("/api/killswitch?active=true").status_code == 200
    assert c.post("/api/killswitch?active=false").status_code == 403
    assert c.post("/api/killswitch?active=false&active=true").status_code == 403
    assert c.post("/api/killswitch").status_code == 403


def test_loopback_and_authenticated_clients_control_an_armed_live_bot(monkeypatch):
    _arm(monkeypatch)
    assert TestClient(_app(), client=LOOPBACK).post("/api/buy").status_code == 200
    assert TestClient(_app(), client=("::ffff:127.0.0.1", 5000)).post("/api/buy").status_code == 200
    monkeypatch.setattr(settings, "WEB_PASSWORD", "s3cret")
    good = base64.b64encode(b"admin:s3cret").decode()
    assert TestClient(_app(), client=LAN).post("/api/buy", headers={"Authorization": f"Basic {good}"}).status_code == 200


@pytest.mark.parametrize("mode,enabled", [("PAPER", True), ("LIVE", False), ("PAPER", False)])
def test_lan_client_unaffected_while_live_is_not_armed(mode, enabled, monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MODE", mode)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", enabled)
    assert TestClient(_app(), client=LAN).post("/api/buy").status_code == 200


# --- 2: DNS rebinding (Host header check) ---

@pytest.mark.parametrize("host", [
    "testserver", "localhost:8000", "192.168.1.10:8000", "[::1]:8000", "[fd7a:115c:a1e0::1]",
    "raspberrypi", "raspberrypi.local", "bot.lan:8000", "pi.home.arpa", "pi.tail1234.ts.net", "RaspberryPi.LOCAL.",
])
def test_local_host_names_allowed(host):
    assert TestClient(_app(), client=LAN).get("/api/ping", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "evil.example:8000", "192.168.1.10.evil.example", "local.evil.example",
    "ts.net.evil.example", "notlocal.example.",
])
def test_foreign_host_names_rejected(host):
    for client in (LAN, LOOPBACK):
        r = TestClient(_app(), client=client).get("/api/ping", headers={"Host": host})
        assert r.status_code == 400
        assert "WEB_ALLOWED_HOSTS" in r.text


def test_rebound_page_cannot_write_even_with_matching_origin():
    # After a rebind, Origin and Host are both the attacker's name: the same-origin check alone passes
    c = TestClient(_app(), client=LAN)
    r = c.post("/api/buy", headers={"Host": "evil.example", "Origin": "http://evil.example"})
    assert r.status_code == 400


def test_configured_and_cors_hosts_allowed(monkeypatch):
    c = TestClient(_app(), client=LAN)
    monkeypatch.setattr(settings, "WEB_ALLOWED_HOSTS", " bot.example.com , Other.Example ")
    assert c.get("/api/ping", headers={"Host": "bot.example.com:8000"}).status_code == 200
    assert c.get("/api/ping", headers={"Host": "other.example"}).status_code == 200
    assert c.get("/api/ping", headers={"Host": "sub.bot.example.com"}).status_code == 400
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "https://dash.example.org:8443")
    assert c.get("/api/ping", headers={"Host": "dash.example.org"}).status_code == 200


def test_password_setup_serves_any_host_name(monkeypatch):
    # A remote setup behind its own domain must keep working; a rebound page has no credentials to send
    monkeypatch.setattr(settings, "WEB_PASSWORD", "s3cret")
    good = base64.b64encode(b"admin:s3cret").decode()
    c = TestClient(_app(), client=LAN)
    assert c.get("/api/ping", headers={"Host": "bot.example.com", "Authorization": f"Basic {good}"}).status_code == 200
    assert c.get("/api/ping", headers={"Host": "bot.example.com"}).status_code == 401


def test_allowed_host_entry_may_carry_a_port(monkeypatch):
    monkeypatch.setattr(settings, "WEB_ALLOWED_HOSTS", "bot.example.com:8000")
    assert TestClient(_app(), client=LAN).get("/api/ping", headers={"Host": "bot.example.com:8000"}).status_code == 200


# --- 3: score thresholds are bounded ---

@pytest.mark.parametrize("field", ["min_consensus_score", "trigger_min_score"])
@pytest.mark.parametrize("value", [0, 49, 101, -5])
def test_score_settings_out_of_range_rejected(field, value, monkeypatch):
    written = []
    monkeypatch.setattr(routes, "update_env_file", written.append)
    before = (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE)
    r = TestClient(_settings_app()).post("/api/settings", json={field: value})
    assert r.status_code == 422
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == before
    assert written == []


@pytest.mark.parametrize("value", [50, 100])
def test_score_settings_bounds_accepted(value):
    for field in ("min_consensus_score", "trigger_min_score"):
        assert routes.SettingsUpdateRequest(**{field: value})


@pytest.mark.parametrize("payload", [{"telegram_chat_id": "666"}, {"telegram_bot_token": "x"}, {"gemini_api_key": "x"}])
def test_lan_client_without_password_cannot_change_credentials_or_telegram_chat(payload, monkeypatch):
    monkeypatch.setattr(settings, "WEB_PASSWORD", None)
    before = settings.TELEGRAM_CHAT_ID
    r = TestClient(_settings_app(), client=LAN).post("/api/settings", json=payload)
    assert r.status_code == 403 and "WEB_PASSWORD" in r.text
    assert settings.TELEGRAM_CHAT_ID == before


def test_forwarded_loopback_request_is_not_trusted_to_arm_live(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PASSWORD", None)
    r = TestClient(_settings_app(), client=LOOPBACK).post(
        "/api/settings", json={"trading_mode": "LIVE"}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403
    assert settings.TRADING_MODE == "PAPER"


def test_blocked_write_explains_itself_in_the_json_detail_the_dashboard_shows(monkeypatch):
    # The dashboard toasts data.detail; a plain-text block showed only "Analysis failed"
    monkeypatch.setattr(settings, "WEB_PASSWORD", None)
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    r = TestClient(_app(), client=LAN).post("/api/analyze", json={"ticker": "ASML.AS"})
    assert r.status_code == 403 and "WEB_PASSWORD" in r.json()["detail"]
