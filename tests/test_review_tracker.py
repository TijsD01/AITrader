import pytest
import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import MarketDigest, PortfolioSnapshot
from src.data.review_tracker import ReviewTracker, review_tracker
from src.telegram.bot import telegram_bot

@pytest.mark.asyncio
async def test_next_session_schedule_cycle():
    """
    Verifies that get_next_session_info returns the exact upcoming session
    according to CET schedule:
    - 09:00 CET: EU Open -> Next is 🇺🇸 US Open at 15:30 CET (fixes reported bug)
    - 15:30 CET: US Open -> Next is 🇪🇺 EU Close at 17:30 CET
    - 17:30 CET: EU Close -> Next is 🇺🇸 US Close at 22:00 CET
    - 22:00 CET: US Close -> Next is 🇪🇺 EU Open tomorrow at 09:00 CET
    """
    tz = ZoneInfo(settings.TIMEZONE)

    # 1. Early morning (08:30 CET) -> Next is EU Open at 09:00 CET
    t1 = datetime.datetime(2026, 9, 7, 8, 30, tzinfo=tz)
    s1 = ReviewTracker.get_next_session_info(t1)
    assert s1["market"] == "EU"
    assert s1["session"] == "OPEN"
    assert "09:00 CET" in s1["formatted"]

    # 2. Right after EU Open (09:10 CET) -> Next MUST be US Open at 15:30 CET!
    t2 = datetime.datetime(2026, 9, 7, 9, 10, tzinfo=tz)
    s2 = ReviewTracker.get_next_session_info(t2)
    assert s2["market"] == "US"
    assert s2["session"] == "OPEN"
    assert "15:30 CET" in s2["formatted"]
    assert "🇺🇸" in s2["flag"]

    # 3. Afternoon after US Open (16:00 CET) -> Next is EU Close at 17:30 CET
    t3 = datetime.datetime(2026, 9, 7, 16, 0, tzinfo=tz)
    s3 = ReviewTracker.get_next_session_info(t3)
    assert s3["market"] == "EU"
    assert s3["session"] == "CLOSE"
    assert "17:30 CET" in s3["formatted"]

    # 4. Evening after EU Close (18:00 CET) -> Next is US Close at 22:00 CET
    t4 = datetime.datetime(2026, 9, 7, 18, 0, tzinfo=tz)
    s4 = ReviewTracker.get_next_session_info(t4)
    assert s4["market"] == "US"
    assert s4["session"] == "CLOSE"
    assert "22:00 CET" in s4["formatted"]

    # 5. Night after US Close (22:30 CET on Monday) -> Next is EU Open tomorrow at 09:00 CET
    t5 = datetime.datetime(2026, 9, 7, 22, 30, tzinfo=tz)
    s5 = ReviewTracker.get_next_session_info(t5)
    assert s5["market"] == "EU"
    assert s5["session"] == "OPEN"
    assert "tomorrow" in s5["formatted"]

    # 6. Friday night after US Close (22:30 CET on Friday 2026-09-11) -> Next is Monday European Open!
    t_fri = datetime.datetime(2026, 9, 11, 22, 30, tzinfo=tz)
    s_fri = ReviewTracker.get_next_session_info(t_fri)
    assert s_fri["market"] == "EU"
    assert s_fri["session"] == "OPEN"
    assert "Monday" in s_fri["formatted"]

    # 7. Saturday afternoon (14:00 CET on Saturday 2026-09-12) -> Next is Monday European Open!
    t_sat = datetime.datetime(2026, 9, 12, 14, 0, tzinfo=tz)
    s_sat = ReviewTracker.get_next_session_info(t_sat)
    assert s_sat["market"] == "EU"
    assert s_sat["session"] == "OPEN"
    assert "Monday" in s_sat["formatted"]

    # 8. Sunday evening (20:00 CET on Sunday 2026-09-13) -> Next is Monday European Open!
    t_sun = datetime.datetime(2026, 9, 13, 20, 0, tzinfo=tz)
    s_sun = ReviewTracker.get_next_session_info(t_sun)
    assert s_sun["market"] == "EU"
    assert s_sun["session"] == "OPEN"
    assert "Monday" in s_sun["formatted"]

