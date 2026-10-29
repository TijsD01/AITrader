import pytest
import datetime
from unittest.mock import patch, MagicMock

from src.db.session import init_db, AsyncSessionLocal
from src.db.models import OrderTrigger, Position, PortfolioSnapshot
from src.agents.risk_manager import RiskManagerAgent
from src.agents.committee import investment_committee
from src.trading.paper import paper_broker
from src.telegram.bot import telegram_bot

@pytest.mark.asyncio
async def test_ai_trigger_price_calculation():
    """Verify AI risk manager and committee calculate entry trigger levels."""
    mock_stock_data = {
        "ticker": "NVDA",
        "name": "NVIDIA Corporation",
        "price": 120.0,
        "currency": "USD",
        "technicals": {
            "price": 120.0,
            "ema20": 118.0,
            "ema50": 114.0,
            "sma200": 105.0,
            "above_ema20": True,
            "above_ema50": True,
            "above_sma200": True,
            "rsi14": 58.0,
            "macd": 1.5,
            "macd_signal": 1.2,
            "macd_hist": 0.3,
            "atr14": 3.5,
            "support_20d": 115.0,
            "resistance_20d": 125.0,
            "return_5d_pct": 2.4,
            "return_1m_pct": 6.8,
            "volume_surge_ratio": 1.1
        },
        "fundamentals": {
            "beta": 1.2,
            "market_cap": 2500000000000
        }
    }

    with patch("src.agents.base.llm_client.has_active_key", return_value=False), \
         patch("src.agents.committee.llm_client.has_active_key", return_value=False):
        report = await investment_committee.evaluate_ticker(mock_stock_data, portfolio_equity_eur=10000.0)
        assert "entry_trigger_price" in report
        assert "trigger_type" in report
        assert "trigger_rationale" in report
        assert report["entry_trigger_price"] > 0
        assert report["trigger_type"] in ("LIMIT_PULLBACK", "BREAKOUT_BUY")

@pytest.mark.asyncio
async def test_order_trigger_crud():
    """Verify creating, querying, and cancelling order triggers."""
    await init_db()

    # Create a test trigger with unique ticker
    import time
    ticker = f"TRIG_{int(time.time() * 1000)}"
    create_res = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Test Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=95.0,
        current_price_at_creation=100.0,
        stop_loss=90.0,
        take_profit=110.0,
        allocation_eur=800.0,
        consensus_score=85,
        ai_rationale="Test pullback support entry"
    )

    assert create_res["success"] is True
    trigger_id = create_res["trigger_id"]

    # Query triggers
    triggers = await paper_broker.get_triggers()
    found = next((t for t in triggers if t["id"] == trigger_id), None)
    assert found is not None
    assert found["ticker"] == ticker
    assert found["target_price"] == 95.0
    assert found["status"] == "PENDING"

    # Cancel trigger
    cancel_res = await paper_broker.cancel_price_trigger(trigger_id)
    assert cancel_res["success"] is True

    # Verify status changed
    triggers_after = await paper_broker.get_triggers()
    found_after = next((t for t in triggers_after if t["id"] == trigger_id), None)
    assert found_after["status"] == "CANCELLED"

@pytest.mark.asyncio
async def test_trigger_blocked_when_market_closed():
    """Verify that even if a trigger price is hit, it will NOT execute if market is closed."""
    await init_db()

    import time
    ticker = f"CLOSE_{int(time.time() * 1000)}"
    create_res = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Closed Market Stock",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=50.0,
        current_price_at_creation=55.0,
        stop_loss=45.0,
        take_profit=65.0,
        allocation_eur=500.0,
        consensus_score=80,
        ai_rationale="Limit entry"
    )
    assert create_res["success"] is True
    trigger_id = create_res["trigger_id"]

    # Mock market closed AND live price at $49 (condition met)
    mock_data = {"price": 49.0, "currency": "USD", "name": "Closed Market Stock"}
    with patch("src.trading.paper.check_market_hours", return_value=(False, "Market is closed")), \
         patch("src.trading.paper.market_data_provider.get_stock_data", return_value=mock_data):
        executed = await paper_broker.check_and_execute_triggers()
        assert not any(t.get("trigger_id") == trigger_id for t in executed)

    # Clean up
    await paper_broker.cancel_price_trigger(trigger_id)

