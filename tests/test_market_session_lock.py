import asyncio
from unittest.mock import patch

from httpx import AsyncClient, ASGITransport

from src.scheduler.jobs import TradingScheduler, trading_scheduler
from src.web.app import app


async def test_market_sessions_never_overlap():
    """A double click, cron plus manual, or Telegram must not run two sessions at once."""
    sched = TradingScheduler()
    running, overlaps, order = 0, 0, []

    async def fake_session(market, session, target_chat_id=None):
        nonlocal running, overlaps
        running += 1
        overlaps += running > 1
        order.append(market)
        await asyncio.sleep(0.05)
        running -= 1

    with patch.object(sched, "_run_market_session", side_effect=fake_session):
        await asyncio.gather(sched.run_market_session("EU", "OPEN"), sched.run_market_session("US", "OPEN"))

    assert overlaps == 0
    assert sorted(order) == ["EU", "US"]
    assert not sched.market_session_running()


async def test_session_endpoints_refuse_while_a_session_runs():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        with patch.object(trading_scheduler, "run_market_session") as run:
            async with trading_scheduler._session_lock:
                r1 = await client.post("/api/market-session/run", json={"market": "EU", "session": "OPEN"})
                r2 = await client.post("/api/scheduler/trigger?market=EU")
            assert r1.json()["success"] is False
            assert r2.json()["success"] is False
            run.assert_not_called()


async def test_two_requests_at_once_start_only_one_session():
    """Both requests can arrive before the first background task takes the lock."""
    calls = []

    async def fake_session(market, session, target_chat_id=None):
        calls.append(market)
        await asyncio.sleep(0.02)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        with patch.object(trading_scheduler, "_run_market_session", side_effect=fake_session):
            r1, r2 = await asyncio.gather(
                client.post("/api/market-session/run", json={"market": "EU", "session": "OPEN"}),
                client.post("/api/market-session/run", json={"market": "EU", "session": "OPEN"}),
            )
            await asyncio.sleep(0.1)
    assert sorted([r1.json()["success"], r2.json()["success"]]) == [False, True]
    assert calls == ["EU"]
    assert not trading_scheduler.market_session_running()


async def test_telegram_review_refuses_while_a_session_runs():
    """Waiting for the lock would stall every other command, /killswitch included."""
    from config.settings import settings
    from src.telegram.bot import TelegramAssistant
    bot = TelegramAssistant()
    replies = []

    async def reply(chat_id, text, *a, **k):
        replies.append(text)

    bot._reply_chat = reply
    update = {"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/review"}}
    with patch.object(settings, "TELEGRAM_CHAT_ID", "12345"), \
            patch.object(trading_scheduler, "run_market_session") as run:
        async with trading_scheduler._session_lock:
            await bot._handle_update(update)
    run.assert_not_called()
    assert any("already running" in r for r in replies)


async def test_session_endpoint_starts_when_idle():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        with patch.object(trading_scheduler, "_run_market_session") as run:
            r = await client.post("/api/market-session/run", json={"market": "EU", "session": "OPEN"})
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    assert r.json()["success"] is True
    run.assert_called_once_with("EU", "OPEN", None)
