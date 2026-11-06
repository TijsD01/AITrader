import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from config.settings import settings
from src.web.app import app
from src.db.session import AsyncSessionLocal
from src.db.models import SystemConfig

@pytest.mark.asyncio
async def test_settings_persistence_cycle():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Read initial settings
        get_res = await ac.get("/api/settings")
        assert get_res.status_code == 200
        initial = get_res.json()
        assert "position_size_pct" in initial
        assert "ibkr_host" not in initial

        # 2. Update settings via POST
        test_payload = {
            "auto_buy_enabled": False,
            "min_consensus_score": 77,
            "position_size_pct": 0.12,
            "max_positions": 5,
            "enforce_market_hours": False
        }
        post_res = await ac.post("/api/settings", json=test_payload)
        assert post_res.status_code == 200
        assert post_res.json()["success"] is True

        # 3. Verify in-memory settings singleton was updated
        assert settings.AUTO_BUY_ENABLED is False
        assert settings.MIN_CONSENSUS_SCORE == 77
        assert abs(settings.POSITION_SIZE_PCT - 0.12) < 1e-4
        assert settings.MAX_POSITIONS == 5
        assert settings.ENFORCE_MARKET_HOURS is False

        # 4. Verify database SystemConfig contains the persisted records
        async with AsyncSessionLocal() as session:
            db_res = await session.execute(select(SystemConfig))
            configs = {c.key: c.value for c in db_res.scalars().all()}
            assert configs["AUTO_BUY_ENABLED"] == "false"
            assert configs["MIN_CONSENSUS_SCORE"] == "77"
            assert float(configs["POSITION_SIZE_PCT"]) == 0.12
            assert configs["MAX_POSITIONS"] == "5"
            assert configs["ENFORCE_MARKET_HOURS"] == "false"

        # 5. Verify GET /api/settings returns the newly updated values
        get_after = await ac.get("/api/settings")
        assert get_after.status_code == 200
        after_data = get_after.json()
        assert after_data["auto_buy_enabled"] is False
        assert after_data["min_consensus_score"] == 77
        assert abs(after_data["position_size_pct"] - 0.12) < 1e-4
        assert after_data["max_positions"] == 5
        assert after_data["enforce_market_hours"] is False

        # 6. Revert back to original/sensible defaults
        revert_payload = {
            "auto_buy_enabled": True,
            "min_consensus_score": 70,
            "position_size_pct": initial.get("position_size_pct", 0.08),
            "enforce_market_hours": True
        }
        revert_res = await ac.post("/api/settings", json=revert_payload)
        assert revert_res.status_code == 200


@pytest.mark.asyncio
async def test_score_auto_tune_and_trigger_score_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "SCORE_AUTO_TUNE", True)
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", settings.TRIGGER_MIN_SCORE)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(SystemConfig).where(SystemConfig.key.in_(["SCORE_AUTO_TUNE", "TRIGGER_MIN_SCORE"])))).scalars().all()
        saved = {r.key: r.value for r in rows}
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            post_res = await ac.post("/api/settings", json={"score_auto_tune": False, "trigger_min_score": 75})
            assert post_res.status_code == 200
            assert settings.SCORE_AUTO_TUNE is False
            data = (await ac.get("/api/settings")).json()
            assert data["score_auto_tune"] is False
            assert data["trigger_min_score"] == 75
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(SystemConfig).where(SystemConfig.key.in_(["SCORE_AUTO_TUNE", "TRIGGER_MIN_SCORE"])))
            session.add_all([SystemConfig(key=k, value=v) for k, v in saved.items()])
            await session.commit()


def test_update_env_file_strips_newlines(tmp_path):
    from config.settings import update_env_file
    env = tmp_path / ".env"
    env.write_text("FOO=1\n", encoding="utf-8")
    update_env_file({"FOO": "bar\nLIVE_TRADING_ENABLED=true", "BAZ\r\nEVIL": "x\r\ny"}, env_path=env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert "LIVE_TRADING_ENABLED=true" not in lines
    assert "FOO=barLIVE_TRADING_ENABLED=true" in lines
    assert "BAZEVIL=xy" in lines
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_kill_switch_persists_to_system_config():
    from src.db.session import set_kill_switch
    original = settings.KILL_SWITCH_ACTIVE
    try:
        for state in (True, False):
            assert await set_kill_switch(state) is state
            assert settings.KILL_SWITCH_ACTIVE is state
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(SystemConfig).where(SystemConfig.key == "KILL_SWITCH_ACTIVE"))
                assert res.scalars().first().value == str(state).lower()
    finally:
        await set_kill_switch(original)


def test_unknown_live_broker_fails_at_startup():
    from pydantic import ValidationError
    from config.settings import Settings
    with pytest.raises(ValidationError, match="not supported"):
        Settings(_env_file=None, LIVE_BROKER="ibkr")
    assert Settings(_env_file=None, LIVE_BROKER="Trading212").LIVE_BROKER == "Trading212"