@pytest.mark.asyncio
async def test_trigger_executes_when_market_open():
    """Verify that when market is open and trigger condition is reached, the trade executes."""
    await init_db()

    import time
    ticker = f"OPEN_{int(time.time() * 1000)}"
    create_res = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Open Market Stock",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=80.0,
        current_price_at_creation=85.0,
        stop_loss=75.0,
        take_profit=95.0,
        allocation_eur=500.0,
        consensus_score=82,
        ai_rationale="Dip buy support reached"
    )
    assert create_res["success"] is True
    trigger_id = create_res["trigger_id"]

    # Mock market OPEN and live price at $79.50 (trigger condition met)
    mock_data = {
        "ticker": ticker,
        "price": 79.50,
        "currency": "USD",
        "name": "Open Market Stock"
    }

    with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=lambda sym: mock_data if sym == ticker else None), \
         patch("src.trading.risk.check_market_hours", return_value=(True, "Market is OPEN")):
        executed = await paper_broker.check_and_execute_triggers()
        
        # Verify trigger execution
        trig_exec = next((t for t in executed if t.get("trigger_id") == trigger_id), None)
        assert trig_exec is not None
        assert trig_exec["success"] is True

    # Verify trigger status is TRIGGERED in database
    triggers = await paper_broker.get_triggers()
    trig_record = next((t for t in triggers if t["id"] == trigger_id), None)
    assert trig_record["status"] == "TRIGGERED"
    assert trig_record["triggered_at"] is not None

    # Clean up position
    with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")):
        await paper_broker.execute_sell(ticker, reason="Test cleanup")

@pytest.mark.asyncio
async def test_trigger_anti_overlap_with_open_position():
    """Verify that a trigger CANNOT be armed if an open position already exists for that ticker."""
    await init_db()
    import time
    ticker = f"POS_{int(time.time() * 1000)}"

    # 1. Open a position for this ticker
    with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.risk.check_market_hours", return_value=(True, "Market is OPEN")):
        buy_res = await paper_broker.execute_buy(
            ticker=ticker,
            name="Existing Position Corp",
            price_native=100.0,
            currency="EUR",
            stop_loss_native=95.0,
            take_profit_native=110.0,
            allocation_eur=200.0,
            consensus_score=85,
            rationale="Initial buy",
            is_manual=True
        )
        assert buy_res["success"] is True

    # 2. Try to arm a trigger for the same ticker -> MUST fail (anti-overlap)
    trig_res = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Existing Position Corp",
        currency="EUR",
        trigger_type="LIMIT_PULLBACK",
        target_price=90.0,
        current_price_at_creation=100.0,
        stop_loss=85.0,
        take_profit=105.0,
        allocation_eur=200.0,
        consensus_score=80,
        ai_rationale="Overlapping trigger attempt"
    )
    assert trig_res["success"] is False
    assert "active position already exists" in trig_res["message"]

    # Clean up position
    with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")):
        await paper_broker.execute_sell(ticker, reason="Test cleanup")

@pytest.mark.asyncio
async def test_trigger_anti_overlap_duplicate_ticker():
    """Verify that multiple pending triggers cannot be armed for the same ticker."""
    await init_db()
    import time
    ticker = f"DUP_{int(time.time() * 1000)}"

    # 1. First trigger succeeds
    res1 = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Duplicate Test Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=90.0,
        current_price_at_creation=100.0,
        stop_loss=85.0,
        take_profit=110.0,
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="First trigger"
    )
    assert res1["success"] is True
    t_id = res1["trigger_id"]

    # 2. Second trigger for same ticker fails
    res2 = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Duplicate Test Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=88.0,
        current_price_at_creation=100.0,
        stop_loss=80.0,
        take_profit=110.0,
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="Duplicate trigger"
    )
    assert res2["success"] is False
    assert "pending trigger already exists" in res2["message"]

    # Clean up
    await paper_broker.cancel_price_trigger(t_id)

