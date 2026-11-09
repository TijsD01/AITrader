"""
Property checks for src/trading/risk.py: a bad quote (NaN, inf, zero, negative) must never
approve a buy, fire a trigger, size a position or move a stop. NaN compares False with
everything, so a guard written as "x <= 0" lets it through.
"""
import itertools
import math

import pytest

from config.settings import settings
from src.trading import risk

NAN, INF = float("nan"), float("inf")
NOT_FINITE = [NAN, INF, -INF]
NOT_A_PRICE = NOT_FINITE + [0.0, -5.0]


@pytest.fixture(autouse=True)
def _no_clock(monkeypatch):
    monkeypatch.setattr(settings, "ENFORCE_MARKET_HOURS", False)
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", False)


def _buy(**over):
    args = dict(
        ticker="ASML.AS", current_positions=[], cash_eur=5000.0, portfolio_total_eur=10000.0,
        allocation_eur=1000.0, consensus_score=90, auto_buy_enabled=True,
        price_native=100.0, currency="EUR", stop_loss_native=96.0, take_profit_native=108.0,
    )
    args.update(over)
    return risk.validate_buy_order(**args)


def test_valid_buy_is_approved():
    assert _buy() == (True, "Approved")


@pytest.mark.parametrize("field,bad", [
    *itertools.product(["cash_eur", "allocation_eur", "available_cash_eur"], NOT_FINITE),
    *itertools.product(["price_native"], NOT_A_PRICE),
    *itertools.product(["stop_loss_native", "take_profit_native"], NOT_FINITE),
])
def test_buy_with_bad_number_is_rejected(field, bad):
    ok, msg = _buy(**{field: bad})
    assert not ok, f"{field}={bad} was approved: {msg}"


@pytest.mark.parametrize("trigger_type", risk.VALID_TRIGGER_TYPES)
@pytest.mark.parametrize("bad", NOT_A_PRICE)
def test_bad_quote_neither_fires_nor_cancels_a_trigger(trigger_type, bad):
    # A zero quote used to read as "below the stop" and cancel a healthy pullback trigger
    assert risk.evaluate_trigger_condition(trigger_type, 100.0, bad, stop_loss=95.0, take_profit=110.0) == (False, None)
    assert risk.evaluate_trigger_condition(trigger_type, bad, 100.0, stop_loss=95.0, take_profit=110.0) == (False, None)


def test_flash_crash_gap_through_stop_cancels_instead_of_buying():
    met, reason = risk.evaluate_trigger_condition("LIMIT_PULLBACK", 100.0, 80.0, stop_loss=95.0)
    assert not met and reason


@pytest.mark.parametrize("bad", NOT_FINITE)
def test_bad_trigger_levels_are_invalid(bad):
    assert risk.validate_trigger_levels("LIMIT_PULLBACK", bad, 100.0, None, None)
    assert risk.validate_trigger_levels("LIMIT_PULLBACK", 98.0, bad, None, None)
    assert risk.validate_trigger_levels("LIMIT_PULLBACK", 98.0, 100.0, bad, None)
    assert risk.validate_trigger_levels("LIMIT_PULLBACK", 98.0, 100.0, None, bad)


@pytest.mark.parametrize("bad", NOT_FINITE)
def test_allocation_is_zero_for_bad_cash(bad):
    assert risk.calculate_position_allocation(bad) == 0.0
    assert risk.get_effective_deployable_cash(bad) == (0.0, 0.0)


@pytest.mark.parametrize("alloc,price", [*itertools.product(NOT_A_PRICE, [50.0]), *itertools.product([1000.0], NOT_A_PRICE)])
def test_share_counts_are_zero_for_bad_inputs(alloc, price):
    assert risk.whole_share_count(alloc, price) == 0
    assert risk.affordable_share_count(alloc, price, 1000.0, 10000.0) == 0


@pytest.mark.parametrize("whole", [True, False])
@pytest.mark.parametrize("field,bad", [
    *itertools.product(["allocation_eur", "price_native"], NOT_A_PRICE),
    *itertools.product(["stop_loss_native", "portfolio_value_eur"], NOT_FINITE),
])
def test_plan_buy_buys_nothing_on_bad_input(monkeypatch, whole, field, bad):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", whole)
    args = dict(allocation_eur=1000.0, price_native=100.0, currency="EUR", stop_loss_native=96.0, portfolio_value_eur=10000.0)
    args[field] = bad
    plan = risk.plan_buy(**args)
    assert plan["shares"] == 0 and plan["cost_eur"] == 0, plan


@pytest.mark.parametrize("whole", [True, False])
@pytest.mark.parametrize("alloc,price,stop", itertools.product([20.0, 333.33, 5000.0], [0.37, 101.37, 4321.0], [0.80, 0.96, 0.99]))
def test_plan_buy_never_spends_more_than_free_cash(monkeypatch, whole, alloc, price, stop):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", whole)
    plan = risk.plan_buy(alloc, price, "EUR", price * stop, 10000.0, max_eur=alloc)
    assert math.isfinite(plan["shares"]) and plan["shares"] >= 0
    assert plan["cost_eur"] <= alloc + 0.01
    assert plan["budget_eur"] <= alloc + 0.005  # the risk cap only ever shrinks a buy


@pytest.mark.parametrize("field,bad", itertools.product(["entry_price", "highest_price"], NOT_FINITE))
def test_trailing_stop_never_moves_on_a_bad_price(field, bad):
    args = dict(entry_price=100.0, current_stop=96.0, highest_price=110.0, atr=2.0, breakeven_trigger_pct=0.03, atr_mult=2.5)
    args[field] = bad
    assert risk.compute_trailing_stop(**args) == 96.0


@pytest.mark.parametrize("bad", NOT_FINITE)
def test_bad_atr_still_gives_the_breakeven_stop(bad):
    # Breakeven needs no ATR; a broken ATR must not block it or produce a non-finite stop
    assert risk.compute_trailing_stop(100.0, 96.0, 110.0, bad, 0.03, 2.5) == 101.0


@pytest.mark.parametrize("high", [100.0, 103.0, 110.0, 150.0, 1000.0])
@pytest.mark.parametrize("stop", [None, 90.0, 96.0, 104.0])
def test_trailing_stop_never_lowers(high, stop):
    new = risk.compute_trailing_stop(100.0, stop, high, 2.0, 0.03, 2.5)
    assert stop is None or new >= stop
