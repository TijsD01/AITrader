"""Committee runs are background jobs: POST answers 202 with a job id, GET /api/jobs/{id} returns the outcome."""
import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from sqlalchemy import delete

from src.db.models import OrderTrigger
from src.db.session import init_db, AsyncSessionLocal
from src.trading.paper import paper_broker
from src.web.app import app


def _report(action="BUY", score=86):
    return {
        "ticker": "JOBX", "action": action, "consensus_score": score, "entry_trigger_price": 98.0,
        "trigger_type": "LIMIT_PULLBACK", "trigger_rationale": "r", "summary": "s", "debate_transcript": "d",
        "technical": {"analysis_summary": "t"}, "fundamental": {"analysis_summary": "f"},
        "sentiment": {"sentiment_summary": "s"},
        "risk": {"approved": True, "take_profit_price": 110.0, "stop_loss_price": 95.0, "recommended_size_pct": 0.05,
                 "risk_summary": "ok"},
    }


STOCK = {"ticker": "JOBX", "name": "Job X", "price": 100.0, "currency": "EUR", "quote_type": "EQUITY",
         "technicals": {"rsi14": 55, "volume_surge_ratio": 1.2, "atr14": 2.0}, "fundamentals": {}}


async def _wait(client, job_id):
    for _ in range(200):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["status"] == "done":
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def _run(report, calls):
    """Runs `calls(client, committee_mock)` with the committee, market data and portfolio mocked."""
    await init_db()
    committee = AsyncMock(return_value=report)
    portfolio = {"available_cash_eur": 5000.0, "total_value_eur": 10000.0, "open_positions_count": 0}
    with patch("src.web.routes.market_data_provider.get_stock_data", MagicMock(return_value=STOCK)), \
         patch("src.web.routes.investment_committee.evaluate_ticker", committee), \
         patch.object(paper_broker, "get_portfolio_summary", AsyncMock(return_value=portfolio)), \
         patch("src.web.routes.get_trigger_min_score", AsyncMock(return_value=80)), \
         patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")), \
         patch("src.telegram.bot.telegram_bot.notify_trigger_armed", AsyncMock()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await calls(client, committee)


@pytest.mark.asyncio
async def test_analyze_runs_as_a_job():
    async def calls(client, committee):
        resp = await client.post("/api/analyze", json={"ticker": "jobx"})
        assert resp.status_code == 202
        job = await _wait(client, resp.json()["job_id"])
        assert (job["kind"], job["ticker"], job["status_code"]) == ("analyze", "JOBX", 200)
        assert job["body"]["action"] == "BUY"
    # Persisting the report is a no-op
    session = MagicMock()
    db = session.return_value.__aenter__.return_value
    db.add, db.commit = MagicMock(), AsyncMock()
    with patch("src.web.routes.AsyncSessionLocal", session):
        await _run(_report(), calls)


@pytest.mark.asyncio
async def test_blocked_arm_job_carries_the_gate_detail():
    async def calls(client, committee):
        resp = await client.post("/api/triggers/from-ai", json={"ticker": "JOBX", "allocation_eur": 500.0})
        assert resp.status_code == 202
        job = await _wait(client, resp.json()["job_id"])
        assert job["status_code"] == 422
        assert job["body"]["detail"]["overridable"] is True
    await _run(_report(action="HOLD", score=72), calls)


@pytest.mark.asyncio
async def test_unknown_job_is_404():
    async def calls(client, committee):
        assert (await client.get("/api/jobs/nope")).status_code == 404
    await _run(_report(), calls)


@pytest.mark.asyncio
async def test_forced_arm_reuses_the_report_of_the_blocked_run():
    async def calls(client, committee):
        first = await client.post("/api/triggers/from-ai", json={"ticker": "JOBX", "allocation_eur": 500.0})
        first_id = first.json()["job_id"]
        assert (await _wait(client, first_id))["status_code"] == 422
        forced = await client.post("/api/triggers/from-ai", json={
            "ticker": "JOBX", "allocation_eur": 500.0, "force": True, "report_job_id": first_id})
        job = await _wait(client, forced.json()["job_id"])
        assert job["status_code"] != 422 and job["body"].get("gate", {}).get("code") != "BLOCKED"
        assert committee.await_count == 1
        async with AsyncSessionLocal() as session:  # the forced run armed a real paper trigger
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "JOBX"))
            await session.commit()
    await _run(_report(action="HOLD", score=72), calls)


@pytest.mark.asyncio
async def test_opportunity_scan_skips_unaffordable_candidates():
    """Expensive scanner picks must not eat a committee run: only stocks one share of which fits get evaluated."""
    from src.scheduler.jobs import trading_scheduler
    await init_db()
    cands = [{"ticker": t, "price": p, "currency": "EUR"} for t, p in
             [("BIGX", 900.0), ("CHEAP1", 20.0), ("CHEAP2", 30.0), ("CHEAP3", 40.0), ("CHEAP4", 50.0)]]
    portfolio = {"available_cash_eur": 400.0, "total_value_eur": 500.0, "open_positions_count": 0}
    fetch = MagicMock(return_value=STOCK)
    committee = AsyncMock(return_value=_report(action="HOLD", score=50))
    status = {"eu_market": {"is_open": True}, "us_market": {"is_open": False}}
    with patch("src.data.market_hours.get_market_statuses", return_value=status), \
         patch("src.scheduler.jobs.settings.WHOLE_SHARES_ONLY", True), \
         patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
         patch("src.scheduler.jobs.market_scanner.scan_universe", MagicMock(return_value=cands)), \
         patch("src.scheduler.jobs.market_data_provider.get_stock_data", fetch), \
         patch("src.scheduler.jobs.investment_committee.evaluate_ticker", committee), \
         patch("src.scheduler.jobs.shadow_portfolios.follow_report", AsyncMock()), \
         patch.object(paper_broker, "get_portfolio_summary", AsyncMock(return_value=portfolio)):
        await trading_scheduler.scan_market_opportunities()
    assert committee.await_count == 3
    assert "BIGX" not in [c.args[0] for c in fetch.call_args_list]
