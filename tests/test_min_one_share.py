"""At least one whole share when cash allows it (the EUR 500 account: EUR 100 slots, EUR 120 shares)."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config.settings import settings
from src.scheduler.jobs import TradingScheduler
from src.trading.risk import allocate_pending_triggers, plan_buy


@pytest.fixture(autouse=True)
def whole_share_settings(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    monkeypatch.setattr(settings, "MAX_POSITION_PCT", 60.0)
    monkeypatch.setattr(settings, "MAX_SINGLE_ORDER_EUR", 10000.0)
    monkeypatch.setattr(settings, "WHOLE_SHARE_ROUND_UP_PCT", 10.0)
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "slot")
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 1.0)
    monkeypatch.setattr(settings, "DEFAULT_STOP_LOSS_PCT", 0.05)


def test_share_above_the_slot_is_bought():
    # EUR 120 share, EUR 100 slot, 4% stop
    plan = plan_buy(100.0, 120.0, "EUR", 115.2, 500.0, max_eur=500.0)
    assert plan["shares"] == 1 and plan["cost_eur"] == 120.0


def test_risk_cap_trims_but_never_kills_the_one_share():
    # EUR 60 share, 8% stop: the slot of 80 is capped to 50 (0 shares) but one share still fits
    plan = plan_buy(80.0, 60.0, "EUR", 55.2, 500.0, max_eur=500.0)
    assert plan["risk_capped"] and plan["budget_eur"] == 50.0
    assert plan["shares"] == 1
    # A bigger slot keeps its trimmed multi-share count
    assert plan_buy(300.0, 60.0, "EUR", 55.2, 500.0, max_eur=500.0)["shares"] == 3


def test_one_share_is_limited_by_max_position_pct():
    assert plan_buy(100.0, 350.0, "EUR", None, 500.0, max_eur=500.0)["shares"] == 0  # 70% of the portfolio
    assert plan_buy(100.0, 300.0, "EUR", None, 500.0, max_eur=500.0)["shares"] == 1  # exactly 60%


def test_one_share_is_limited_by_free_cash():
    assert plan_buy(100.0, 150.0, "EUR", None, 500.0, max_eur=140.0)["shares"] == 0
    assert plan_buy(100.0, 150.0, "EUR", None, 500.0, max_eur=150.0)["shares"] == 1
    # Without a free-cash figure the allocation is all that is known
    assert plan_buy(100.0, 150.0, "EUR", None, 500.0)["shares"] == 0


def test_no_allocation_buys_nothing():
    assert plan_buy(0.0, 50.0, "EUR", None, 500.0, max_eur=500.0)["shares"] == 0


def test_fractional_mode_is_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", False)
    plan = plan_buy(100.0, 150.0, "EUR", None, 500.0, max_eur=500.0)
    assert plan["shares"] == round(100.0 / 150.0, 4)


def test_scheduler_check_agrees_with_the_rule():
    assert TradingScheduler._fits_whole_share(100.0, 120.0, "EUR", 115.2, 500.0, 500.0)
    assert not TradingScheduler._fits_whole_share(100.0, 350.0, "EUR", None, 500.0, 500.0)
    assert not TradingScheduler._fits_whole_share(100.0, 150.0, "EUR", None, 500.0, 140.0)


@pytest.mark.asyncio
async def test_hard_block_lets_a_share_above_the_slot_through():
    from src.trading.trigger_gate import _hard_block
    verdict = SimpleNamespace(action="BUY", consensus_score=80, trigger_min_score=60)
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "ok")):
        ok = await _hard_block("AAA", {"currency": "EUR"}, 120.0, 100.0, verdict, 115.2, 500.0, free_cash_eur=500.0)
        too_pricey = await _hard_block("AAA", {"currency": "EUR"}, 350.0, 100.0, verdict, None, 500.0, free_cash_eur=500.0)
    assert ok is None
    assert too_pricey is not None and too_pricey.code == "SHARE_TOO_EXPENSIVE"


def _trigger(trigger_id, score, price):
    return SimpleNamespace(
        id=trigger_id, ticker=f"T{trigger_id}", currency="EUR", consensus_score=score,
        created_at=datetime(2026, 1, 1), target_price=price, stop_loss=None
    )


def test_two_triggers_on_200_free_cash_fund_only_one_150_share(monkeypatch):
    monkeypatch.setattr(settings, "FULL_BUDGET_UTILIZATION", True)
    monkeypatch.setattr(settings, "MAX_POSITION_PCT", 100.0)
    monkeypatch.setattr(settings, "MAX_POSITIONS", 2)
    allocs = allocate_pending_triggers(200.0, 0, [_trigger(1, 70, 150.0), _trigger(2, 80, 150.0)], portfolio_value_eur=200.0)
    assert allocs == {2: 150.0, 1: 0.0}
    assert sum(allocs.values()) <= 200.0


def test_pending_trigger_reserves_the_cost_of_its_share_not_the_slot(monkeypatch):
    # EUR 500, 5 slots of EUR 100: the EUR 120 share reserves EUR 120, the EUR 60 one its slot of EUR 100
    monkeypatch.setattr(settings, "FULL_BUDGET_UTILIZATION", True)
    monkeypatch.setattr(settings, "MAX_POSITIONS", 5)
    allocs = allocate_pending_triggers(500.0, 0, [_trigger(1, 90, 120.0), _trigger(2, 80, 60.0)], portfolio_value_eur=500.0)
    assert allocs[1] == 120.0
    assert allocs[2] == 100.0


def test_a_share_above_max_position_pct_leaves_the_trigger_unfunded(monkeypatch):
    monkeypatch.setattr(settings, "FULL_BUDGET_UTILIZATION", True)
    monkeypatch.setattr(settings, "MAX_POSITIONS", 5)
    allocs = allocate_pending_triggers(500.0, 0, [_trigger(1, 90, 350.0)], portfolio_value_eur=500.0)
    assert allocs == {1: 0.0}
