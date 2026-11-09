import pytest
import datetime
from unittest.mock import patch, MagicMock

from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position, TradeLesson, SystemConfig, MarketDigest, AnalysisReport, OrderTrigger
from src.trading.learner import trade_learner
from src.trading.paper import paper_broker
from src.telegram.bot import telegram_bot
from src.scheduler.jobs import trading_scheduler
from sqlalchemy import select, desc, delete

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
async def test_autonomous_parameter_calibration(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "LEARNER_MIN_TRADES", 3)

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
    before_size = settings.POSITION_SIZE_PCT
    async with AsyncSessionLocal() as session:
        before_row = (await session.execute(select(SystemConfig).where(SystemConfig.key == "POSITION_SIZE_PCT"))).scalars().first()
        before_db = before_row.value if before_row else None
    cal_res = await trade_learner.calibrate_internal_parameters()
    assert cal_res["success"] is True
    assert cal_res["consecutive_losses"] >= 3
    
    # Check that parameters became more defensive:
    # min_consensus_score raised to >= 75
    # position size is manual and untouched by calibration
    p = cal_res["calibrated_parameters"]
    assert p["min_consensus_score"] >= 75
    assert "position_size_pct" not in p and "POSITION_SIZE_PCT" not in p
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(SystemConfig).where(SystemConfig.key == "POSITION_SIZE_PCT"))).scalars().first()
        assert (row.value if row else None) == before_db
    assert settings.POSITION_SIZE_PCT == before_size
    assert not any("sizing" in a.lower() for a in cal_res["adjustments"])
    assert p["atr_stop_multiplier"] >= 1.75

@pytest.mark.asyncio
async def test_learning_context_for_agents():
    await init_db()
    # A stored lesson must not reach the prompt: single-trade stories anchor the LLM
    async with AsyncSessionLocal() as session:
        lesson = TradeLesson(trade_id=-1, ticker="LESSON_CTX", outcome="WIN", lesson="Stale single-trade story")
        session.add(lesson)
        await session.commit()
        lesson_id = lesson.id
    try:
        context = await trade_learner.get_learning_context_for_agents()
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(TradeLesson).where(TradeLesson.id == lesson_id))
            await session.commit()
    assert "HISTORICAL TRADE LEARNING" in context
    assert "Active Adapted Min Score" in context
    assert "Active Risk Sizing" in context
    assert "Key Lessons" not in context
    assert "Stale single-trade story" not in context

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


_IDLE_DECAY_KEYS = ["MIN_CONSENSUS_SCORE", "TRIGGER_MIN_SCORE", "LAST_CALIBRATION_TIMESTAMP", "LAST_IDLE_DECAY_DATE"]


@pytest.fixture
async def idle_decay_db():
    """Restores the SystemConfig rows the idle decay tests touch and removes their BUY rows afterwards."""
    from sqlalchemy import delete
    await init_db()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(SystemConfig).where(SystemConfig.key.in_(_IDLE_DECAY_KEYS)))).scalars().all()
        saved = {r.key: r.value for r in rows}
    yield
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Trade).where(Trade.ticker == "IDLE_BUY"))
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == "IDLE_BUY"))
        await session.execute(delete(Position).where(Position.ticker == "IDLE_BUY"))
        await session.execute(delete(SystemConfig).where(SystemConfig.key.in_(_IDLE_DECAY_KEYS)))
        session.add_all([SystemConfig(key=k, value=v) for k, v in saved.items()])
        await session.commit()


async def _arrange_idle_decay(monkeypatch, buy_days_ago=None, calibration_days_ago=None, min_score=78, trig_score=84,
                              trigger_days_ago=None, open_position=False):
    """Fresh state for decay_idle_thresholds: other tests share this DB and the settings object."""
    from sqlalchemy import delete
    monkeypatch.setattr(settings, "MIN_CONSENSUS_SCORE", min_score)
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", trig_score)
    monkeypatch.setattr(settings, "AUTO_BUY_ENABLED", True)
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", False)
    monkeypatch.setattr(settings, "LEARNER_IDLE_DECAY_DAYS", 7)
    now = datetime.datetime.utcnow()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Trade).where(Trade.order_type == "BUY", Trade.is_live == False))
        await session.execute(delete(OrderTrigger).where(OrderTrigger.is_live == False))
        await session.execute(delete(Position).where(Position.status == "OPEN", Position.is_live == False))
        await session.execute(delete(SystemConfig).where(SystemConfig.key.in_(["LAST_CALIBRATION_TIMESTAMP", "LAST_IDLE_DECAY_DATE"])))
        if trigger_days_ago is not None:
            session.add(OrderTrigger(
                ticker="IDLE_BUY", target_price=100.0, current_price_at_creation=100.0, allocation_eur=100.0,
                status="PENDING", is_live=False, created_at=now - datetime.timedelta(days=trigger_days_ago)
            ))
        if open_position:
            session.add(Position(
                ticker="IDLE_BUY", shares=1.0, entry_price_native=100.0, current_price_native=100.0,
                highest_price_native=100.0, entry_price_eur=100.0, current_price_eur=100.0,
                cost_basis_eur=100.0, status="OPEN", is_live=False
            ))
        if buy_days_ago is not None:
            session.add(Trade(
                ticker="IDLE_BUY", name="Idle Corp", order_type="BUY", shares=1.0, currency="EUR",
                price_native=100.0, price_eur=100.0, total_eur=100.0, is_live=False,
                executed_at=now - datetime.timedelta(days=buy_days_ago)
            ))
        if calibration_days_ago is not None:
            session.add(SystemConfig(key="LAST_CALIBRATION_TIMESTAMP",
                                     value=(now - datetime.timedelta(days=calibration_days_ago)).isoformat()))
        await session.commit()


