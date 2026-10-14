import pytest
import datetime
from unittest.mock import patch, MagicMock

from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position, TradeLesson, SystemConfig, MarketDigest, AnalysisReport
from src.trading.learner import trade_learner
from src.trading.paper import paper_broker
from src.telegram.bot import telegram_bot
from src.scheduler.jobs import trading_scheduler
from sqlalchemy import select, desc

@pytest.mark.asyncio
async def test_trade_post_mortem_win_and_loss():
    await init_db()
    
    # 1. Create a simulated winning trade
    async with AsyncSessionLocal() as session:
        win_trade = Trade(
            ticker="WIN_TEST",
            name="Win Corp",
            order_type="SELL",
            shares=10.0,
            currency="EUR",
            price_native=108.0,
            price_eur=108.0,
            total_eur=1080.0,
            realized_pnl_eur=80.0,
            realized_pnl_pct=8.0,
            rationale="Take-Profit triggered at 108.0 >= 108.0",
            executed_at=datetime.datetime.utcnow()
        )
        session.add(win_trade)
        await session.commit()
        await session.refresh(win_trade)
        win_id = win_trade.id

    # Conduct post-mortem
    with patch("src.agents.base.llm_client.has_active_key", return_value=False):
        res_win = await trade_learner.conduct_trade_post_mortem(win_id)
        assert res_win["success"] is True
        assert res_win["outcome"] == "WIN"
        assert "Take-profit target achieved" in res_win["lesson"] or "conviction" in res_win["lesson"].lower()

    # 2. Create a simulated losing trade with tight stop loss
    async with AsyncSessionLocal() as session:
        loss_trade = Trade(
            ticker="LOSS_TEST",
            name="Loss Corp",
            order_type="SELL",
            shares=10.0,
            currency="EUR",
            price_native=96.0,
            price_eur=96.0,
            total_eur=960.0,
            realized_pnl_eur=-40.0,
            realized_pnl_pct=-4.0,
            rationale="Stop-Loss triggered at 96.0 <= 96.0",
            executed_at=datetime.datetime.utcnow()
        )
        session.add(loss_trade)
        await session.commit()
        await session.refresh(loss_trade)
        loss_id = loss_trade.id

    # Conduct post-mortem
    with patch("src.agents.base.llm_client.has_active_key", return_value=False):
        res_loss = await trade_learner.conduct_trade_post_mortem(loss_id)
        assert res_loss["success"] is True
        assert res_loss["outcome"] == "LOSS"
        assert "ATR stop multiplier" in res_loss["lesson"] or "stop" in res_loss["lesson"].lower()

@pytest.mark.asyncio
async def test_autonomous_parameter_calibration():
    await init_db()

    # Insert 3 consecutive losing trades
    async with AsyncSessionLocal() as session:
        for i in range(3):
            t = Trade(
                ticker=f"CAL_LOSS_{i}",
                name="Cal Loss",
                order_type="SELL",
                shares=5.0,
                currency="EUR",
                price_native=95.0,
                price_eur=95.0,
                total_eur=475.0,
                realized_pnl_eur=-25.0,
                realized_pnl_pct=-5.0,
                rationale="Stop-Loss triggered",
                executed_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=i)
            )
            session.add(t)
        await session.commit()

    # Calibrate parameters
    cal_res = await trade_learner.calibrate_internal_parameters()
    assert cal_res["success"] is True
    assert cal_res["consecutive_losses"] >= 3
    
    # Check that parameters became more defensive:
    # min_consensus_score raised to >= 75
    # position_size_pct scaled down to <= 0.07
    p = cal_res["calibrated_parameters"]
    assert p["min_consensus_score"] >= 75
    assert p["position_size_pct"] <= 0.07
    assert p["atr_stop_multiplier"] >= 1.75

@pytest.mark.asyncio
async def test_learning_context_for_agents():
    await init_db()
    context = await trade_learner.get_learning_context_for_agents()
    assert "HISTORICAL TRADE LEARNING" in context
    assert "Active Adapted Min Score" in context
    assert "Active Risk Sizing" in context

@pytest.mark.asyncio
async def test_telegram_learning_and_review_commands():
    # 1. Test /learning
    mock_update_learning = {
        "message": {
            "text": "/learning",
            "chat": {"id": 12345}
        }
    }
    with patch.object(telegram_bot, "_reply_chat", return_value=None) as mock_reply:
        await telegram_bot._handle_update(mock_update_learning)
        mock_reply.assert_called_once()
        text = mock_reply.call_args[0][1]
        assert "AI TRADER POST-MORTEMS" in text
        assert "AUTONOMOUS ADAPTED PARAMETERS" in text

    # 2. Test /calibrate
    mock_update_calibrate = {
        "message": {
            "text": "/calibrate",
            "chat": {"id": 12345}
        }
    }
    with patch.object(telegram_bot, "_reply_chat", return_value=None) as mock_reply:
        await telegram_bot._handle_update(mock_update_calibrate)
        # Should have replied at least once
        assert mock_reply.call_count >= 1

    # 3. Test /review
    mock_update_review = {
        "message": {
            "text": "/review EU",
            "chat": {"id": 12345}
        }
    }
    with patch.object(telegram_bot, "_reply_chat", return_value=None) as mock_reply, \
         patch.object(trading_scheduler, "run_market_session", return_value=None) as mock_run:
        await telegram_bot._handle_update(mock_update_review)
        mock_run.assert_called_once_with(market="EU", session="OPEN", target_chat_id=12345)
