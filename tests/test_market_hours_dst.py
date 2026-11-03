import datetime
from zoneinfo import ZoneInfo

from src.data.market_hours import check_market_hours, get_market_statuses

AMS = ZoneInfo("Europe/Amsterdam")


def test_us_session_in_dst_mismatch_week_october():
    # 2026-10-27: EU is already on CET (UTC+1), US still on EDT (UTC-4) -> US opens 14:30 Amsterdam.
    before = datetime.datetime(2026, 10, 27, 14, 15, tzinfo=AMS)
    after_open = datetime.datetime(2026, 10, 27, 14, 45, tzinfo=AMS)
    last_hour = datetime.datetime(2026, 10, 27, 20, 30, tzinfo=AMS)
    after_close = datetime.datetime(2026, 10, 27, 21, 15, tzinfo=AMS)
    assert check_market_hours("AAPL", now=before)[0] is False
    assert check_market_hours("AAPL", now=after_open)[0] is True
    assert check_market_hours("AAPL", now=last_hour)[0] is True
    assert check_market_hours("AAPL", now=after_close)[0] is False


def test_us_session_after_both_switched():
    # 2026-11-03: both on standard time -> classic 15:30-22:00 Amsterdam.
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 11, 3, 15, 15, tzinfo=AMS))[0] is False
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 11, 3, 15, 45, tzinfo=AMS))[0] is True
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 11, 3, 21, 45, tzinfo=AMS))[0] is True
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 11, 3, 22, 15, tzinfo=AMS))[0] is False


def test_us_session_in_march_mismatch_week():
    # 2026-03-16: US already on EDT, EU still CET -> US opens 14:30 Amsterdam.
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 3, 16, 14, 45, tzinfo=AMS))[0] is True
    assert check_market_hours("AAPL", now=datetime.datetime(2026, 3, 16, 14, 15, tzinfo=AMS))[0] is False


def test_eu_session_unaffected():
    assert check_market_hours("ASML.AS", now=datetime.datetime(2026, 10, 27, 9, 30, tzinfo=AMS))[0] is True
    assert check_market_hours("ASML.AS", now=datetime.datetime(2026, 10, 27, 17, 45, tzinfo=AMS))[0] is False
    assert check_market_hours("ASML.AS", now=datetime.datetime(2026, 10, 27, 8, 45, tzinfo=AMS))[0] is False


def test_market_statuses_follow_exchange_timezone():
    now = datetime.datetime(2026, 10, 27, 14, 45, tzinfo=AMS)
    status = get_market_statuses(now=now)
    assert status["us_market"]["is_open"] is True
    assert status["eu_market"]["is_open"] is True
    assert status["us_market"]["hours"] == "14:30 - 21:00 CET"


def test_us_cron_jobs_use_new_york_timezone():
    import asyncio
    from src.scheduler.jobs import TradingScheduler

    async def run():
        sched = TradingScheduler()
        sched.start()
        try:
            jobs = {j.id: j for j in sched.scheduler.get_jobs()}
            for job_id, (h, m) in {"us_market_open": (9, 30), "us_market_close": (16, 0)}.items():
                trig = jobs[job_id].trigger
                assert str(trig.timezone) == "America/New_York"
                fields = {f.name: str(f) for f in trig.fields}
                assert fields["hour"] == str(h) and fields["minute"] == str(m)
        finally:
            sched.stop()

    asyncio.run(run())