async def _db_scores():
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(SystemConfig).where(
            SystemConfig.key.in_(["MIN_CONSENSUS_SCORE", "TRIGGER_MIN_SCORE"])))).scalars().all()
        return {r.key: r.value for r in rows}


@pytest.mark.asyncio
async def test_idle_decay_lowers_scores_once_per_day(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10)
    res = await trade_learner.decay_idle_thresholds()
    assert res["success"] is True and len(res["adjustments"]) == 2
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (77, 83)
    assert await _db_scores() == {"MIN_CONSENSUS_SCORE": "77", "TRIGGER_MIN_SCORE": "83"}

    res2 = await trade_learner.decay_idle_thresholds()
    assert res2["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (77, 83)
    assert await _db_scores() == {"MIN_CONSENSUS_SCORE": "77", "TRIGGER_MIN_SCORE": "83"}


@pytest.mark.asyncio
async def test_calibration_keeps_scores_when_auto_tune_off(monkeypatch, idle_decay_db):
    from sqlalchemy import delete
    await init_db()
    monkeypatch.setattr(settings, "LEARNER_MIN_TRADES", 3)
    monkeypatch.setattr(settings, "SCORE_AUTO_TUNE", False)
    monkeypatch.setattr(settings, "MIN_CONSENSUS_SCORE", 70)
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", 76)
    keys = ["MIN_CONSENSUS_SCORE", "TRIGGER_MIN_SCORE", "BREAKEVEN_TRIGGER_PCT"]
    async with AsyncSessionLocal() as session:
        saved = {r.key: r.value for r in (await session.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))).scalars().all()}
        await session.execute(delete(SystemConfig).where(SystemConfig.key.in_(keys)))
        session.add_all([SystemConfig(key="MIN_CONSENSUS_SCORE", value="70"), SystemConfig(key="TRIGGER_MIN_SCORE", value="76")])
        for i in range(3):
            session.add(Trade(
                ticker=f"AUTOTUNE_LOSS_{i}", name="Loss", order_type="SELL", shares=5.0, currency="EUR",
                price_native=95.0, price_eur=95.0, total_eur=475.0, realized_pnl_eur=-25.0, realized_pnl_pct=-5.0,
                rationale="Stop-Loss triggered", executed_at=datetime.datetime.utcnow() + datetime.timedelta(minutes=i)))
        await session.commit()
    try:
        def user_moves_sliders():
            # Runs after calibration read the configs: simulates a slider change during a slow LLM call
            import sqlite3
            from src.db.session import engine
            con = sqlite3.connect(engine.url.database)
            con.execute("UPDATE system_configs SET value='73' WHERE key='MIN_CONSENSUS_SCORE'")
            con.execute("UPDATE system_configs SET value='79' WHERE key='TRIGGER_MIN_SCORE'")
            con.commit()
            con.close()
            return False

        with patch("src.agents.base.llm_client.has_active_key", side_effect=user_moves_sliders):
            res = await trade_learner.calibrate_internal_parameters()
        assert res["success"] is True and res["consecutive_losses"] >= 3
        assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (70, 76)
        assert res["calibrated_parameters"]["MIN_CONSENSUS_SCORE"] == 70
        assert any("fixed by the user" in a for a in res["adjustments"])
        assert not any("Losing streak" in a for a in res["adjustments"])
        assert await _db_scores() == {"MIN_CONSENSUS_SCORE": "73", "TRIGGER_MIN_SCORE": "79"}
        async with AsyncSessionLocal() as session:
            assert (await session.execute(select(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT"))).scalars().first() is not None
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Trade).where(Trade.ticker.like("AUTOTUNE_LOSS_%")))
            await session.execute(delete(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT"))
            if "BREAKEVEN_TRIGGER_PCT" in saved:
                session.add(SystemConfig(key="BREAKEVEN_TRIGGER_PCT", value=saved["BREAKEVEN_TRIGGER_PCT"]))
            await session.commit()


@pytest.mark.asyncio
async def test_idle_decay_skips_when_auto_tune_off(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10)
    monkeypatch.setattr(settings, "SCORE_AUTO_TUNE", False)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)


@pytest.mark.asyncio
async def test_idle_decay_skips_after_recent_buy(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=1)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)


@pytest.mark.asyncio
async def test_idle_decay_waits_after_recent_calibration(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10, calibration_days_ago=1)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)


@pytest.mark.asyncio
async def test_idle_decay_never_goes_below_or_raises_to_defaults(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10, min_score=70, trig_score=80)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (70, 80)

    await _arrange_idle_decay(monkeypatch, buy_days_ago=10, min_score=65, trig_score=84)
    res = await trade_learner.decay_idle_thresholds()
    assert res["success"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (65, 83)


@pytest.mark.asyncio
async def test_idle_decay_skips_when_bot_cannot_buy(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10)
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    assert (await trade_learner.decay_idle_thresholds())["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)

    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", False)
    monkeypatch.setattr(settings, "AUTO_BUY_ENABLED", False)
    assert (await trade_learner.decay_idle_thresholds())["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)


@pytest.mark.asyncio
async def test_idle_decay_skips_with_recent_armed_trigger(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10, trigger_days_ago=1)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)


@pytest.mark.asyncio
async def test_idle_decay_skips_when_slots_full(monkeypatch, idle_decay_db):
    await _arrange_idle_decay(monkeypatch, buy_days_ago=10, open_position=True)
    monkeypatch.setattr(settings, "MAX_POSITIONS", 1)
    res = await trade_learner.decay_idle_thresholds()
    assert res["skipped"] is True
    assert (settings.MIN_CONSENSUS_SCORE, settings.TRIGGER_MIN_SCORE) == (78, 84)
