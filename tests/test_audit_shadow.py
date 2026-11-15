"""Audit fixes: breakeven lock below the price, learner step clamps, manual learning on new trades only,
shadow risk sizing like the main bot, and presets whose breakeven does not undercut their trail."""
import pytest

from config.settings import settings
from src.db.models import ShadowAccount, ShadowTrigger
from src.db.session import AsyncSessionLocal
from src.trading.levels import effective_breakeven_pct
from src.trading.risk import compute_trailing_stop
from src.trading.shadow import PRESETS, shadow_portfolios
from src.trading.shadow_learner import MANUAL_MIN_TRADES, shadow_learner
from src.trading.strategy_params import SPECS, clamp_step, validate
from tests.test_shadow_learner import KEY, _add_closed, _fresh_store, _learner, _params  # noqa: F401  (autouse: fresh store + stubbed LLM)


# --- 1. Breakeven lock ---------------------------------------------------------------------------

def test_breakeven_lock_default_trigger_is_still_plus_one_percent():
    assert compute_trailing_stop(100.0, 92.0, 102.6, None, 0.025, atr_mult=0) == 101.0
    assert compute_trailing_stop(100.0, 92.0, 102.4, None, 0.025, atr_mult=0) == 92.0


@pytest.mark.parametrize("trigger", [0.005, 0.00625, 0.01, 0.015, 0.02])
def test_breakeven_lock_stays_below_the_price_that_triggered_it(trigger):
    high = round(100.0 * (1 + trigger) + 0.01, 4)  # just past the trigger
    stop = compute_trailing_stop(100.0, 92.0, high, None, trigger, atr_mult=0)
    assert 100.0 < stop < high
    assert stop == round(100.0 * (1 + min(0.01, trigger / 2)), 2)


def test_breakeven_lock_never_lowers_the_stop_and_atr_trail_still_applies():
    assert compute_trailing_stop(100.0, 100.4, 100.5, None, 0.005, atr_mult=0) == 100.4
    assert compute_trailing_stop(100.0, 92.0, 120.0, 2.0, 0.005, atr_mult=3.0) == 114.0


# --- 2. clamp_step -------------------------------------------------------------------------------

@pytest.mark.parametrize("name,old,up,down", [
    ("breakeven_pct", 0.025, 0.03, 0.02),
    ("trailing_stop_pct", 0.08, 0.09, 0.07),
    ("max_positions", 5, 6, 4),
])
def test_lowest_strength_can_move_one_grid_step(name, old, up, down):
    assert clamp_step(name, old, SPECS[name].max, 1) == up
    assert clamp_step(name, old, SPECS[name].min, 1) == down


def test_no_learnable_number_is_stuck_at_any_strength():
    for spec in SPECS.values():
        if spec.learnable and spec.kind in ("int", "float"):
            lo = spec.min if spec.learn_min is None else spec.learn_min
            for strength in (1, 2, 3):
                assert clamp_step(spec.name, lo, spec.max, strength) != lo, (spec.name, strength)


@pytest.mark.parametrize("name", ["use_take_profit", "regime_filter"])
def test_boolean_flips_only_at_the_highest_strength(name):
    assert clamp_step(name, True, False, 1) is True
    assert clamp_step(name, True, False, 2) is True
    assert clamp_step(name, True, False, 3) is False
    assert clamp_step(name, False, True, 3) is True
    assert clamp_step(name, True, "x", 3) is True


@pytest.mark.parametrize("name,floor", [("risk_per_trade_pct", 0.1), ("trailing_atr_mult", 0.5)])
def test_learner_cannot_step_a_zero_means_off_parameter_down_to_off(name, floor):
    value = floor + SPECS[name].step
    for _ in range(5):
        value = clamp_step(name, value, 0.0, 3)
    assert value == floor
    assert clamp_step(name, 0.0, 2.0, 3) == 0.0  # a human switched it off: the learner leaves it off
    assert validate({name: 0.0}) == {name: 0.0}  # a human edit may still set 0


# --- 3. Manual learning needs new trades ---------------------------------------------------------

async def test_manual_learn_does_not_reuse_trades_already_learned_from(_learner):  # noqa: F811
    await _add_closed(10)
    _learner.return_value = {"changes": {"stop_scale": 2.0}}
    assert (await shadow_learner.learn(KEY, manual=True))["success"] is True
    assert _params()["stop_scale"] == 1.15
    res = await shadow_learner.learn(KEY, manual=True)
    assert res["skipped"] is True and "0 new closed trades" in res["message"]
    assert _params()["stop_scale"] == 1.15 and _learner.await_count == 1

    await _add_closed(MANUAL_MIN_TRADES - 1, start=400)
    assert (await shadow_learner.learn(KEY, manual=True))["skipped"] is True
    await _add_closed(1, start=500)
    assert (await shadow_learner.learn(KEY, manual=True))["success"] is True
    assert _params()["stop_scale"] == 1.3


async def test_first_manual_learn_counts_all_closed_trades(_learner):  # noqa: F811
    await _add_closed(MANUAL_MIN_TRADES)
    _learner.return_value = {"changes": {"stop_scale": 2.0}}
    assert (await shadow_learner.learn(KEY, manual=True))["success"] is True


# --- 4. Shadow risk sizing -----------------------------------------------------------------------

async def _shadow_cost(strategy, stop, max_positions=4):
    """EUR size of a shadow buy at 100 with 10,000 cash, no open positions and the main bot's 0.5% frozen."""
    account = ShadowAccount(strategy="sizing_test", cash_eur=10000.0, initial_capital_eur=10000.0,
                            frozen_params={"max_positions": max_positions, "risk_per_trade_pct": 0.5})
    trigger = ShadowTrigger(strategy="sizing_test", ticker="SIZE.AS", name="Size", currency="EUR",
                            trigger_type="MARKET", target_price=100.0, stop_loss=stop, take_profit=120.0)
    async with AsyncSessionLocal() as session:
        fill = await shadow_portfolios._open_position(session, strategy, account, trigger, 100.0)
        await session.rollback()
    return fill["cost_eur"]


# --- 5. Presets ----------------------------------------------------------------------------------

def test_trailing_presets_do_not_lock_breakeven_before_their_trail():
    assert PRESETS["momentum"].breakeven_pct == PRESETS["no_breakeven"].breakeven_pct
    aggressive = PRESETS["aggressive"]
    assert aggressive.breakeven_r == 1.5
    # 5% initial stop: the +1% lock waits for +7.5%, where the 6% trail is already above entry
    trigger = effective_breakeven_pct(aggressive, 0.025, 100.0, 95.0)
    assert trigger == pytest.approx(0.075) and (1 + trigger) * (1 - aggressive.trailing_stop_pct) > 1.0


async def test_deleting_a_strategy_deletes_its_outcome_atoms():
    # A new strategy that reuses the key must not learn from the deleted one's trades
    from src.memory.outcomes import record_outcome
    from src.memory.store import memory_store
    from src.trading.shadow import create_strategy, delete_strategy, get_strategies
    key = next((s.key for s in get_strategies() if s.label == "Audit delete"), None) or await create_strategy("Audit delete", {})
    await record_outcome(source="shadow", strategy=key, ref_id="x1", ticker="A", pnl_pct=1.0, r_multiple=None,
                         hold_days=1.0, exit_reason="x", score=None, entry_type="MARKET", params={})
    assert len(await memory_store.search_outcomes(strategy=key)) == 1
    assert await delete_strategy(key) is True
    assert await memory_store.search_outcomes(strategy=key) == []
