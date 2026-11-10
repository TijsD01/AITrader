import math, pytest
from src.trading.strategy_params import SPECS, validate, clamp_step, to_json_schema
from src.trading.shadow import PRESETS

def test_every_preset_is_valid():
    for p in PRESETS.values():
        assert validate(p.to_params()) == p.to_params()

def test_specs_cover_every_strategy_param():
    assert set(SPECS) == set(PRESETS["baseline"].to_params())

@pytest.mark.parametrize("bad", [{"stop_scale": 9}, {"nope": 1}, {"stop_scale": "x"},
                                 {"stop_scale": None}, {"stop_scale": math.nan}, {"entry_mode": "limit"},
                                 {"stop_scale": True}, {"max_positions": 2.5}])
def test_validate_rejects(bad):
    with pytest.raises(ValueError):
        validate({**PRESETS["baseline"].to_params(), **bad})

def test_validate_partial_and_int_for_float():
    assert validate({"stop_scale": 1}) == {"stop_scale": 1.0}

def test_clamp_step_caps_move_by_strength():
    # stop_scale range 0.5..2.0 = 1.5 wide; low strength = 5% = 0.075 → rounds to step 0.05
    assert clamp_step("stop_scale", 1.0, 2.0, 1) == pytest.approx(1.05)
    assert clamp_step("stop_scale", 1.0, 2.0, 3) == pytest.approx(1.30)
    assert clamp_step("stop_scale", 1.0, 0.0, 3) == pytest.approx(0.70)

def test_clamp_step_ignores_non_learnable_and_garbage():
    assert clamp_step("ignore_risk_veto", False, True, 3) is False
    assert clamp_step("accept_hold", False, True, 3) is False
    assert clamp_step("entry_mode", "trigger", "market", 3) == "trigger"
    assert clamp_step("stop_scale", 1.0, "big", 3) == 1.0
    assert clamp_step("stop_scale", 1.0, math.inf, 3) == 1.0
    assert clamp_step("stop_scale", 1.0, 2.0, 0) == 1.0

def test_clamp_step_uses_learn_range():
    assert clamp_step("breakeven_pct", 0.09, 5.0, 3) <= 0.10

def test_clamp_step_none_bool_and_int():
    assert clamp_step("reward_risk", None, 2.0, 3) is None
    assert clamp_step("use_take_profit", True, False, 1) is False
    assert clamp_step("use_take_profit", True, "x", 1) is True
    assert clamp_step("max_positions", 5, 20, 3) == 8 and isinstance(clamp_step("max_positions", 5, 20, 3), int)

def test_json_schema_shape():
    row = next(r for r in to_json_schema() if r["name"] == "stop_scale")
    assert row["min"] == 0.5 and row["max"] == 2.0 and row["step"] == 0.05

def test_clamp_step_leaves_old_outside_learn_range():
    assert clamp_step("breakeven_pct", 10.0, 0.05, 3) == 10.0
    assert clamp_step("stop_scale", 3.0, 1.0, 3) == 3.0
