"""Task 13: the main bot applies a promoted rule set when it decides on and plans a new automated entry.

Every buy still goes through the broker's own execute_buy (validate_buy_order, fee viability, whole shares,
market hours, kill switch). Default rules must leave today's behaviour untouched.
"""
import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import OrderTrigger, PortfolioSnapshot, Position, SystemConfig, Trade
from src.db.session import AsyncSessionLocal, init_db
from src.trading.main_strategy import get_main_rules, write_main_rules
from src.trading.paper import paper_broker
from src.trading.risk import derive_trigger_levels, risk_guardrail
from src.trading.trigger_gate import evaluate_trigger_verdict

TICKER = "MAINPX"


def _report(action="BUY", score=85, entry=95.0, sl=90.0, tp=110.0, approved=True, trigger_type="LIMIT_PULLBACK"):
    return {
        "ticker": TICKER, "name": "Main Parity", "price": 100.0, "currency": "EUR",
        "action": action, "consensus_score": score, "entry_trigger_price": entry, "trigger_type": trigger_type,
        "trigger_rationale": "setup", "summary": "s", "debate_transcript": "d",
        "technical": {"analysis_summary": "t", "bias": "BULLISH"},
        "fundamental": {"analysis_summary": "f", "valuation": "FAIR", "health": "STRONG"},
        "sentiment": {"sentiment_summary": "s"},
        "risk": {"approved": approved, "take_profit_price": tp, "stop_loss_price": sl,
                 "recommended_size_pct": 0.05, "risk_summary": "Position too risky"},
    }


def _stock(price=100.0):
    return {"ticker": TICKER, "name": "Main Parity", "price": price, "currency": "EUR", "quote_type": "EQUITY",
            "technicals": {"rsi14": 55, "volume_surge_ratio": 1.2, "atr14": 2.0}, "fundamentals": {}}


async def _clean_ticker():
    async with AsyncSessionLocal() as s:
        await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == TICKER))
        await s.execute(delete(Position).where(Position.ticker == TICKER))
        await s.execute(delete(Trade).where(Trade.ticker == TICKER))
        await s.commit()


@pytest.fixture(autouse=True)
async def _isolated(monkeypatch):
    """Fresh paper cash, no MAX positions pressure from other modules, and default main rules afterwards."""
    await init_db()
    await _clean_ticker()
    started = datetime.datetime.utcnow()
    for attr in ("TRIGGER_MIN_SCORE", "MAX_POSITIONS", "RISK_PER_TRADE_PCT", "TRAILING_STOP_ATR_MULT",
                 "REGIME_FILTER_ENABLED", "KILL_SWITCH_ACTIVE", "MAX_SINGLE_ORDER_EUR", "AUTO_BUY_ENABLED"):
        monkeypatch.setattr(settings, attr, getattr(settings, attr))
    monkeypatch.setattr(settings, "ENFORCE_MARKET_HOURS", False)
    monkeypatch.setattr(settings, "MAX_POSITIONS", 50)
    async with AsyncSessionLocal() as s:
        await s.execute(delete(SystemConfig).where(SystemConfig.key.in_(["AUTO_BUY_ENABLED", "TRIGGER_MIN_SCORE"])))
        s.add(PortfolioSnapshot(cash_eur=10000.0, equity_eur=0.0, total_value_eur=10000.0, open_positions_count=0,
                                is_live=False, timestamp=datetime.datetime.utcnow() + datetime.timedelta(seconds=1)))
        await s.commit()
    yield
    await _clean_ticker()
    async with AsyncSessionLocal() as s:
        # Drop the paper cash snapshots this test added (its own, future-dated so they win, and those its buys wrote)
        await s.execute(delete(PortfolioSnapshot).where(
            PortfolioSnapshot.is_live == False, PortfolioSnapshot.timestamp >= started))
        await s.execute(delete(SystemConfig).where(SystemConfig.key.like("MAIN\\_%", escape="\\")))
        await s.commit()
    assert (await get_main_rules()).version == 0


