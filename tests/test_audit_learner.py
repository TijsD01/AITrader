"""Regression tests for the learner audit: step clamps, score floors, rule-based gates, mode and manual filters."""
import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, func, select

from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position, TradeLesson, SystemConfig
from src.trading.learner import trade_learner, _step, _int_step


def test_step_leaves_a_value_outside_its_range_alone():
    # A human or a promote put it there: the learner must not pull it back in one calibration
    assert _int_step(95, 95, 68, 85) == 95
    assert _int_step(95, 70, 68, 85) == 95
    assert _step(10.0, 10.0, 0.015, 0.05) == 10.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_step_ignores_a_non_finite_proposal(bad):
    assert _step(2.1, bad, 1.5, 3.0) == 2.1
    assert _int_step(74, bad, 68, 85) == 74


@pytest.fixture
async def audit_db(monkeypatch):
    """Default calibration rows and no pause; restores SystemConfig and removes the AUD rows afterwards."""
    await init_db()
    monkeypatch.setattr(settings, "LEARNER_MIN_TRADES", 3)
    monkeypatch.setattr(settings, "SCORE_AUTO_TUNE", True)
    # Calibration writes these into settings; monkeypatch puts them back
    monkeypatch.setattr(settings, "MIN_CONSENSUS_SCORE", settings.MIN_CONSENSUS_SCORE)
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", settings.TRIGGER_MIN_SCORE)
    async with AsyncSessionLocal() as session:
        saved = {r.key: r.value for r in (await session.execute(select(SystemConfig))).scalars().all()}
        await session.execute(delete(SystemConfig))
        await session.commit()
    await _set_rows(MIN_CONSENSUS_SCORE="70", TRIGGER_MIN_SCORE="80")
    yield
    async with AsyncSessionLocal() as session:
        for model in (Trade, Position, TradeLesson):
            await session.execute(delete(model).where(model.ticker.like("AUD%")))
        await session.execute(delete(SystemConfig))
        session.add_all([SystemConfig(key=k, value=v) for k, v in saved.items()])
        await session.commit()


async def _set_rows(**rows):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(SystemConfig).where(SystemConfig.key.in_(list(rows))))
        session.add_all([SystemConfig(key=k, value=v) for k, v in rows.items()])
        await session.commit()


async def _rows():
    async with AsyncSessionLocal() as session:
        return {r.key: r.value for r in (await session.execute(select(SystemConfig))).scalars().all()}


async def _add_sells(pnls, **fields):
    """Sells newer than every trade in the DB (oldest first in pnls); losses are stop-outs. Returns the newest one."""
    async with AsyncSessionLocal() as session:
        latest = (await session.execute(select(func.max(Trade.executed_at)))).scalar()
        start = max(latest or datetime.datetime.utcnow(), datetime.datetime.utcnow()) + datetime.timedelta(days=1)
        for i, pnl in enumerate(pnls):
            trade = Trade(**{**dict(
                ticker=f"AUD{i}", name="Audit", order_type="SELL", shares=1.0, currency="EUR", price_native=100.0,
                price_eur=100.0, total_eur=100.0, realized_pnl_eur=pnl, realized_pnl_pct=pnl,
                rationale="Stop-Loss triggered" if pnl < 0 else "Take-Profit", is_live=False,
                executed_at=start + datetime.timedelta(minutes=i)), **fields})
            session.add(trade)
        await session.commit()
        await session.refresh(trade)
        return trade


async def _add_lesson(trade, text):
    async with AsyncSessionLocal() as session:
        session.add(TradeLesson(trade_id=trade.id, ticker=trade.ticker, outcome="LOSS", lesson=text,
                                created_at=trade.executed_at))
        await session.commit()


async def _calibrate_by_rules(**kwargs):
    with patch("src.agents.base.llm_client.has_active_key", return_value=False):
        return await trade_learner.calibrate_internal_parameters(**kwargs)


async def _calibrate_by_llm(answer):
    llm = AsyncMock(return_value={"adjustment_reasons": ["test"], **answer})
    with patch("src.agents.base.llm_client.has_active_key", return_value=True), \
         patch("src.agents.base.llm_client.generate_json", llm):
        res = await trade_learner.calibrate_internal_parameters()
    return res, llm


async def test_llm_never_moves_a_score_below_its_code_default(audit_db):
    await _add_lesson(await _add_sells([-1.0] * 20), "Audit lesson")
    res, llm = await _calibrate_by_llm({"MIN_CONSENSUS_SCORE": 60, "TRIGGER_MIN_SCORE": 60})
    llm.assert_awaited()
    p = res["calibrated_parameters"]
    assert (p["MIN_CONSENSUS_SCORE"], p["TRIGGER_MIN_SCORE"]) == (70, 80)

    # Above the default the LLM may still lower a score, but not past the default
    await _set_rows(MIN_CONSENSUS_SCORE="71", TRIGGER_MIN_SCORE="84")
    res, _ = await _calibrate_by_llm({"MIN_CONSENSUS_SCORE": 60, "TRIGGER_MIN_SCORE": 60})
    p = res["calibrated_parameters"]
    assert (p["MIN_CONSENSUS_SCORE"], p["TRIGGER_MIN_SCORE"]) == (70, 83)


