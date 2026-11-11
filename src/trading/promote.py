"""
Manual promote: the owner copies a shadow strategy's rules onto the main bot.

Shadow values are resolved against the CURRENT main rules (never the shadow's frozen start values), shown as a
preview, and written only on an explicit confirm. In LIVE, a risky change (see RISKY_FLAGS) also needs the typed text
"PROMOTE LIVE". A promote pauses automatic calibration so the learner does not overwrite the promoted rules.
It changes rules only: no broker call, and open positions and pending triggers keep their stored levels.
"""
import html
import json
import logging
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.db import session as db_session  # read AsyncSessionLocal at call time (tests rebind it)
from src.db.models import ShadowParamChange, SystemConfig
from src.trading.main_strategy import (
    PREVIOUS_KEY, MainRules, effective_max_positions, get_main_rules, revert_main_rules, validate_main_rules,
    write_main_rules,
)
from src.trading.shadow import ShadowStrategy, get_strategies
from src.trading.strategy_params import SPECS

logger = logging.getLogger(__name__)

PAUSE_KEY = "MAIN_LEARNER_PAUSED"
CONFIRM_TEXT = "PROMOTE LIVE"
# Changes that need the typed text in LIVE, in the order they are reported
RISKY_FLAGS = ("ignore_risk_veto", "accept_hold", "entry_mode", "risk_per_trade_pct", "regime_filter", "trigger_min_score")
SLOT_RISK_NOTE = ("Risk per trade only switches the risk cap on or off while RISK_BUDGET_BASE is \"slot\". "
                  "Position sizes will not match this shadow's; set RISK_BUDGET_BASE=portfolio to size by % of "
                  "portfolio at the stop.")
RISKY_THRESHOLD_DROP = 5  # lowering the score threshold by this many points or more is risky
# No main value to inherit: None is a real setting here ("no fixed R target / no R breakeven / no % trail / no time exit")
_NONE_IS_A_SETTING = ("reward_risk", "breakeven_r", "trailing_stop_pct", "max_hold_days")
_INHERIT_WHEN_NONE = ("breakeven_pct", "max_positions", "risk_per_trade_pct", "trailing_atr_mult", "regime_filter")
_COPY = ("pullback_scale", "stop_scale", "entry_mode", "accept_hold", "ignore_risk_veto", "use_take_profit")


def is_live() -> bool:
    return settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED


def resolve_for_main(strategy: ShadowStrategy, current: MainRules) -> Dict[str, Any]:
    """Absolute values for all 16 main rules from a shadow, inheriting the current main value where the shadow has none."""
    out: Dict[str, Any] = {"trigger_min_score": strategy.threshold(current.trigger_min_score)}
    for name in _COPY + _NONE_IS_A_SETTING:
        out[name] = getattr(strategy, name)
    for name in _INHERIT_WHEN_NONE:
        v = getattr(strategy, name)
        out[name] = getattr(current, name) if v is None else v
    return {name: out[name] for name in MainRules.RULE_FIELDS}


def _find(key: str) -> ShadowStrategy:
    for s in get_strategies():
        if s.key == key:
            return s
    raise LookupError(f"No shadow portfolio named '{key}'.")