@pytest.mark.asyncio
async def test_save_and_retrieve_weekly_reviews():
    """Verify saving session review persists portfolio state and review text."""
    await init_db()

    mock_picks = [
        {
            "ticker": "TEST_ASML",
            "name": "ASML Holding",
            "action": "BUY",
            "consensus_score": 85,
            "price": 800.0,
            "currency": "EUR",
            "risk": {"take_profit_price": 880.0, "stop_loss_price": 760.0}
        }
    ]
    mock_state = {"total_value_eur": 10500.0, "cash_eur": 8500.0, "equity_eur": 2000.0}
    mock_text = "Sample formatted telegram review text"

    digest_id = await review_tracker.save_session_review(
        market="EU",
        session_type="OPEN",
        headline="European Market Open Test",
        content="Testing review archival",
        top_picks=mock_picks,
        portfolio_state=mock_state,
        review_text=mock_text
    )

    assert digest_id > 0

    weekly = await review_tracker.get_weekly_reviews(days=7)
    found = next((d for d in weekly if d["id"] == digest_id), None)
    assert found is not None
    assert found["headline"] == "European Market Open Test"
    assert found["portfolio_state"]["total_value_eur"] == 10500.0
    assert found["review_text"] == mock_text
    assert len(found["top_picks"]) == 1
    assert found["top_picks"][0]["ticker"] == "TEST_ASML"

@pytest.mark.asyncio
async def test_compare_past_week_reviews_evaluation():
    """Verify comparing past-week reviews calculates win rates and status badges."""
    await init_db()

    # Seed an archival review with two picks
    test_picks = [
        {
            "ticker": "WIN_STOCK",
            "name": "Winning Corp",
            "price": 100.0,
            "currency": "USD",
            "target_price": 115.0,
            "stop_loss": 92.0
        },
        {
            "ticker": "LOSS_STOCK",
            "name": "Losing Corp",
            "price": 50.0,
            "currency": "USD",
            "target_price": 60.0,
            "stop_loss": 45.0
        }
    ]

    await review_tracker.save_session_review(
        market="US",
        session_type="OPEN",
        headline="US Open Benchmark Test",
        content="Evaluation test digest",
        top_picks=test_picks,
        portfolio_state={"total_value_eur": 10000.0}
    )

    # Mock market data provider: WIN_STOCK is up to 110 (+10%), LOSS_STOCK is down to 48 (-4%)
    def mock_stock_data(ticker):
        if ticker == "WIN_STOCK":
            return {"price": 110.0, "currency": "USD"}
        elif ticker == "LOSS_STOCK":
            return {"price": 48.0, "currency": "USD"}
        return {"price": 100.0, "currency": "USD"}

    with patch("src.data.review_tracker.market_data_provider.get_stock_data", side_effect=mock_stock_data):
        comp = await review_tracker.compare_past_week_reviews(days=7)

        assert comp["has_history"] is True
        assert comp["total_picks_tracked"] >= 2
        assert comp["positive_picks_count"] >= 1
        assert "text_summary" in comp
        assert "PAST-WEEK REVIEWS" in comp["text_summary"]

        win_pick = next((p for p in comp["tracked_picks"] if p["ticker"] == "WIN_STOCK"), None)
        assert win_pick is not None
        assert win_pick["pnl_pct"] == 10.0
        assert "PROFIT" in win_pick["status_badge"]

        loss_pick = next((p for p in comp["tracked_picks"] if p["ticker"] == "LOSS_STOCK"), None)
        assert loss_pick is not None
        assert loss_pick["pnl_pct"] == -4.0
        assert "PULLING BACK" in loss_pick["status_badge"]

@pytest.mark.asyncio
async def test_telegram_notifications_dynamic_next_session():
    """Verify Telegram notifications format the dynamic next session correctly."""
    portfolio = {
        "cash_eur": 9000.0,
        "total_value_eur": 10200.0,
        "open_positions_count": 1
    }
    with patch("src.telegram.bot.telegram_bot.send_message") as mock_send, \
         patch("src.data.review_tracker.ReviewTracker.get_next_session_info", return_value={
             "market": "US",
             "session": "OPEN",
             "formatted": "🇺🇸 US Market Open at 15:30 CET"
         }):
        text = await telegram_bot.notify_market_open(
            market="EU",
            reports=[],
            executed_trades=[],
            portfolio=portfolio,
            comparison={
                "has_history": True,
                "text_summary": "📜 PAST-WEEK REVIEWS: 75% Win Rate"
            }
        )

        assert "🇺🇸 US Market Open at 15:30 CET" in text
        assert "Next update: <b>🇺🇸 US Market Open at 15:30 CET</b>" in text
        assert "PAST-WEEK REVIEWS: 75% Win Rate" in text