async def _run_session(report, price=100.0):
    from src.scheduler.jobs import trading_scheduler
    cands = [{"ticker": TICKER, "name": "Main Parity", "pre_score": 85}]

    async def evaluate(data, total):
        return dict(report)

    follow = AsyncMock()
    with patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
         patch("src.scheduler.jobs.market_scanner.scan_universe", return_value=cands), \
         patch("src.scheduler.jobs.market_data_provider.get_stock_data", return_value=_stock(price)), \
         patch("src.scheduler.jobs.investment_committee.evaluate_ticker", side_effect=evaluate), \
         patch("src.scheduler.jobs.shadow_portfolios.follow_report", follow), \
         patch("src.scheduler.jobs.review_tracker.compare_past_week_reviews", AsyncMock(return_value={})), \
         patch("src.scheduler.jobs.review_tracker.save_session_review", AsyncMock()), \
         patch("src.scheduler.jobs.telegram_bot.notify_market_open", AsyncMock(return_value="")), \
         patch("src.scheduler.jobs.telegram_bot.notify_trigger_armed", AsyncMock()), \
         patch("src.scheduler.jobs.telegram_bot.notify_trigger_executed", AsyncMock()):
        await trading_scheduler.run_market_session(market="EU", session="OPEN")
    return follow


async def _pending():
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(OrderTrigger).where(OrderTrigger.ticker == TICKER, OrderTrigger.status == "PENDING"))
        return res.scalars().all()


async def _positions():
    async with AsyncSessionLocal() as s:
        res = await s.execute(select(Position).where(Position.ticker == TICKER, Position.status == "OPEN"))
        return res.scalars().all()


# --- Verdict gate -------------------------------------------------------------------------------

async def test_default_rules_give_the_same_verdict_as_no_rules():
    rules = await get_main_rules()
    reports = [
        _report(), _report(action="HOLD"), _report(action="SELL"), _report(score=60), _report(score=75),
        _report(approved=False), _report(approved=False, sl=None), _report(action="WATCH"), _report(action=None),
    ]
    for rep in reports:
        for force in (False, True):
            assert evaluate_trigger_verdict(rep, 80, force=force, rules=rules) == evaluate_trigger_verdict(rep, 80, force=force)


async def test_rules_keep_sell_and_min_consensus_hard_blocks():
    await write_main_rules({"accept_hold": True, "ignore_risk_veto": True}, promoted_from="test")
    rules = await get_main_rules()
    assert evaluate_trigger_verdict(_report(action="SELL", score=90), 80, rules=rules).code == "SELL_VERDICT"
    low = evaluate_trigger_verdict(_report(action="HOLD", score=settings.MIN_CONSENSUS_SCORE - 1), 50, force=True, rules=rules)
    assert low.code == "BELOW_MIN_CONSENSUS"
    assert evaluate_trigger_verdict(_report(action="HOLD", score=79), 80, rules=rules).code == "LOW_SCORE"
    # Stop must be > 0 and below the entry
    for sl in (None, 0, 95.0, 96.0, float("nan")):
        assert evaluate_trigger_verdict(_report(approved=False, sl=sl), 80, rules=rules).code == "RISK_VETO"


# --- A: level shaping -----------------------------------------------------------------------------

async def test_default_rules_arm_identical_trigger():
    # A tight stop (0.2% below entry) and no target: plan_entry would change both, so default rules must skip it
    report = _report(entry=95.0, sl=94.8, tp=None)
    expected = derive_trigger_levels(report, 100.0)
    await _run_session(report)
    (trig,) = await _pending()
    assert (trig.trigger_type, trig.target_price, trig.stop_loss, trig.take_profit) == expected
    assert (trig.trigger_type, trig.target_price, trig.stop_loss, trig.take_profit) == ("LIMIT_PULLBACK", 95.0, 94.8, None)
    assert trig.rules_version is None


async def test_pullback_and_stop_scale_shift_levels():
    version = await write_main_rules({"pullback_scale": 0.5, "stop_scale": 1.25}, promoted_from="test")
    await _run_session(_report(entry=95.0, sl=90.0, tp=110.0))
    (trig,) = await _pending()
    assert trig.trigger_type == "LIMIT_PULLBACK"
    assert trig.target_price == 97.5
    assert trig.stop_loss == 91.25
    assert trig.take_profit == 110.0
    assert trig.rules_version == version


async def test_reward_risk_and_no_take_profit():
    await write_main_rules({"reward_risk": 3.0}, promoted_from="test")
    await _run_session(_report(entry=95.0, sl=90.0, tp=130.0))
    (trig,) = await _pending()
    assert (trig.target_price, trig.stop_loss, trig.take_profit) == (95.0, 90.0, 110.0)  # 95 + 3 x 5, not 130
    await _clean_ticker()

    await write_main_rules({"reward_risk": None, "use_take_profit": False}, promoted_from="test")
    await _run_session(_report(entry=95.0, sl=90.0, tp=110.0))
    (trig,) = await _pending()
    assert (trig.target_price, trig.stop_loss, trig.take_profit) == (95.0, 90.0, None)


