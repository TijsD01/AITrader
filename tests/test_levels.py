import datetime

import pytest

from src.trading import levels, shadow
from src.trading.levels import max_hold_exceeded, pct_trail_stop, plan_entry
from src.trading.shadow import PRESETS


def test_pct_trail_stop_raises_lowers_never():
    assert pct_trail_stop(90.0, 120.0, 0.10) == 108.0
    assert pct_trail_stop(110.0, 120.0, 0.10) == 110.0
    assert pct_trail_stop(None, 100.0, 0.05) == 95.0


def test_pct_trail_stop_off_keeps_current_stop():
    assert pct_trail_stop(90.0, 120.0, None) == 90.0
    assert pct_trail_stop(90.0, 120.0, 0) == 90.0
    assert pct_trail_stop(None, 120.0, None) is None


def test_max_hold_exceeded():
    opened = datetime.datetime(2026, 1, 1, 12, 0)
    assert max_hold_exceeded(opened, opened + datetime.timedelta(days=21), 20) is True
    assert max_hold_exceeded(opened, opened + datetime.timedelta(days=19), 20) is False
    assert max_hold_exceeded(opened, opened + datetime.timedelta(days=20), 20) is False  # strictly greater
    assert max_hold_exceeded(opened, opened + datetime.timedelta(days=21), None) is False
    assert max_hold_exceeded(None, opened, 20) is False


def test_plan_entry_presets():
    assert plan_entry(PRESETS["baseline"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0) == {
        "trigger_type": "LIMIT_PULLBACK", "target_price": 95.0, "stop_loss": 90.0, "take_profit": 110.0}
    assert plan_entry(PRESETS["aggressive"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0) == {
        "trigger_type": "LIMIT_PULLBACK", "target_price": 97.5, "stop_loss": 91.25, "take_profit": 116.25}
    assert plan_entry(PRESETS["momentum"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0) == {
        "trigger_type": "MARKET", "target_price": 100.0, "stop_loss": 95.0, "take_profit": None}


def test_shadow_reexports_the_same_functions():
    assert shadow.plan_entry is levels.plan_entry
    assert shadow.effective_breakeven_pct is levels.effective_breakeven_pct


@pytest.mark.parametrize("key", list(PRESETS))
@pytest.mark.parametrize("args", [
    ("LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0),
    ("BREAKOUT", 105.0, 98.0, 120.0, 100.0),
    ("LIMIT_PULLBACK", 95.0, None, None, 100.0),
    ("BREAKOUT", 50.0, 49.9, 49.0, 48.0),
])
def test_plan_entry_parity_with_shadow(key, args):
    assert levels.plan_entry(PRESETS[key], *args) == shadow.plan_entry(PRESETS[key], *args)


@pytest.mark.parametrize("pct", [float("nan"), float("inf"), -0.1, 1.0, 1.5, "0.1"])
def test_pct_trail_stop_out_of_range_pct_keeps_current_stop(pct):
    assert pct_trail_stop(90.0, 120.0, pct) == 90.0
    assert pct_trail_stop(None, 120.0, pct) is None


@pytest.mark.parametrize("highest", [float("nan"), float("inf"), 0.0, -5.0, None])
def test_pct_trail_stop_bad_highest_keeps_current_stop(highest):
    assert pct_trail_stop(90.0, highest, 0.10) == 90.0


@pytest.mark.parametrize("days", [-1, 0, float("nan"), "20"])
def test_max_hold_exceeded_non_positive_or_bad_days_is_false(days):
    opened = datetime.datetime(2026, 1, 1, 12, 0)
    assert max_hold_exceeded(opened, opened + datetime.timedelta(days=400), days) is False


def test_max_hold_exceeded_mixes_aware_and_naive_utc():
    naive = datetime.datetime(2026, 1, 1, 12, 0)
    aware_later = datetime.datetime(2026, 1, 22, 12, 0, tzinfo=datetime.timezone.utc)
    assert max_hold_exceeded(naive, aware_later, 20) is True
    assert max_hold_exceeded(aware_later, naive, 20) is False
    aware_cet = datetime.datetime(2026, 1, 1, 13, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
    assert max_hold_exceeded(aware_cet, naive + datetime.timedelta(days=20, seconds=1), 20) is True