async def test_rule_calibration_tightens_scores_one_point_per_run(audit_db):
    await _add_sells([-1.0] * 20)
    res = await _calibrate_by_rules()
    rows = await _rows()
    assert (rows["MIN_CONSENSUS_SCORE"], rows["TRIGGER_MIN_SCORE"]) == ("71", "81")
    assert any("70 -> 71" in a and "80 -> 81" in a for a in res["adjustments"])

    await _set_rows(MIN_CONSENSUS_SCORE="82", TRIGGER_MIN_SCORE="84")
    await _calibrate_by_rules()
    rows = await _rows()
    assert (rows["MIN_CONSENSUS_SCORE"], rows["TRIGGER_MIN_SCORE"]) == ("82", "84")


async def test_rule_calibration_does_not_tighten_scores_on_positive_expectancy(audit_db):
    await _add_sells([-1.0] * 13 + [10.0] * 7)  # 35% winners that still pay
    res = await _calibrate_by_rules()
    assert res["win_rate_pct"] < 40 and res["expectancy_pct"] > 0
    rows = await _rows()
    assert (rows["MIN_CONSENSUS_SCORE"], rows["TRIGGER_MIN_SCORE"]) == ("70", "80")


async def test_rule_calibration_keeps_stop_width_and_weights_on_positive_expectancy(audit_db):
    await _add_sells([5.0] * 17 + [-1.0] * 3)  # three newest are stop-outs, the window still pays
    res = await _calibrate_by_rules()
    assert res["consecutive_losses"] == 3 and res["expectancy_pct"] > 0
    p = res["calibrated_parameters"]
    assert p["ATR_STOP_MULTIPLIER"] == 1.75
    assert (p["WEIGHT_RISK"], p["WEIGHT_TECHNICAL"]) == (0.20, 0.35)


async def test_rule_calibration_keeps_weights_summing_to_one(audit_db):
    await _add_sells([-1.0] * 20)
    # Risk is at its 0.30 cap, so only the technical weight moves and the sum would drop to 0.95
    await _set_rows(WEIGHT_TECHNICAL="0.35", WEIGHT_FUNDAMENTAL="0.25", WEIGHT_SENTIMENT="0.10", WEIGHT_RISK="0.30")
    p = (await _calibrate_by_rules())["calibrated_parameters"]
    assert p["ATR_STOP_MULTIPLIER"] == 1.9  # control: a losing window still widens the stop
    assert p["WEIGHT_TECHNICAL"] < 0.35
    total = p["WEIGHT_TECHNICAL"] + p["WEIGHT_FUNDAMENTAL"] + p["WEIGHT_SENTIMENT"] + p["WEIGHT_RISK"]
    assert total == pytest.approx(1.0)


async def test_fallback_lesson_comes_from_the_active_mode(audit_db):
    paper = await _add_sells([-1.0] * 20)
    await _add_lesson(paper, "PAPER LESSON TEXT")
    await _add_lesson(await _add_sells([-1.0], ticker="AUDLIVE", is_live=True), "LIVE LESSON TEXT")
    await _add_sells([-1.0], ticker="AUDNEW")  # newest paper sell has no lesson of its own
    _, llm = await _calibrate_by_llm({})
    prompt = llm.await_args.args[1]
    assert "PAPER LESSON TEXT" in prompt and "LIVE LESSON TEXT" not in prompt


async def test_post_mortem_uses_the_position_of_the_trades_mode(audit_db):
    trade = await _add_sells([-4.0], ticker="AUDPM")
    async with AsyncSessionLocal() as session:
        for hours, is_live in ((72, False), (1, True)):  # the live holding of the same ticker was opened later
            session.add(Position(
                ticker="AUDPM", shares=1, entry_price_native=100.0, current_price_native=100.0,
                highest_price_native=100.0, entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=100.0,
                status="CLOSED", is_live=is_live, opened_at=trade.executed_at - datetime.timedelta(hours=hours)))
        await session.commit()
    with patch("src.agents.base.llm_client.has_active_key", return_value=False):
        await trade_learner.conduct_trade_post_mortem(trade.id)
    async with AsyncSessionLocal() as session:
        lesson = (await session.execute(select(TradeLesson).where(TradeLesson.trade_id == trade.id))).scalars().first()
    assert lesson.holding_period_hours == 72.0


async def test_manual_trades_stay_out_of_calibration(audit_db):
    last_auto = await _add_sells([1.0] * 20)
    await _set_rows(LAST_CALIBRATION_TIMESTAMP=last_auto.executed_at.isoformat())
    manual = await _add_sells([-5.0] * 20, is_manual=True)

    # Only manual sells closed since the last calibration: the automatic run keeps waiting
    res = await _calibrate_by_rules(trade_id=manual.id, automatic=True)
    assert res["success"] is False and res["skipped"] is True

    res = await _calibrate_by_rules()
    assert res["success"] is True
    assert (res["win_count"], res["loss_count"], res["consecutive_losses"]) == (20, 0, 0)
