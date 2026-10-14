import base64
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from src.web.security import install_web_security


def _app():
    app = FastAPI()
    install_web_security(app)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.post("/api/killswitch")
    async def kill():
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PASSWORD", None)
    monkeypatch.setattr(settings, "WEB_ALLOW_PRIVATE_NETWORK_ONLY", True)
    monkeypatch.setattr(settings, "CORS_ALLOW_ORIGINS", "")


def test_lan_client_allowed_public_client_blocked():
    assert TestClient(_app(), client=("192.168.1.20", 5000)).get("/api/ping").status_code == 200
    assert TestClient(_app(), client=("127.0.0.1", 5000)).get("/api/ping").status_code == 200
    assert TestClient(_app(), client=("8.8.8.8", 5000)).get("/api/ping").status_code == 403


def test_cross_origin_write_blocked_same_origin_allowed():
    c = TestClient(_app(), client=("192.168.1.20", 5000), base_url="http://192.168.1.10:8000")
    assert c.post("/api/killswitch", headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/api/killswitch", headers={"Origin": "http://192.168.1.10:8000"}).status_code == 200
    assert c.post("/api/killswitch").status_code == 200  # scripts without Origin
    # Reads are not origin-checked and get no permissive CORS header
    r = c.get("/api/ping", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_basic_auth_required_when_password_set(monkeypatch):
    monkeypatch.setattr(settings, "WEB_PASSWORD", "s3cret")
    c = TestClient(_app(), client=("8.8.8.8", 5000))
    r = c.get("/api/ping")
    assert r.status_code == 401
    assert "Basic" in r.headers["www-authenticate"]
    bad = base64.b64encode(b"admin:wrong").decode()
    assert c.get("/api/ping", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/api/ping", headers={"Authorization": f"Basic {good}"}).status_code == 200
