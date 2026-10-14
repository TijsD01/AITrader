import datetime
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from src.db import session as db_session
from src.db.models import AnalysisReport


@pytest.mark.asyncio
async def test_recommendations_show_only_latest_report_per_ticker():
    from src.web.app import app
    now = datetime.datetime.utcnow()
    rows = [
        ("DUPA", "HOLD", now - datetime.timedelta(hours=3)),
        ("DUPB", "BUY", now - datetime.timedelta(hours=2)),
        ("DUPA", "BUY", now - datetime.timedelta(hours=1)),
    ]
    async with db_session.AsyncSessionLocal() as session:
        for ticker, action, ts in rows:
            session.add(AnalysisReport(ticker=ticker, action=action, consensus_score=50.0,
                                       summary="test", created_at=ts))
        await session.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            data = (await ac.get("/api/recommendations")).json()
        ours = [r for r in data if r["ticker"] in ("DUPA", "DUPB")]
        assert [r["ticker"] for r in ours] == ["DUPA", "DUPB"]
        assert ours[0]["action"] == "BUY"
    finally:
        async with db_session.AsyncSessionLocal() as session:
            await session.execute(delete(AnalysisReport).where(AnalysisReport.ticker.in_(["DUPA", "DUPB"])))
            await session.commit()
