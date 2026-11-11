"""
The main bot's trading rules: one place that answers "which rules does the main bot use right now".

The rules live in SystemConfig rows. Rows the bot already had (TRIGGER_MIN_SCORE, BREAKEVEN_TRIGGER_PCT,
MAX_POSITIONS) are reused; the rest use MAIN_* keys. A missing row means today's behaviour, so with no rows
the main bot trades exactly as before. Every write keeps the complete previous rule set in
MAIN_RULES_PREVIOUS and bumps MAIN_RULES_VERSION, so one revert puts back what the owner had.
"""
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional

from sqlalchemy import delete, func, select

from config.settings import settings
from src.db import session as db_session  # read AsyncSessionLocal at call time (tests rebind it)
from src.db.models import OrderTrigger, Position, SystemConfig
from src.trading.strategy_params import SPECS, validate as validate_params

logger = logging.getLogger(__name__)

DEFAULT_BREAKEVEN_PCT = 0.025  # same fallback the brokers and shadows use for BREAKEVEN_TRIGGER_PCT

VERSION_KEY = "MAIN_RULES_VERSION"
PROMOTED_FROM_KEY = "MAIN_PROMOTED_FROM"
PREVIOUS_KEY = "MAIN_RULES_PREVIOUS"

# Rule name -> (SystemConfig key, value kind). "opt_" kinds store "" for None.
_KEYS: Dict[str, tuple] = {
    "trigger_min_score": ("TRIGGER_MIN_SCORE", "int"),
    "pullback_scale": ("MAIN_PULLBACK_SCALE", "float"),
    "stop_scale": ("MAIN_STOP_SCALE", "float"),
    "entry_mode": ("MAIN_ENTRY_MODE", "entry_mode"),
    "accept_hold": ("MAIN_ACCEPT_HOLD", "bool"),
    "ignore_risk_veto": ("MAIN_IGNORE_RISK_VETO", "bool"),
    "reward_risk": ("MAIN_REWARD_RISK", "opt_float"),
    "use_take_profit": ("MAIN_USE_TAKE_PROFIT", "bool"),
    "breakeven_pct": ("BREAKEVEN_TRIGGER_PCT", "float"),
    "breakeven_r": ("MAIN_BREAKEVEN_R", "opt_float"),
    "trailing_stop_pct": ("MAIN_TRAILING_STOP_PCT", "opt_float"),
    "max_positions": ("MAX_POSITIONS", "int"),
    "max_hold_days": ("MAIN_MAX_HOLD_DAYS", "opt_int"),
    "risk_per_trade_pct": ("MAIN_RISK_PER_TRADE_PCT", "float"),
    "trailing_atr_mult": ("MAIN_TRAILING_ATR_MULT", "float"),
    "regime_filter": ("MAIN_REGIME_FILTER", "bool"),
}

# Rules that existing code reads from `settings`; kept in step so those readers follow without edits.
# Rules with no row of their own before promote: their source of truth is .env, the stored row only a promote.
_ENV_OWNED = ("risk_per_trade_pct", "trailing_atr_mult", "regime_filter")

_SETTINGS_ATTR = {
    "trigger_min_score": "TRIGGER_MIN_SCORE",
    "max_positions": "MAX_POSITIONS",
    "risk_per_trade_pct": "RISK_PER_TRADE_PCT",
    "trailing_atr_mult": "TRAILING_STOP_ATR_MULT",
    "regime_filter": "REGIME_FILTER_ENABLED",
}


def _defaults() -> Dict[str, Any]:
    return {
        "trigger_min_score": settings.TRIGGER_MIN_SCORE,
        "pullback_scale": 1.0,
        "stop_scale": 1.0,
        "entry_mode": "trigger",
        "accept_hold": False,
        "ignore_risk_veto": False,
        "reward_risk": None,  # None = keep the committee's own target
        "use_take_profit": True,
        "breakeven_pct": DEFAULT_BREAKEVEN_PCT,
        "breakeven_r": None,
        "trailing_stop_pct": None,
        "max_positions": settings.MAX_POSITIONS,
        "max_hold_days": None,
        "risk_per_trade_pct": settings.RISK_PER_TRADE_PCT,
        "trailing_atr_mult": settings.TRAILING_STOP_ATR_MULT,
        "regime_filter": settings.REGIME_FILTER_ENABLED,
    }