async def test_reward_risk_sets_target_from_stop_distance():
    await write_main_rules({"reward_risk": 2.0}, promoted_from="test")
    await _run_session(_report(entry=95.0, sl=90.0, tp=150.0))
    (trig,) = await _pending()
    assert trig.take_profit == 105.0  # 95 + 2 x 5, not the committee's 150


# --- B: market entry ------------------------------------------------------------------------------

async def test_market_entry_buys_now_through_execute_buy():
    version = await write_main_rules({"entry_mode": "market"}, promoted_from="test")
    spy = AsyncMock(wraps=paper_broker.execute_buy)
    with patch.object(paper_broker, "execute_buy", spy):
        await _run_session(_report(entry=95.0, sl=90.0, tp=110.0))
    assert spy.await_count == 1
    kw = spy.await_args.kwargs
    assert kw["is_manual"] is False and kw["price_native"] == 100.0
    assert (kw["stop_loss_native"], kw["take_profit_native"]) == (95.0, 110.0)  # stop distance 5 kept below the market price
    (pos,) = await _positions()
    assert pos.rules_version == version
    assert pos.entry_price_native == 100.0
    assert await _pending() == []


async def test_market_entry_cannot_afford_one_share_places_nothing():
    await write_main_rules({"entry_mode": "market"}, promoted_from="test")
    spy = AsyncMock(wraps=paper_broker.execute_buy)
    report = _report(entry=95000.0, sl=90000.0, tp=110000.0)
    report["price"] = 100000.0
    with patch.object(paper_broker, "execute_buy", spy):
        await _run_session(report, price=100000.0)
    assert await _positions() == []
    assert await _pending() == []
    assert spy.await_count == 0  # the one-share check stops it before any order


async def test_market_entry_fee_unviable_places_nothing():
    await write_main_rules({"entry_mode": "market"}, promoted_from="test")
    results = []
    real = paper_broker.execute_buy

    async def spy(**kw):
        res = await real(**kw)
        results.append(res)
        return res

    unviable = {"viable": False, "reason": "fees eat the profit"}
    with patch.object(paper_broker, "execute_buy", side_effect=spy), \
         patch("src.trading.fees.evaluate_trade_economic_viability", return_value=unviable):
        await _run_session(_report(entry=95.0, sl=90.0, tp=110.0))
    assert len(results) == 1 and results[0]["success"] is False
    assert "Fee guardrail" in results[0]["message"]
    assert await _positions() == []
    assert await _pending() == []


# --- C: accept_hold -------------------------------------------------------------------------------

async def test_accept_hold_arms_on_hold_above_threshold():
    await _run_session(_report(action="HOLD", score=85))
    assert await _pending() == []

    await write_main_rules({"accept_hold": True}, promoted_from="test")
    await _run_session(_report(action="HOLD", score=85))
    assert len(await _pending()) == 1
    await _clean_ticker()

    await _run_session(_report(action="HOLD", score=79))  # below the trigger threshold of 80
    assert await _pending() == []


# --- D: ignore_risk_veto ---------------------------------------------------------------------------

async def test_ignore_risk_veto_arms_when_stop_present():
    await _run_session(_report(approved=False, sl=90.0))
    assert await _pending() == []

    await write_main_rules({"ignore_risk_veto": True}, promoted_from="test")
    await _run_session(_report(approved=False, sl=90.0))
    (trig,) = await _pending()
    assert trig.stop_loss == 90.0


async def test_ignore_risk_veto_without_stop_is_rejected():
    await write_main_rules({"ignore_risk_veto": True}, promoted_from="test")
    for sl in (None, 0.0):
        await _run_session(_report(approved=False, sl=sl))
        assert await _pending() == []
        assert await _positions() == []


async def test_ignore_risk_veto_with_kill_switch_on_places_nothing(monkeypatch):
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    # Market entry: the buy is attempted now and the kill switch refuses it
    await write_main_rules({"ignore_risk_veto": True, "entry_mode": "market"}, promoted_from="test")
    results = []
    real = paper_broker.execute_buy

    async def spy(**kw):
        res = await real(**kw)
        results.append(res)
        return res

    with patch.object(paper_broker, "execute_buy", side_effect=spy):
        await _run_session(_report(approved=False, sl=90.0))
    assert len(results) == 1 and "kill switch" in results[0]["message"].lower()
    assert await _positions() == []

    # Trigger entry: the trigger may arm (as today), but firing it buys nothing
    await write_main_rules({"entry_mode": "trigger"}, promoted_from="test")
    await _run_session(_report(approved=False, sl=90.0))
    assert len(await _pending()) == 1
    with patch("src.trading.paper.market_data_provider.get_stock_data", return_value=_stock(94.0)):
        executed = await paper_broker.check_and_execute_triggers()
    assert [e for e in executed if e.get("ticker") == TICKER] == []
    assert await _positions() == []