@pytest.mark.asyncio
async def test_trigger_anti_overlap_price_inversion():
    """Verify that overlapping or inverted trigger price levels are rejected."""
    await init_db()
    import time
    ticker = f"PRC_{int(time.time() * 1000)}"

    # 1. Pullback target must be BELOW current price
    bad_pullback = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Price Inversion Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=105.0,  # Above current price!
        current_price_at_creation=100.0,
        stop_loss=95.0,
        take_profit=120.0,
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="Bad pullback"
    )
    assert bad_pullback["success"] is False
    assert "LIMIT_PULLBACK target price (105.0) must be below current price" in bad_pullback["message"]

    # 2. Breakout target must be ABOVE current price
    bad_breakout = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Price Inversion Corp",
        currency="USD",
        trigger_type="BREAKOUT_BUY",
        target_price=95.0,  # Below current price!
        current_price_at_creation=100.0,
        stop_loss=90.0,
        take_profit=115.0,
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="Bad breakout"
    )
    assert bad_breakout["success"] is False
    assert "BREAKOUT_BUY target price (95.0) must be above current price" in bad_breakout["message"]

    # 3. Stop loss cannot be above target price (SL overlap)
    bad_sl = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Price Inversion Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=95.0,
        current_price_at_creation=100.0,
        stop_loss=96.0,  # Above target price!
        take_profit=115.0,
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="Bad SL"
    )
    assert bad_sl["success"] is False
    assert "Stop Loss (96.0) must be below target price (95.0)" in bad_sl["message"]

    # 4. Take profit cannot be below target price (TP overlap)
    bad_tp = await paper_broker.create_price_trigger(
        ticker=ticker,
        name="Price Inversion Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=95.0,
        current_price_at_creation=100.0,
        stop_loss=90.0,
        take_profit=92.0,  # Below target price!
        allocation_eur=100.0,
        consensus_score=80,
        ai_rationale="Bad TP"
    )
    assert bad_tp["success"] is False
    assert "Take Profit (92.0) must be above target price (95.0)" in bad_tp["message"]

@pytest.mark.asyncio
async def test_trigger_capital_overlap_and_cash_reservation():
    """Verify that triggers reserve available cash and cannot overlap or exceed real cash."""
    await init_db()
    summary = await paper_broker.get_portfolio_summary()
    avail = summary["available_cash_eur"]

    import time
    ticker_huge = f"HUGE_{int(time.time() * 1000)}"

    # 1. Attempt to arm trigger requiring more than available cash -> blocked
    res_oversized = await paper_broker.create_price_trigger(
        ticker=ticker_huge,
        name="Too Expensive Corp",
        currency="USD",
        trigger_type="LIMIT_PULLBACK",
        target_price=90.0,
        current_price_at_creation=100.0,
        stop_loss=85.0,
        take_profit=110.0,
        allocation_eur=avail + 500.0,  # Exceeds cash
        consensus_score=80,
        ai_rationale="Exceeds budget"
    )
    assert res_oversized["success"] is False
    assert "insufficient available cash" in res_oversized["message"]

@pytest.mark.asyncio
async def test_telegram_triggers_command():
    """Verify telegram bot handles /triggers without crashing."""
    mock_update = {
        "message": {
            "text": "/triggers",
            "chat": {"id": 12345}
        }
    }
    with patch.object(telegram_bot, "_reply_chat", return_value=None) as mock_reply:
        await telegram_bot._handle_update(mock_update)
        mock_reply.assert_called_once()
        call_args = mock_reply.call_args[0]
        assert "ACTIVE AI PRICE TRIGGERS" in call_args[1] or "NO ACTIVE AI TRIGGERS" in call_args[1]

