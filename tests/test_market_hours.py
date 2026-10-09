import datetime
from zoneinfo import ZoneInfo
import pytest
from unittest.mock import patch

from src.data.market_hours import check_market_hours, is_eu_ticker, get_market_statuses
from src.trading.risk import risk_guardrail
from src.trading.paper import paper_broker

def test_market_ticker_detection():
    assert is_eu_ticker("ASML.AS") is True
    assert is_eu_ticker("SAP.DE") is True
    assert is_eu_ticker("MC.PA") is True
    assert is_eu_ticker("NVDA") is False
    assert is_eu_ticker("AAPL") is False

def test_market_hours_weekends():
    tz = ZoneInfo("Europe/Amsterdam")
    # Saturday at 14:00 CET
    sat = datetime.datetime(2026, 9, 5, 14, 0, tzinfo=tz)
    is_open_eu, msg_eu = check_market_hours("ASML.AS", now=sat)
    is_open_us, msg_us = check_market_hours("AAPL", now=sat)

    assert is_open_eu is False
    assert "weekends" in msg_eu.lower()
    assert is_open_us is False
    assert "weekends" in msg_us.lower()

def test_market_hours_weekday_trading_hours():
    tz = ZoneInfo("Europe/Amsterdam")
    # Wednesday 11:00 CET (EU should be OPEN, US should be CLOSED)
    wed_11am = datetime.datetime(2026, 9, 9, 11, 0, tzinfo=tz)
    eu_open, _ = check_market_hours("ASML.AS", now=wed_11am)
    us_open, _ = check_market_hours("AAPL", now=wed_11am)
    assert eu_open is True
    assert us_open is False

    # Wednesday 16:30 CET (Both EU and US should be OPEN)
    wed_1630 = datetime.datetime(2026, 9, 9, 16, 30, tzinfo=tz)
    eu_open_afternoon, _ = check_market_hours("ASML.AS", now=wed_1630)
    us_open_afternoon, _ = check_market_hours("AAPL", now=wed_1630)
    assert eu_open_afternoon is True
    assert us_open_afternoon is True

    # Wednesday 20:00 CET (EU CLOSED, US OPEN)
    wed_2000 = datetime.datetime(2026, 9, 9, 20, 0, tzinfo=tz)
    eu_open_night, _ = check_market_hours("ASML.AS", now=wed_2000)
    us_open_night, _ = check_market_hours("AAPL", now=wed_2000)
    assert eu_open_night is False
    assert us_open_night is True

    # Wednesday 23:30 CET (Both CLOSED)
    wed_late = datetime.datetime(2026, 9, 9, 23, 30, tzinfo=tz)
    eu_late, _ = check_market_hours("ASML.AS", now=wed_late)
    us_late, _ = check_market_hours("AAPL", now=wed_late)
    assert eu_late is False
    assert us_late is False

@pytest.mark.asyncio
async def test_paper_trade_rejected_when_market_closed():
    # Test with simulated closed market
    with patch("src.trading.risk.check_market_hours", return_value=(False, "Market is currently closed.")):
        valid, msg = risk_guardrail.validate_buy_order(
            ticker="ASML.AS",
            current_positions=[],
            cash_eur=10000.0,
            portfolio_total_eur=10000.0,
            allocation_eur=800.0,
            consensus_score=85,
            auto_buy_enabled=True
        )
        assert valid is False
        assert "closed" in msg.lower()

@pytest.mark.asyncio
async def test_execute_buy_and_sell_blocked_when_closed():
    with patch("src.trading.paper.check_market_hours", return_value=(False, "Markets are closed on weekends (Saturday, 14:00 CET). Next trading session opens Monday.")):
        buy_res = await paper_broker.execute_buy(
            ticker="ASML.AS",
            name="ASML Holding",
            price_native=750.0,
            currency="EUR",
            stop_loss_native=720.0,
            take_profit_native=810.0,
            allocation_eur=800.0,
            consensus_score=85,
            rationale="Test buy"
        )
        assert buy_res["success"] is False
        assert "closed on weekends" in buy_res["message"].lower()

        sell_res = await paper_broker.execute_sell("ASML.AS", reason="Test sell")
        assert sell_res["success"] is False
        assert "closed on weekends" in sell_res["message"].lower()

@pytest.mark.asyncio
async def test_scheduler_crons_only_mon_to_fri():
    from src.scheduler.jobs import TradingScheduler
    sched = TradingScheduler()
    sched.start()
    try:
        jobs = {j.id: j for j in sched.scheduler.get_jobs()}
        for job_id in ["eu_market_open", "eu_market_close", "us_market_open", "us_market_close"]:
            assert job_id in jobs, f"Job {job_id} not found in scheduler"
            trigger = jobs[job_id].trigger
            fields = {f.name: str(f) for f in trigger.fields}
            assert "mon-fri" in fields.get("day_of_week", "").lower()
    finally:
        sched.stop()

@pytest.mark.asyncio
async def test_scheduled_session_skips_on_weekend():
    from src.scheduler.jobs import TradingScheduler
    sched = TradingScheduler()
    tz = ZoneInfo("Europe/Amsterdam")
    sat = datetime.datetime(2026, 9, 12, 14, 0, tzinfo=tz)

    with patch("datetime.datetime") as mock_dt, \
         patch.object(sched, "run_market_session") as mock_run:
        mock_dt.now.return_value = sat
        await sched._scheduled_market_session("EU", "OPEN")
        mock_run.assert_not_called()