def _risky_flags(current: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    newly_on = {
        "ignore_risk_veto": not current["ignore_risk_veto"] and after["ignore_risk_veto"],
        "accept_hold": not current["accept_hold"] and after["accept_hold"],
        "entry_mode": current["entry_mode"] == "trigger" and after["entry_mode"] == "market",
        # More risk per trade, or 0 which turns the risk cap off
        "risk_per_trade_pct": after["risk_per_trade_pct"] > current["risk_per_trade_pct"]
                              or (after["risk_per_trade_pct"] == 0 and current["risk_per_trade_pct"] > 0),
        "regime_filter": current["regime_filter"] and not after["regime_filter"],
        "trigger_min_score": current["trigger_min_score"] - after["trigger_min_score"] >= RISKY_THRESHOLD_DROP,
    }
    return [name for name in RISKY_FLAGS if newly_on[name]]


async def _plan(key: str):
    """(strategy, current rules, values that would be stored, live) for one shadow."""
    strategy = _find(key)
    current = await get_main_rules()
    live = is_live()
    after = resolve_for_main(strategy, current)
    # What write_main_rules will actually store: in LIVE max_positions can only go down
    after["max_positions"] = effective_max_positions(after["max_positions"], live, current.max_positions)
    return strategy, current, after, live


async def preview(key: str) -> Dict[str, Any]:
    strategy, current, after, live = await _plan(key)
    now = current.rules()
    try:
        validate_main_rules(after)  # same checks write_main_rules runs, so a blocked promote shows here first
        blocked_reason = ""
    except ValueError as e:
        blocked_reason = str(e)
    risky = _risky_flags(now, after)
    notes = []
    if settings.RISK_BUDGET_BASE != "portfolio" and now["risk_per_trade_pct"] != after["risk_per_trade_pct"]:
        notes.append(SLOT_RISK_NOTE)
    params = [{"name": name, "label": SPECS[name].label if name in SPECS else "Score threshold",
               "current": now[name], "after": after[name], "changed": now[name] != after[name]}
              for name in MainRules.RULE_FIELDS]
    return {
        "key": strategy.key, "label": strategy.label, "live": live,
        "risky_flags": risky, "needs_confirm_text": live and bool(risky),
        "params": params, "has_changes": any(p["changed"] for p in params),
        "blocked": bool(blocked_reason), "blocked_reason": blocked_reason, "notes": notes,
    }


async def _get_pause() -> Optional[str]:
    async with db_session.AsyncSessionLocal() as session:
        row = await session.get(SystemConfig, PAUSE_KEY)
        return row.value if row else None


async def _set_pause(value: Optional[str]) -> None:
    """Store the pause row as given; None removes it."""
    async with db_session.AsyncSessionLocal() as session:
        if value is not None:
            await session.merge(SystemConfig(key=PAUSE_KEY, value=value))
        else:
            row = await session.get(SystemConfig, PAUSE_KEY)
            if row:
                await session.delete(row)
        await session.commit()


async def set_learner_paused(paused: bool) -> None:
    await _set_pause("true" if paused else None)


async def _record_history(key: str, changes: dict, version: int) -> None:
    async with db_session.AsyncSessionLocal() as session:
        session.add(ShadowParamChange(strategy=key, source="promote", changes=changes,
                                      reason=f"Promoted to main bot (rules v{version})"))
        await session.commit()


async def _remember(key: str, changes: dict, version: int) -> None:
    from src.memory.models import L1Atom
    from src.memory.store import memory_store
    summary = "; ".join(f"{n} {c['old']} -> {c['new']}" for n, c in changes.items()) or "no rule changes"
    try:
        await memory_store.add_l1_atom(L1Atom(
            type="episodic", agent_id="outcomes", subject=key, predicate="promoted_to_main", object="main",
            content=f"Owner promoted shadow '{key}' to the main bot as rules v{version}: {summary}"
                    + " ||| " + json.dumps({"changes": changes, "version": version}),
        ))
    except Exception as e:  # best effort: the promote already stands
        logger.warning(f"Could not record promote of '{key}' in memory: {e}")


async def _notify(label: str, changes: dict, version: int, live: bool) -> None:
    lines = "\n".join(f"• {html.escape(n)}: {html.escape(str(c['old']))} → {html.escape(str(c['new']))}"
                      for n, c in changes.items()) or "• no rule changes"
    text = (f"⚙️ <b>Main bot rules v{version}</b> ({'LIVE' if live else 'Paper'})\n"
            f"Promoted from shadow <b>{html.escape(label)}</b>. Automatic calibration is paused.\n{lines}")
    try:
        from src.telegram.bot import telegram_bot
        await telegram_bot.send_message(text)
    except Exception as e:  # best effort: the promote already stands
        logger.warning(f"Could not send promote notice: {e}")


async def promote(key: str, confirm: Any, confirm_text: Any = "") -> Dict[str, Any]:
    """Write a shadow's resolved rules as the main rules. ValueError (nothing written) without the needed confirmation."""
    if confirm is not True:
        raise ValueError("Promote needs confirm: true.")
    strategy, current, after, live = await _plan(key)
    now = current.rules()
    risky = _risky_flags(now, after)
    if live and risky and confirm_text != CONFIRM_TEXT:
        raise ValueError(f"LIVE promote turns on {', '.join(risky)}: type {CONFIRM_TEXT} to confirm.")
    # Pause before the write so the learner cannot retune the promoted rules in between
    paused_before = await _get_pause()
    await _set_pause("true")
    try:
        version = await write_main_rules(after, promoted_from=key, live=live)  # ValueError: nothing written
    except Exception:
        try:
            await _set_pause(paused_before)
        except Exception:  # never mask the write error the caller must see
            logger.critical(f"Promote of '{key}': could not restore the learner pause after a failed write.")
        raise
    changes = {n: {"old": now[n], "new": after[n]} for n in MainRules.RULE_FIELDS if now[n] != after[n]}
    try:
        await _record_history(key, changes, version)
    except Exception:
        logger.exception(f"Promote of '{key}': could not record history; reverting the rules.")
        try:
            await revert_main_rules()
            await _set_pause(paused_before)
        except Exception:
            logger.critical(f"Promote of '{key}': rollback failed; rules v{version} may still be active.")
        raise
    logger.info(f"Promoted shadow '{key}' to main rules v{version} (live={live}): {changes}")
    await _remember(key, changes, version)
    await _notify(strategy.label, changes, version, live)
    return {"success": True, "version": version, "changes": changes, "learner_paused": True}


async def revert() -> bool:
    """Put back the rules from before the last write. False when nothing to revert.

    The learner resumes only when the restored rules are the owner's own; restored promoted rules stay paused.
    """
    if not await revert_main_rules():
        return False
    if (await get_main_rules()).promoted_from is None:
        await set_learner_paused(False)
    return True


async def status() -> Dict[str, Any]:
    rules = await get_main_rules()
    async with db_session.AsyncSessionLocal() as session:
        paused = await session.get(SystemConfig, PAUSE_KEY)
        previous = await session.get(SystemConfig, PREVIOUS_KEY)
    labels = {s.key: s.label for s in get_strategies()}
    return {
        "rules": rules.rules(), "version": rules.version, "promoted_from": rules.promoted_from,
        "promoted_label": labels.get(rules.promoted_from, rules.promoted_from) if rules.promoted_from else None,
        "learner_paused": bool(paused and (paused.value or "").strip().lower() == "true"),
        "can_revert": bool(previous and previous.value), "live": is_live(),
    }
