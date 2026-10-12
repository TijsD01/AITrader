import pytest
from httpx import AsyncClient, ASGITransport
from config.settings import settings
from src.trading.risk import get_effective_deployable_cash
from src.trading.paper import paper_broker
from src.trading.live import live_broker
from src.db.session import init_db
from src.web.app import app

@pytest.mark.asyncio
async def test_get_effective_deployable_cash_helper():
    # 1. 100% usage (default, no reserve)
    usable, reserve = get_effective_deployable_cash(10000.0, 100.0, None)
    assert usable == 10000.0
    assert reserve == 0.0

    # 2. 75% usage
    usable, reserve = get_effective_deployable_cash(10000.0, 75.0, None)
    assert usable == 7500.0
    assert reserve == 2500.0

    # 3. 80% usage with absolute euro cap lower than percentage
    usable, reserve = get_effective_deployable_cash(10000.0, 80.0, 5000.0)
    assert usable == 5000.0
    assert reserve == 5000.0

    # 4. 80% usage with absolute euro cap higher than percentage
    usable, reserve = get_effective_deployable_cash(10000.0, 80.0, 9000.0)
    assert usable == 8000.0
    assert reserve == 2000.0

    # 5. Zero / negative cash edge cases
    usable, reserve = get_effective_deployable_cash(0.0, 50.0, 1000.0)
    assert usable == 0.0
    assert reserve == 0.0

@pytest.mark.asyncio
async def test_broker_portfolio_summary_capital_limits():
    orig_pct = settings.MAX_CAPITAL_USAGE_PCT
    orig_cap = settings.MAX_DEPLOYABLE_CAPITAL_EUR
    try:
        await init_db()
        settings.MAX_CAPITAL_USAGE_PCT = 70.0
        settings.MAX_DEPLOYABLE_CAPITAL_EUR = 6000.0

        # Paper Broker
        paper_summary = await paper_broker.get_portfolio_summary()
        assert "usable_capital_eur" in paper_summary
        assert "protected_reserve_eur" in paper_summary
        assert "total_account_cash_eur" in paper_summary
        assert paper_summary["max_capital_usage_pct"] == 70.0
        assert paper_summary["max_deployable_capital_eur"] == 6000.0
        assert paper_summary["usable_capital_eur"] + paper_summary["protected_reserve_eur"] == paper_summary["cash_eur"]

        # Live Broker
        live_summary = await live_broker.get_portfolio_summary()
        assert "usable_capital_eur" in live_summary
        assert "protected_reserve_eur" in live_summary
        assert live_summary["max_capital_usage_pct"] == 70.0
        assert live_summary["max_deployable_capital_eur"] == 6000.0
    finally:
        settings.MAX_CAPITAL_USAGE_PCT = orig_pct
        settings.MAX_DEPLOYABLE_CAPITAL_EUR = orig_cap

@pytest.mark.asyncio
async def test_api_settings_capital_limits():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Update settings with 65% capital limit and 4500 EUR cap
        post_res = await client.post("/api/settings", json={
            "max_capital_usage_pct": 65.0,
            "max_deployable_capital_eur": 4500.0
        })
        assert post_res.status_code == 200
        assert post_res.json()["success"] is True

        # 2. Fetch settings and verify persisted values
        get_res = await client.get("/api/settings")
        assert get_res.status_code == 200
        data = get_res.json()
        assert data["max_capital_usage_pct"] == 65.0
        assert data["max_deployable_capital_eur"] == 4500.0

        # 3. Clear the cap
        post_clear = await client.post("/api/settings", json={
            "clear_max_deployable_capital": True
        })
        assert post_clear.status_code == 200

        get_res2 = await client.get("/api/settings")
        assert get_res2.status_code == 200
        data2 = get_res2.json()
        assert data2["max_deployable_capital_eur"] is None

        # 4. Reset back to 100%
        await client.post("/api/settings", json={
            "max_capital_usage_pct": 100.0,
            "clear_max_deployable_capital": True
        })

@pytest.mark.asyncio
async def test_paper_broker_trigger_and_buy_cap_enforcement():
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    orig_pct = settings.MAX_CAPITAL_USAGE_PCT
    orig_cap = settings.MAX_DEPLOYABLE_CAPITAL_EUR
    orig_budget_util = settings.FULL_BUDGET_UTILIZATION
    try:
        settings.ENFORCE_MARKET_HOURS = False
        settings.FULL_BUDGET_UTILIZATION = False
        await init_db()

        summary = await paper_broker.get_portfolio_summary()
        cash = summary["cash_eur"]

        # Limit to 20% capital usage (e.g. if cash is €10k, usable is €2k, reserve is €8k)
        settings.MAX_CAPITAL_USAGE_PCT = 20.0
        settings.MAX_DEPLOYABLE_CAPITAL_EUR = None

        usable, reserve = get_effective_deployable_cash(cash, 20.0, None)
        assert usable < cash

        # Attempt to arm a trigger that requests more than usable cash
        trig_res = await paper_broker.create_price_trigger(
            ticker="BMW.DE",
            name="BMW",
            currency="EUR",
            trigger_type="LIMIT_PULLBACK",
            target_price=90.0,
            current_price_at_creation=100.0,
            stop_loss=85.0,
            take_profit=110.0,
            allocation_eur=usable + 500.0,
            consensus_score=85,
            ai_rationale="Test limit trigger"
        )
        assert trig_res["success"] is False
        assert "insufficient available cash" in trig_res["message"].lower()
    finally:
        settings.ENFORCE_MARKET_HOURS = orig_enforce
        settings.MAX_CAPITAL_USAGE_PCT = orig_pct
        settings.MAX_DEPLOYABLE_CAPITAL_EUR = orig_cap
        settings.FULL_BUDGET_UTILIZATION = orig_budget_util
