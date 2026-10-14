import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

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
        assert "ibkr_host" in initial

        # 2. Update settings via POST
        test_payload = {
            "auto_buy_enabled": False,
            "min_consensus_score": 77,
            "position_size_pct": 0.25,
            "max_positions": 5,
            "enforce_market_hours": False,
            "ibkr_host": "172.17.0.2",
            "ibkr_port": 4003,
            "ibkr_client_id": 99,
            "ibkr_account_id": "TEST_ACC_123"
        }
        post_res = await ac.post("/api/settings", json=test_payload)
        assert post_res.status_code == 200
        assert post_res.json()["success"] is True

        # 3. Verify in-memory settings singleton was updated
        assert settings.AUTO_BUY_ENABLED is False
        assert settings.MIN_CONSENSUS_SCORE == 77
        assert abs(settings.POSITION_SIZE_PCT - 0.25) < 1e-4
        assert settings.MAX_POSITIONS == 5
        assert settings.ENFORCE_MARKET_HOURS is False
        assert settings.IBKR_HOST == "172.17.0.2"
        assert settings.IBKR_PORT == 4003
        assert settings.IBKR_CLIENT_ID == 99
        assert settings.IBKR_ACCOUNT_ID == "TEST_ACC_123"

        # 4. Verify database SystemConfig contains the persisted records
        async with AsyncSessionLocal() as session:
            db_res = await session.execute(select(SystemConfig))
            configs = {c.key: c.value for c in db_res.scalars().all()}
            assert configs["AUTO_BUY_ENABLED"] == "false"
            assert configs["MIN_CONSENSUS_SCORE"] == "77"
            assert float(configs["POSITION_SIZE_PCT"]) == 0.25
            assert configs["MAX_POSITIONS"] == "5"
            assert configs["ENFORCE_MARKET_HOURS"] == "false"
            assert configs["IBKR_HOST"] == "172.17.0.2"
            assert configs["IBKR_PORT"] == "4003"
            assert configs["IBKR_CLIENT_ID"] == "99"
            assert configs["IBKR_ACCOUNT_ID"] == "TEST_ACC_123"

        # 5. Verify GET /api/settings returns the newly updated values
        get_after = await ac.get("/api/settings")
        assert get_after.status_code == 200
        after_data = get_after.json()
        assert after_data["auto_buy_enabled"] is False
        assert after_data["min_consensus_score"] == 77
        assert abs(after_data["position_size_pct"] - 0.25) < 1e-4
        assert after_data["max_positions"] == 5
        assert after_data["enforce_market_hours"] is False
        assert after_data["ibkr_host"] == "172.17.0.2"
        assert after_data["ibkr_port"] == 4003
        assert after_data["ibkr_client_id"] == 99
        assert after_data["ibkr_account_id"] == "TEST_ACC_123"

        # 6. Revert back to original/sensible defaults
        revert_payload = {
            "auto_buy_enabled": True,
            "min_consensus_score": 70,
            "position_size_pct": initial.get("position_size_pct", 0.08),
            "enforce_market_hours": True,
            "ibkr_client_id": 1,
            "ibkr_account_id": ""
        }
        revert_res = await ac.post("/api/settings", json=revert_payload)
        assert revert_res.status_code == 200


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