@pytest.mark.asyncio
async def test_market_open_advice_card_statuses():
    """Verify that notify_market_open formats all execution statuses correctly."""
    reports = [
        {
            "ticker": "BOUGHT_TICKER",
            "name": "Bought Corp",
            "consensus_score": 85,
            "action": "STRONG_BUY",
            "price": 100.0,
            "currency": "EUR",
            "status_type": "BOUGHT",
            "technical": {"analysis_summary": "Strong breakout above 50 EMA", "bias": "BULLISH"},
            "fundamental": {"valuation": "UNDERVALUED", "health": "STRONG"},
            "risk": {"stop_loss_price": 95.0, "take_profit_price": 115.0, "risk_reward_ratio": 3.0}
        },
        {
            "ticker": "ARMED_TICKER",
            "name": "Armed Corp",
            "consensus_score": 82,
            "action": "BUY",
            "price": 50.0,
            "currency": "EUR",
            "status_type": "TRIGGER_ARMED",
            "entry_trigger_price": 48.5,
            "technical": {"analysis_summary": "Pullback to 20 EMA expected", "bias": "BULLISH"},
            "fundamental": {"valuation": "FAIR", "health": "STRONG"},
            "risk": {"stop_loss_price": 46.0, "take_profit_price": 58.0, "risk_reward_ratio": 2.5}
        },
        {
            "ticker": "HELD_TICKER",
            "name": "Held Corp",
            "consensus_score": 80,
            "action": "BUY",
            "price": 200.0,
            "currency": "EUR",
            "status_type": "ALREADY_HELD",
            "technical": {"analysis_summary": "Holding trend nicely", "bias": "BULLISH"},
            "fundamental": {"valuation": "FAIR", "health": "MODERATE"},
            "risk": {"stop_loss_price": 190.0, "take_profit_price": 225.0, "risk_reward_ratio": 2.2}
        },
        {
            "ticker": "PENDING_TICKER",
            "name": "Pending Corp",
            "consensus_score": 78,
            "action": "BUY",
            "price": 30.0,
            "currency": "EUR",
            "status_type": "TRIGGER_PENDING",
            "technical": {"analysis_summary": "Consolidating", "bias": "NEUTRAL"},
            "fundamental": {"valuation": "FAIR", "health": "MODERATE"},
            "risk": {"stop_loss_price": 28.0, "take_profit_price": 35.0, "risk_reward_ratio": 2.0}
        }
    ]

    executed_trades = [
        {"ticker": "BOUGHT_TICKER", "shares": 10, "total_eur": 1000.0}
    ]
    armed_triggers = [
        {"ticker": "ARMED_TICKER", "target_price": 48.5}
    ]
    positions = [
        {
            "ticker": "HELD_TICKER",
            "shares": 25,
            "entry_price_native": 195.0,
            "current_price_native": 200.0,
            "currency": "EUR",
            "current_value_eur": 5000.0,
            "unrealized_pnl_eur": 125.0,
            "unrealized_pnl_pct": 2.56,
            "stop_loss_native": 190.0,
            "take_profit_native": 220.0
        }
    ]
    portfolio = {"cash_eur": 5000.0, "total_value_eur": 10000.0, "open_positions_count": 1}

    with patch("src.telegram.bot.telegram_bot.send_message") as mock_send, \
         patch("src.data.review_tracker.ReviewTracker.get_next_session_info", return_value={
             "market": "US", "session": "OPEN", "formatted": "🇺🇸 US Market Open at 15:30 CET"
         }):
        text = await telegram_bot.notify_market_open(
            market="EU",
            reports=reports,
            executed_trades=executed_trades,
            portfolio=portfolio,
            armed_triggers=armed_triggers,
            positions=positions
        )

        assert "CURRENT STOCK HOLDINGS:" in text
        assert "HELD_TICKER" in text
        assert "+€125.00 (+2.6%)" in text
        assert "AUTO-BOUGHT:" in text
        assert "BOUGHT_TICKER" in text
        assert "TRIGGER ARMED:" in text
        assert "ARMED_TICKER" in text
        assert "CURRENT HOLDING:" in text
        assert "TRIGGER PENDING:" in text
        assert "PENDING_TICKER" in text

