import datetime

from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from src.db import session as db_session
from src.db.models import PortfolioSnapshot

VALUES = (91001.11, 91002.22, 91003.33, 91004.44)


async def test_history_returns_recent_paper_snapshots_in_order():
    from src.web.app import app
    now = datetime.datetime.utcnow()
    rows = [
        (VALUES[0], now - datetime.timedelta(days=40), False),  # older than the window
        (VALUES[1], now - datetime.timedelta(hours=2), False),
        (VALUES[2], now - datetime.timedelta(hours=1), False),
        (VALUES[3], now - datetime.timedelta(hours=1), True),   # live, not shown in paper mode
    ]
    async with db_session.AsyncSessionLocal() as s:
        for value, ts, live in rows:
            s.add(PortfolioSnapshot(cash_eur=value, equity_eur=0.0, total_value_eur=value, is_live=live, timestamp=ts))
        await s.commit()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            data = (await ac.get("/api/portfolio/history?days=30")).json()
        ours = [v for _, v in data if v in VALUES]
        assert ours == [VALUES[1], VALUES[2]]
        assert all(isinstance(t, str) for t, _ in data)
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.total_value_eur.in_(VALUES)))
            await s.commit()