async def test_ignore_risk_veto_still_fails_validate_buy_order(monkeypatch):
    await write_main_rules({"ignore_risk_veto": True, "entry_mode": "market"}, promoted_from="test")
    monkeypatch.setattr(settings, "MAX_SINGLE_ORDER_EUR", 25.0)  # one share at 100 can never fit this cap
    results = []
    real = paper_broker.execute_buy

    async def spy(**kw):
        res = await real(**kw)
        results.append(res)
        return res

    with patch.object(paper_broker, "execute_buy", side_effect=spy):
        await _run_session(_report(approved=False, sl=90.0))
    assert await _positions() == []
    assert await _pending() == []
    assert all(not r["success"] for r in results)

    # A check only validate_buy_order makes (automated buying paused): the buy is attempted and refused there
    monkeypatch.setattr(settings, "MAX_SINGLE_ORDER_EUR", 10000.0)
    monkeypatch.setattr(settings, "AUTO_BUY_ENABLED", False)
    with patch.object(paper_broker, "execute_buy", side_effect=spy), \
         patch("src.trading.paper.risk_guardrail.validate_buy_order", wraps=risk_guardrail.validate_buy_order) as vbo:
        await _run_session(_report(approved=False, sl=90.0))
    assert vbo.call_count == 1
    assert results and "paused" in results[-1]["message"]
    assert await _positions() == []
    monkeypatch.setattr(settings, "AUTO_BUY_ENABLED", True)

    # No cash at all: nothing is bought either
    monkeypatch.setattr(settings, "MAX_SINGLE_ORDER_EUR", 10000.0)
    async with AsyncSessionLocal() as s:
        s.add(PortfolioSnapshot(cash_eur=0.0, equity_eur=0.0, total_value_eur=0.0, open_positions_count=0,
                                is_live=False, timestamp=datetime.datetime.utcnow() + datetime.timedelta(seconds=2)))
        await s.commit()
    with patch.object(paper_broker, "execute_buy", side_effect=spy):
        await _run_session(_report(approved=False, sl=90.0))
    assert await _positions() == []
    assert await _pending() == []


async def test_accept_hold_trigger_survives_redebate_only_under_the_rules():
    from src.scheduler.jobs import trading_scheduler
    await write_main_rules({"accept_hold": True}, promoted_from="test")
    rules = await get_main_rules()
    await _run_session(_report(action="HOLD", score=85))
    assert len(await _pending()) == 1
    assert await trading_scheduler._cancel_downgraded_triggers(TICKER, _report(action="HOLD", score=85), 80, rules) is False
    assert len(await _pending()) == 1
    # A trigger not armed under the rules (manual or default) keeps today's gate, even while the rules are active
    async with AsyncSessionLocal() as s:
        (trig,) = (await s.execute(select(OrderTrigger).where(
            OrderTrigger.ticker == TICKER, OrderTrigger.status == "PENDING"))).scalars().all()
        trig.rules_version = None
        await s.commit()
    assert await trading_scheduler._cancel_downgraded_triggers(TICKER, _report(action="HOLD", score=85), 80, rules) is True
    assert await _pending() == []


async def test_opportunity_scan_market_entry_buys_through_execute_buy():
    from src.scheduler.jobs import trading_scheduler
    version = await write_main_rules({"entry_mode": "market"}, promoted_from="test")
    status = {"eu_market": {"is_open": True}, "us_market": {"is_open": False}}
    spy = AsyncMock(wraps=paper_broker.execute_buy)
    with patch("src.data.market_hours.get_market_statuses", return_value=status), \
         patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
         patch("src.scheduler.jobs.market_scanner.scan_universe", return_value=[{"ticker": TICKER}]), \
         patch("src.scheduler.jobs.market_data_provider.get_stock_data", return_value=_stock()), \
         patch("src.scheduler.jobs.investment_committee.cached_report", return_value=None), \
         patch("src.scheduler.jobs.investment_committee.evaluate_ticker", AsyncMock(return_value=_report())), \
         patch("src.scheduler.jobs.shadow_portfolios.follow_report", AsyncMock()) as follow, \
         patch("src.scheduler.jobs.telegram_bot.notify_trigger_executed", AsyncMock()), \
         patch.object(paper_broker, "execute_buy", spy):
        await trading_scheduler.scan_market_opportunities()
    assert spy.await_count == 1 and spy.await_args.kwargs["is_manual"] is False
    (pos,) = await _positions()
    assert pos.rules_version == version
    assert await _pending() == []
    assert follow.await_args.kwargs["followed_main"] is True
