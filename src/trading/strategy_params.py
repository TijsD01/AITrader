"""Bounds for every editable shadow-strategy parameter: one schema for UI sliders, API validation and learner clamps.

Must not import src.trading.shadow (shadow imports this module).
"""
import dataclasses
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    kind: str  # "int" | "float" | "bool" | "choice"
    min: Optional[float]
    max: Optional[float]
    step: Optional[float]
    nullable: bool
    learnable: bool
    choices: tuple = ()
    learn_min: Optional[float] = None  # learner range when narrower than the edit range
    learn_max: Optional[float] = None


def _num(name, label, kind, lo, hi, step, nullable=False, **kw) -> ParamSpec:
    return ParamSpec(name, label, kind, lo, hi, step, nullable, True, **kw)


def _flag(name, label, learnable=True, nullable=False) -> ParamSpec:
    return ParamSpec(name, label, "bool", None, None, None, nullable, learnable)


_LIST = [
    _num("score_offset", "Score offset", "int", -20, 10, 1),
    _flag("accept_hold", "Accept HOLD verdicts", learnable=False),
    _flag("ignore_risk_veto", "Ignore risk veto", learnable=False),
    ParamSpec("entry_mode", "Entry mode", "choice", None, None, None, False, False, ("trigger", "market")),
    _num("pullback_scale", "Pullback scale", "float", 0.0, 1.5, 0.05),
    _num("stop_scale", "Stop scale", "float", 0.5, 2.0, 0.05),
    _num("reward_risk", "Reward / risk", "float", 1.0, 5.0, 0.1, True),
    _flag("use_take_profit", "Use take profit"),
    _num("breakeven_pct", "Breakeven gain", "float", 0.005, 10.0, 0.005, True, learn_min=0.005, learn_max=0.10),
    _num("breakeven_r", "Breakeven (R)", "float", 0.5, 3.0, 0.1, True),
    _num("trailing_stop_pct", "Trailing stop", "float", 0.02, 0.20, 0.01, True),
    _num("max_positions", "Max positions", "int", 1, 20, 1, True),
    _num("max_hold_days", "Max hold days", "int", 1, 120, 1, True),
    _num("risk_per_trade_pct", "Risk per trade %", "float", 0.0, 3.0, 0.1, True),
    _num("trailing_atr_mult", "Trailing ATR multiple", "float", 0.0, 6.0, 0.25, True),
    _flag("regime_filter", "Regime filter", nullable=True),
]
SPECS: Dict[str, ParamSpec] = {s.name: s for s in _LIST}

STRENGTH_SHARE = {1: 0.05, 2: 0.10, 3: 0.20}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check(spec: ParamSpec, v):
    n = spec.name
    if v is None:
        if not spec.nullable:
            raise ValueError(f"{n}: may not be null")
        return None
    if spec.kind == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{n}: must be true or false")
        return v
    if spec.kind == "choice":
        if v not in spec.choices:
            raise ValueError(f"{n}: must be one of {list(spec.choices)}")
        return v
    if not _is_num(v):
        raise ValueError(f"{n}: must be a number")
    if spec.kind == "int" and not isinstance(v, int):
        raise ValueError(f"{n}: must be an integer")
    if not math.isfinite(v):
        raise ValueError(f"{n}: must be finite")
    if not spec.min <= v <= spec.max:
        raise ValueError(f"{n}: must be between {spec.min} and {spec.max}")
    return float(v) if spec.kind == "float" else v


def validate(params: Dict[str, Any]) -> Dict[str, Any]:
    """Check the given keys only; return a cleaned copy."""
    out = {}
    for name, v in params.items():
        if name not in SPECS:
            raise ValueError(f"{name}: unknown parameter")
        out[name] = _check(SPECS[name], v)
    return out


def clamp_step(name: str, old, proposed, strength: int):
    """Limit a learner's proposed change: max move is STRENGTH_SHARE x range, clamped, snapped to step toward old."""
    spec = SPECS[name]
    if old is None:
        return None
    share = STRENGTH_SHARE.get(strength)
    if not spec.learnable or share is None:
        return old
    if spec.kind == "bool":
        return proposed if isinstance(proposed, bool) else old
    if spec.kind == "choice":
        return proposed if proposed in spec.choices else old
    if not _is_num(proposed) or not math.isfinite(proposed):
        return old
    lo = spec.min if spec.learn_min is None else spec.learn_min
    hi = spec.max if spec.learn_max is None else spec.learn_max
    if _is_num(old) and not lo <= old <= hi:
        return old  # a human set it outside the learnable range; never move it
    cap = share * (hi - lo)
    delta = max(-cap, min(cap, proposed - old))
    new = max(lo, min(hi, old + delta))
    k = (new - lo) / spec.step
    # Snap toward old so the move never exceeds the cap; never cross old.
    if delta >= 0:
        new = max(old, lo + math.floor(k + 1e-9) * spec.step) if new < hi else new
        new = min(new, hi)
    else:
        new = min(old, lo + math.ceil(k - 1e-9) * spec.step) if new > lo else new
        new = max(new, lo)
    new = round(new, 6)
    return int(round(new)) if spec.kind == "int" else new


def to_json_schema() -> List[Dict[str, Any]]:
    return [dataclasses.asdict(s) for s in SPECS.values()]