@pytest.mark.asyncio
async def test_run_market_session_candidate_selection_anti_overlap():
    """Verify run_market_session selects 4 candidates, skips held/pending for fresh candidates, and backfills if needed."""
    from src.scheduler.jobs import trading_scheduler
    from src.db.session import AsyncSessionLocal
    from src.db.models import Position, OrderTrigger

    # Seed an open position and a pending trigger in DB
    async with AsyncSessionLocal() as session:
        pos = Position(
            ticker="HELD_STK",
            name="Held Stock",
            shares=10,
            entry_price_native=100.0,
            current_price_native=105.0,
            highest_price_native=105.0,
            entry_price_eur=100.0,
            current_price_eur=105.0,
            currency="EUR",
            cost_basis_eur=1000.0,
            status="OPEN"
        )
        trig = OrderTrigger(
            ticker="PEND_STK",
            name="Pending Stock",
            currency="EUR",
            trigger_type="LIMIT_PULLBACK",
            target_price=45.0,
            current_price_at_creation=50.0,
            allocation_eur=500.0,
            status="PENDING"
        )
        session.add(pos)
        session.add(trig)
        await session.commit()

    mock_candidates = [
        {"ticker": "HELD_STK", "name": "Held Stock", "pre_score": 95},
        {"ticker": "PEND_STK", "name": "Pending Stock", "pre_score": 90},
        {"ticker": "FRESH1", "name": "Fresh Stock 1", "pre_score": 85},
        {"ticker": "FRESH2", "name": "Fresh Stock 2", "pre_score": 80},
        {"ticker": "FRESH3", "name": "Fresh Stock 3", "pre_score": 75},
        {"ticker": "FRESH4", "name": "Fresh Stock 4", "pre_score": 70},
    ]

    def mock_stock(ticker):
        return {
            "ticker": ticker,
            "name": f"Name {ticker}",
            "price": 100.0,
            "currency": "EUR",
            "technicals": {"rsi14": 55, "volume_surge_ratio": 1.5},
            "fundamentals": {}
        }

    async def mock_eval(data, total_val):
        return {
            "ticker": data["ticker"],
            "name": data["name"],
            "price": data["price"],
            "currency": data["currency"],
            "action": "BUY",
            "consensus_score": 78,
            "entry_trigger_price": 100.0,
            "trigger_type": "LIMIT_PULLBACK",
            "risk": {
                "take_profit_price": 115.0,
                "stop_loss_price": 95.0,
                "recommended_size_pct": 0.05,
                "risk_summary": "Low"
            },
            "technical": {"analysis_summary": "Good momentum", "bias": "BULLISH"},
            "fundamental": {"valuation": "FAIR", "health": "STRONG", "analysis_summary": "Solid"},
            "sentiment": {"sentiment_summary": "Positive"},
            "summary": "Solid candidate",
            "debate_transcript": "Good pick"
        }

    try:
        with patch("src.screener.scanner.market_scanner.scan_universe", return_value=mock_candidates), \
             patch("src.data.market.market_data_provider.get_stock_data", side_effect=mock_stock), \
             patch("src.agents.committee.investment_committee.evaluate_ticker", side_effect=mock_eval), \
             patch("src.telegram.bot.telegram_bot.send_message") as mock_send, \
             patch("src.data.review_tracker.ReviewTracker.get_next_session_info", return_value={
                 "market": "US", "session": "OPEN", "formatted": "🇺🇸 US Market Open at 15:30 CET"
             }):
            
            await trading_scheduler.run_market_session(market="EU", session="OPEN")

            # Verify that send_message was called for notify_market_open
            assert mock_send.call_count >= 1
            open_report_call = next((call[0][0] for call in mock_send.call_args_list if "EUROPEAN MARKET OPEN" in call[0][0]), None)
            assert open_report_call is not None
            
            # Verify that HELD_STK appears in CURRENT STOCK HOLDINGS
            assert "CURRENT STOCK HOLDINGS:" in open_report_call
            assert "HELD_STK" in open_report_call
            
            # Verify that under recommendations, fresh candidates were selected
            rec_section = open_report_call.split("TOP COMMITTEE STOCK RECOMMENDATIONS:")[1]
            assert "FRESH1" in rec_section
            assert "FRESH2" in rec_section
            assert "FRESH3" in rec_section
            assert "FRESH4" in rec_section
            assert "HELD_STK" not in rec_section
            assert "PEND_STK" not in rec_section
    finally:
        from sqlalchemy import delete
        from src.db.models import AnalysisReport, MarketDigest
        async with AsyncSessionLocal() as session:
            test_tickers = ["HELD_STK", "PEND_STK", "FRESH1", "FRESH2", "FRESH3", "FRESH4"]
            await session.execute(delete(Position).where(Position.ticker.in_(test_tickers)))
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_(test_tickers)))
            await session.execute(delete(AnalysisReport).where(AnalysisReport.ticker.in_(test_tickers)))
            await session.commit()