@pytest.mark.asyncio
async def test_auto_arm_triggers_score_above_80():
    """Verify run_market_session auto-arms trigger if score >= 80, and skips if score < 80."""
    from src.scheduler.jobs import trading_scheduler
    from src.db.models import OrderTrigger
    from sqlalchemy import select, delete
    await init_db()

    mock_candidates = [
        {"ticker": "HIGH85", "name": "High Conviction Corp", "pre_score": 85},
        {"ticker": "LOW75", "name": "Moderate Conviction Corp", "pre_score": 75}
    ]

    def mock_stock(ticker):
        return {
            "ticker": ticker,
            "name": f"Name {ticker}",
            "price": 100.0,
            "currency": "EUR",
            "technicals": {"rsi14": 55, "volume_surge_ratio": 1.5, "atr14": 2.0},
            "fundamentals": {}
        }

    async def mock_eval(data, total_val):
        score = 85 if data["ticker"] == "HIGH85" else 75
        return {
            "ticker": data["ticker"],
            "name": data["name"],
            "price": data["price"],
            "currency": data["currency"],
            "action": "STRONG_BUY" if score >= 80 else "BUY",
            "consensus_score": score,
            "entry_trigger_price": 98.0,
            "trigger_type": "LIMIT_PULLBACK",
            "trigger_rationale": "High score setup",
            "risk": {
                "take_profit_price": 110.0,
                "stop_loss_price": 95.0,
                "recommended_size_pct": 0.05,
                "risk_summary": "Managed risk"
            },
            "technical": {"analysis_summary": "Bullish", "bias": "BULLISH"},
            "fundamental": {"valuation": "UNDERVALUED", "health": "STRONG", "analysis_summary": "Solid"},
            "sentiment": {"sentiment_summary": "Positive"},
            "summary": f"Conviction {score}",
            "debate_transcript": "Debate conclusion"
        }

    try:
        with patch("src.screener.scanner.market_scanner.scan_universe", return_value=mock_candidates), \
             patch("src.data.market.market_data_provider.get_stock_data", side_effect=mock_stock), \
             patch("src.agents.committee.investment_committee.evaluate_ticker", side_effect=mock_eval), \
             patch("src.telegram.bot.telegram_bot.send_message"), \
             patch("src.telegram.bot.telegram_bot.notify_trigger_armed"):
            
            await trading_scheduler.run_market_session(market="EU", session="OPEN")

            triggers = await paper_broker.get_triggers()
            high85_trigger = next((t for t in triggers if t["ticker"] == "HIGH85" and t["status"] == "PENDING"), None)
            low75_trigger = next((t for t in triggers if t["ticker"] == "LOW75" and t["status"] == "PENDING"), None)

            # HIGH85 (score 85 >= 80) MUST be automatically armed
            assert high85_trigger is not None
            assert high85_trigger["target_price"] == 98.0

            # LOW75 (score 75 < 80) MUST NOT be armed
            assert low75_trigger is None
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_(["HIGH85", "LOW75"])))
            await session.commit()