@dataclass(frozen=True)
class MainRules:
    trigger_min_score: int
    pullback_scale: float
    stop_scale: float
    entry_mode: str
    accept_hold: bool
    ignore_risk_veto: bool
    reward_risk: Optional[float]
    use_take_profit: bool
    breakeven_pct: float
    breakeven_r: Optional[float]
    trailing_stop_pct: Optional[float]
    max_positions: int
    max_hold_days: Optional[int]
    risk_per_trade_pct: float
    trailing_atr_mult: float
    regime_filter: bool
    version: int
    promoted_from: Optional[str]

    RULE_FIELDS: ClassVar[tuple] = tuple(_KEYS)

    def rules(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.RULE_FIELDS}

    def as_strategy(self):
        """The rules as a ShadowStrategy, so plan_entry / effective_breakeven_pct work on the main bot too."""
        from src.trading.shadow import ShadowStrategy  # lazy: shadow.py will import this module

        params = {k: v for k, v in self.rules().items() if k != "trigger_min_score"}
        return ShadowStrategy(key="main", label="Main", description="", score_offset=0, **params)


def is_default_shape(rules: MainRules) -> bool:
    """True when every behaviour rule the main bot did not have before is at today's value."""
    return (
        rules.pullback_scale == 1.0 and rules.stop_scale == 1.0 and rules.entry_mode == "trigger"
        and rules.accept_hold is False and rules.ignore_risk_veto is False and rules.reward_risk is None
        and rules.use_take_profit is True and rules.breakeven_r is None and rules.trailing_stop_pct is None
        and rules.max_hold_days is None
    )


def effective_max_positions(new: int, live: bool, cap: int) -> int:
    """In LIVE a promote may lower the number of positions but never raise it above the current value."""
    return min(new, cap) if live else new


def _parse(kind: str, raw: str):
    if kind.startswith("opt_"):
        if raw.strip() == "":
            return None
        kind = kind[4:]
    if kind == "bool":
        low = raw.strip().lower()
        if low not in ("true", "false"):
            raise ValueError(f"not a boolean: {raw!r}")
        return low == "true"
    if kind == "int":
        return int(raw)
    if kind == "float":
        f = float(raw)
        if not math.isfinite(f):
            raise ValueError(f"not finite: {raw!r}")
        return f
    if raw not in SPECS["entry_mode"].choices:
        raise ValueError(f"unknown entry mode: {raw!r}")
    return raw


def _check_range(name: str, v):
    """Raise ValueError when a stored value is outside what the money logic can safely use."""
    if name == "trigger_min_score":
        if not 0 <= v <= 100:
            raise ValueError("must be between 0 and 100")
    elif name == "max_positions":
        if not 1 <= v <= 50:  # the settings route writes up to 50, wider than the shadow schema
            raise ValueError("must be between 1 and 50")
    else:
        validate_params({name: v})
    return v


