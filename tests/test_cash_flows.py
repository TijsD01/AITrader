import datetime
import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from src.db import session as db_session
from src.db.models import CashFlow, PortfolioSnapshot, SystemConfig
from src.trading.cash_flows import detect_flow, scan_live_cash_flows, net_cash_flows_eur, SCAN_KEY


def _snap(cash, total):
    return PortfolioSnapshot(cash_eur=cash, equity_eur=total - cash, total_value_eur=total, is_live=True)


def test_detect_flow_tells_deposits_from_trades():
    base = _snap(1000.0, 5000.0)
    # Deposit: cash and total rise together
    assert detect_flow(base, _snap(2000.0, 6000.0), min_eur=100) == 1000.0
    # Withdrawal
    assert detect_flow(base, _snap(500.0, 4500.0), min_eur=100) == -500.0
    # Deposit on a day the shares fell: still a deposit of the cash amount
    assert detect_flow(base, _snap(2000.0, 5700.0), min_eur=100) == 1000.0
    # Buy fill: cash swapped for shares, total flat
    assert detect_flow(base, _snap(500.0, 4998.0), min_eur=100) is None
    # Broker stop sold a position the bot never recorded: cash up, total flat
    assert detect_flow(base, _snap(1800.0, 4990.0), min_eur=100) is None
    # Market move only
    assert detect_flow(base, _snap(1000.0, 5400.0), min_eur=100) is None
    # Small dividend stays gain
    assert detect_flow(base, _snap(1020.0, 5020.0), min_eur=100) is None


async def _fresh_live_history():
    """Isolates this test from live snapshots other tests left behind: starts the scan at a known baseline."""
    async with db_session.AsyncSessionLocal() as session:
        await session.execute(delete(CashFlow))
        baseline = _snap(1000.0, 1000.0)
        session.add(baseline)
        await session.flush()
        max_id = baseline.id
        cfg = await session.get(SystemConfig, SCAN_KEY)
        if cfg:
            cfg.value = str(max_id)
        else:
            session.add(SystemConfig(key=SCAN_KEY, value=str(max_id)))
        await session.commit()
        return max_id


async def _cleanup(from_id):
    async with db_session.AsyncSessionLocal() as session:
        await session.execute(delete(CashFlow))
        await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id >= from_id))
        await session.commit()


@pytest.mark.asyncio
async def test_deposit_is_not_counted_as_live_gain():
    from src.trading.live import live_broker
    first_id = await _fresh_live_history()
    try:
        await _check_deposit_flow(live_broker)
    finally:
        await _cleanup(first_id)


async def _check_deposit_flow(live_broker):
    t0 = datetime.datetime.utcnow() - datetime.timedelta(days=2)
    async with db_session.AsyncSessionLocal() as session:
        for i, (cash, total) in enumerate([(1000.0, 1000.0), (3000.0, 3000.0), (3000.0, 3050.0)]):
            s = _snap(cash, total)
            s.timestamp = t0 + datetime.timedelta(minutes=10 * i)
            session.add(s)
        await session.commit()

    assert await scan_live_cash_flows() == 1
    # A second scan finds nothing new
    assert await scan_live_cash_flows() == 0
    assert await net_cash_flows_eur(is_live=True) == 2000.0
    assert await net_cash_flows_eur(is_live=True, since=t0 + datetime.timedelta(minutes=15)) == 0.0

    with patch.object(live_broker, "refresh_positions_pnl", AsyncMock()), \
         patch.object(live_broker, "adjust_triggers_dynamically", AsyncMock()):
        summary = await live_broker.get_portfolio_summary()
    assert summary["net_deposits_eur"] == 2000.0
    assert summary["invested_capital_eur"] == round(summary["initial_capital_eur"] + 2000.0, 2)
    assert summary["total_gain_eur"] == round(summary["total_value_eur"] - summary["invested_capital_eur"], 2)
    expected_pct = round(summary["total_gain_eur"] / summary["invested_capital_eur"] * 100, 2)
    assert summary["total_return_pct"] == expected_pct


@pytest.mark.asyncio
async def test_cash_flow_api_add_and_remove():
    from src.web.app import app
    first_id = await _fresh_live_history()
    try:
        await _check_api(ASGITransport(app=app))
    finally:
        await _cleanup(first_id)


async def _check_api(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/cash-flows", json={"amount_eur": 250.0, "note": "Missed deposit"})
        assert r.status_code == 200
        flow_id = r.json()["id"]

        r = await ac.get("/api/cash-flows")
        data = r.json()
        assert data["net_deposits_eur"] == 250.0
        assert data["flows"][0]["source"] == "manual"

        assert (await ac.post("/api/cash-flows", json={"amount_eur": 0})).status_code == 400
        assert (await ac.delete(f"/api/cash-flows/{flow_id}")).status_code == 200
        assert (await ac.delete(f"/api/cash-flows/{flow_id}")).status_code == 404
        assert (await ac.get("/api/cash-flows")).json()["net_deposits_eur"] == 0.0