@pytest.mark.asyncio
async def test_telegram_analyze_auto_arms_trigger_score_above_80():
    """Verify /analyze auto-arms trigger if score >= 80."""
    from src.db.models import OrderTrigger
    from sqlalchemy import delete
    await init_db()

    mock_stock = {
        "ticker": "AUTO85",
        "name": "Auto Trigger Inc",
        "price": 50.0,
        "currency": "EUR",
        "technicals": {"rsi14": 60, "volume_surge_ratio": 1.2, "atr14": 1.0},
        "fundamentals": {}
    }

    mock_report = {
        "ticker": "AUTO85",
        "name": "Auto Trigger Inc",
        "price": 50.0,
        "currency": "EUR",
        "action": "STRONG_BUY",
        "consensus_score": 86,
        "entry_trigger_price": 49.0,
        "trigger_type": "LIMIT_PULLBACK",
        "trigger_rationale": "Score > 80 setup",
        "risk": {
            "take_profit_price": 55.0,
            "stop_loss_price": 47.0,
            "recommended_size_pct": 0.05,
            "risk_summary": "Risk controlled"
        },
        "technical": {"analysis_summary": "Strong momentum", "bias": "BULLISH"},
        "fundamental": {"valuation": "UNDERVALUED", "health": "STRONG", "analysis_summary": "Solid balance sheet"},
        "sentiment": {"sentiment_summary": "Very bullish"},
        "summary": "Strong conviction",
        "debate_transcript": "Transcript"
    }

    replies = []
    async def mock_reply(chat_id, text):
        replies.append(text)

    try:
        with patch.object(telegram_bot, "_reply_chat", side_effect=mock_reply), \
             patch("src.data.market.market_data_provider.get_stock_data", return_value=mock_stock), \
             patch("src.agents.committee.investment_committee.evaluate_ticker", return_value=mock_report), \
             patch.object(telegram_bot, "notify_trigger_armed", return_value=None):

            update = {
                "message": {
                    "text": "/analyze AUTO85",
                    "chat": {"id": 12345}
                }
            }
            await telegram_bot._handle_update(update)

            assert len(replies) >= 2  # "Analyzing..." then verdict
            verdict_reply = replies[-1]
            assert "AUTOMATIC TRIGGER ARMED" in verdict_reply
            assert "49.0" in verdict_reply

            # Verify trigger in database
            triggers = await paper_broker.get_triggers()
            armed = next((t for t in triggers if t["ticker"] == "AUTO85" and t["status"] == "PENDING"), None)
            assert armed is not None
            assert armed["target_price"] == 49.0
            assert armed["consensus_score"] == 86
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "AUTO85"))
            await session.commit()


@pytest.mark.asyncio
async def test_dynamic_trigger_adjustment_based_on_cash_and_positions():
    """Verify triggers dynamically adjust allocation_eur when cash balance or max positions change."""
    from sqlalchemy import select, delete
    from config.settings import settings
    await init_db()

    orig_max_positions = settings.MAX_POSITIONS
    orig_full_budget = settings.FULL_BUDGET_UTILIZATION
    settings.FULL_BUDGET_UTILIZATION = True
    settings.MAX_POSITIONS = 4

    test_ticker = "DYN_ADJUST_TEST"
    try:
        # 1. Snapshot cash at 4000 EUR
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == test_ticker))
            snap = PortfolioSnapshot(
                cash_eur=4000.0,
                equity_eur=0.0,
                total_value_eur=4000.0,
                open_positions_count=0,
                is_live=False,
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(snap)
            await session.commit()

        # 2. Arm trigger with initial 100 EUR allocation
        trig = await paper_broker.create_price_trigger(
            ticker=test_ticker,
            name="Dynamic Adjust Co",
            currency="EUR",
            trigger_type="LIMIT_PULLBACK",
            target_price=90.0,
            current_price_at_creation=100.0,
            stop_loss=85.0,
            take_profit=110.0,
            allocation_eur=100.0,
            consensus_score=85,
            ai_rationale="Testing dynamic adjustments"
        )
        assert trig["success"] is True
        t_id = trig["trigger_id"]

        # 3. Adjust triggers dynamically based on 4000 EUR cash across 4 slots -> 1000 EUR each
        adjusted = await paper_broker.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == t_id))
            t = res.scalars().first()
            assert t.allocation_eur == 1000.0

        # 4. Cash doubles to 8000 EUR -> triggers dynamically adjust to 2000 EUR
        async with AsyncSessionLocal() as session:
            snap2 = PortfolioSnapshot(
                cash_eur=8000.0,
                equity_eur=0.0,
                total_value_eur=8000.0,
                open_positions_count=0,
                is_live=False,
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(snap2)
            await session.commit()

        await paper_broker.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == t_id))
            t = res.scalars().first()
            assert t.allocation_eur == 2000.0

        # 5. User changes MAX_POSITIONS in settings from 4 to 2 slots -> triggers adjust to 4000 EUR (8000 / 2)
        settings.MAX_POSITIONS = 2
        await paper_broker.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == t_id))
            t = res.scalars().first()
            assert t.allocation_eur == 4000.0

    finally:
        settings.MAX_POSITIONS = orig_max_positions
        settings.FULL_BUDGET_UTILIZATION = orig_full_budget
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == test_ticker))
            await session.commit()