def _dump(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


async def _load_rows(session) -> Dict[str, str]:
    keys = [k for k, _ in _KEYS.values()] + [VERSION_KEY, PROMOTED_FROM_KEY, PREVIOUS_KEY]
    res = await session.execute(select(SystemConfig).where(SystemConfig.key.in_(keys)))
    return {r.key: r.value for r in res.scalars().all()}


def _resolve(rows: Dict[str, str]) -> MainRules:
    values = _defaults()
    for name, (key, kind) in _KEYS.items():
        if key in rows:
            try:
                values[name] = _check_range(name, _parse(kind, rows[key]))
            except (ValueError, TypeError):
                logger.warning(f"Main rules: bad {key} value {rows[key]!r}; using default {values[name]!r}.")
    version = 0
    if VERSION_KEY in rows:
        try:
            version = int(rows[VERSION_KEY])
        except (ValueError, TypeError):
            logger.warning(f"Main rules: bad {VERSION_KEY} value {rows[VERSION_KEY]!r}; using 0.")
    return MainRules(**values, version=version, promoted_from=rows.get(PROMOTED_FROM_KEY) or None)


async def get_main_rules() -> MainRules:
    """The main bot's current rules. Never raises: a DB error gives today's defaults."""
    try:
        async with db_session.AsyncSessionLocal() as session:
            return _resolve(await _load_rows(session))
    except Exception as e:
        logger.warning(f"Main rules: could not read SystemConfig ({e}); using defaults.")
        return MainRules(**_defaults(), version=0, promoted_from=None)


def validate_main_rules(values: Dict[str, Any]) -> Dict[str, Any]:
    """The checks write_main_rules runs: ValueError on an unknown rule or a bad value, else the cleaned values."""
    unknown = set(values) - set(_KEYS)
    if unknown:
        raise ValueError(f"{sorted(unknown)[0]}: unknown rule")
    clean = dict(values)
    # max_positions uses the main range (the settings route allows up to 50), not the shadow schema's 1..20
    clean.update(validate_params({k: v for k, v in values.items() if k in SPECS and k != "max_positions"}))
    if "trigger_min_score" in clean:
        v = clean["trigger_min_score"]
        if isinstance(v, bool) or not isinstance(v, int) or not 50 <= v <= 100:
            raise ValueError("trigger_min_score: must be an integer between 50 and 100")
    if "max_positions" in clean:
        v = clean["max_positions"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 50:
            raise ValueError("max_positions: must be an integer between 1 and 50")
    for name, v in clean.items():
        if v is None and not _KEYS[name][1].startswith("opt_"):
            raise ValueError(f"{name}: may not be empty")
    return clean


async def _next_version(session, stored: int) -> int:
    """Positions are matched to rules by version number, so never reuse one, even when the version row was lost."""
    highest_pos = (await session.execute(select(func.max(Position.rules_version)))).scalar() or 0
    highest_trig = (await session.execute(select(func.max(OrderTrigger.rules_version)))).scalar() or 0
    return max(stored, highest_pos, highest_trig) + 1


async def _save(session, rules: Dict[str, Any], version: int, promoted_from: Optional[str], previous: Optional[str]):
    for name, v in rules.items():
        await session.merge(SystemConfig(key=_KEYS[name][0], value=_dump(v)))
    await session.merge(SystemConfig(key=VERSION_KEY, value=str(version)))
    await session.merge(SystemConfig(key=PROMOTED_FROM_KEY, value=promoted_from or ""))
    if previous is None:
        await session.execute(delete(SystemConfig).where(SystemConfig.key == PREVIOUS_KEY))
    else:
        await session.merge(SystemConfig(key=PREVIOUS_KEY, value=previous))
    await session.commit()


def _apply_settings(rules: Dict[str, Any]) -> None:
    for name, attr in _SETTINGS_ATTR.items():
        if name in rules:
            setattr(settings, attr, rules[name])


async def write_main_rules(values: Dict[str, Any], promoted_from: Optional[str], live: bool = False) -> int:
    """Store new main rules in one transaction, keeping the previous set for revert. Returns the new version.

    Raises ValueError (nothing written) for an unknown rule or an out-of-range value. In LIVE,
    max_positions can only go down. Does not touch .env.
    """
    clean = validate_main_rules(values)
    async with db_session.AsyncSessionLocal() as session:
        current = _resolve(await _load_rows(session))
        if live and "max_positions" in clean:
            clean["max_positions"] = effective_max_positions(clean["max_positions"], True, current.max_positions)
        version = await _next_version(session, current.version)
        snapshot = {**current.rules(), "promoted_from": current.promoted_from}
        await _save(session, clean, version, promoted_from, json.dumps(snapshot))
    _apply_settings(clean)
    logger.info(f"Main rules v{version} written (from {promoted_from or 'manual edit'}): {clean}")
    return version


async def revert_main_rules() -> bool:
    """Put back the rule set (and its promoted_from) saved by the last write. False when nothing is stored."""
    async with db_session.AsyncSessionLocal() as session:
        rows = await _load_rows(session)
        raw = rows.get(PREVIOUS_KEY)
        if not raw:
            return False
        try:
            stored = json.loads(raw)
            # Same parse and range checks as a read: an out-of-range stored value reverts to its default.
            previous = _resolve({key: _dump(stored[name]) for name, (key, _) in _KEYS.items()}).rules()
            # Snapshots written before promoted_from was stored restore as "not promoted"
            promoted_from = str(stored.get("promoted_from") or "") or None
        except (ValueError, TypeError, KeyError) as e:
            logger.warning(f"Main rules: cannot revert, {PREVIOUS_KEY} is unreadable ({e}).")
            return False
        version = await _next_version(session, _resolve(rows).version)
        to_store = previous
        if promoted_from is None:
            # Back to the owner's own rules: drop the .env-owned rows so a later .env edit takes effect again.
            to_store = {k: v for k, v in previous.items() if k not in _ENV_OWNED}
            await session.execute(delete(SystemConfig).where(SystemConfig.key.in_([_KEYS[k][0] for k in _ENV_OWNED])))
        await _save(session, to_store, version, promoted_from, None)
    _apply_settings(previous)
    logger.info(f"Main rules reverted to the previous set as v{version}.")
    return True


async def sync_settings_from_main_rules() -> None:
    """At startup, put the stored main rules into `settings` so risk.py and the brokers read the same values."""
    try:
        _apply_settings((await get_main_rules()).rules())
    except Exception as e:
        logger.warning(f"Main rules: could not apply to settings at startup ({e}).")
