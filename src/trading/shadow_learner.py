"""Memory-based learner for shadow (simulation-only) strategies.

Asks the LLM for small parameter changes from a shadow's closed trades and outcome memory, then
guards the reply: at most 3 entries, only learnable params, every value through clamp_step.
Never touches real-money code: it only edits shadow strategy params.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List

from sqlalchemy import select

from config.settings import settings
from src.agents.base import LLMClient
from src.db import session as db_session
from src.db.models import ShadowAccount, ShadowParamChange, ShadowPosition, ShadowStrategyRow
from src.memory.models import L1Atom
from src.memory.store import memory_store
from src.trading.shadow import ShadowPortfolios, apply_param_change, get_strategies, learning_strengths
from src.trading.strategy_params import SPECS, _is_num, clamp_step

logger = logging.getLogger(__name__)

MAX_CHANGES = 3
MANUAL_MIN_TRADES = 5
OWN_OUTCOMES = 40
MAIN_OUTCOMES = 20
# Params whose None means "use the main bot's value frozen at the shadow's start" (not "feature off")
INHERITED = ("breakeven_pct", "max_positions", "risk_per_trade_pct", "trailing_atr_mult", "regime_filter")
OUTCOME_FIELDS = ("ticker", "pnl_pct", "r_multiple", "hold_days", "exit_reason", "score", "entry_type")

SYSTEM_PROMPT = (
    "You tune the parameters of a SIMULATED paper-trading strategy (a shadow portfolio; no real money). "
    "Only the listed tunable parameters may change, each within its [lo, hi] range. "
    f"Change at most {MAX_CHANGES} parameters. The system enforces small steps, so propose a direction, not a jump. "
    "Prefer no change when the evidence is thin: fewer than about 30 trades is mostly noise. "
    'Return JSON only, in exactly this shape: {"changes": {"<param>": <value>}, "reason": "<one or two sentences>"}. '
    'Return {"changes": {}, "reason": "..."} to keep the current parameters.'
)


def _result(success: bool, skipped: bool, message: str, changes: dict = None) -> dict:
    return {"success": success, "skipped": skipped, "message": message, "changes": changes or {}}


def _r_multiple(p: ShadowPosition):
    risk = (p.entry_price_native or 0) - (p.initial_stop_native or 0)
    if p.initial_stop_native is None or p.exit_price_native is None or risk <= 0:
        return None
    return (p.exit_price_native - p.entry_price_native) / risk


def _avg(values: List[float]):
    return round(sum(values) / len(values), 4) if values else None


def compute_stats(positions: List[ShadowPosition]) -> Dict[str, Any]:
    """Summary of closed shadow positions. Pure: reads the given rows only."""
    pnls = [p.realized_pnl_pct or 0.0 for p in positions]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    exit_reasons: Dict[str, int] = {}
    for p in positions:
        reason = p.exit_reason or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    holds = [(p.closed_at - p.opened_at).total_seconds() / 86400 for p in positions if p.closed_at and p.opened_at]
    rs = [r for r in map(_r_multiple, positions) if r is not None]
    return {
        "trades": len(positions),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else None,
        "avg_win_pct": _avg(wins),
        "avg_loss_pct": _avg(losses),
        "avg_r": _avg(rs),
        "expectancy_pct": _avg(pnls),
        "exit_reasons": exit_reasons,
        "avg_hold_days": _avg(holds),
        "fees_eur": round(sum(p.fees_eur or 0.0 for p in positions), 2),
    }


def _ranges() -> List[str]:
    lines = []
    for s in SPECS.values():
        if not s.learnable:
            continue
        if s.kind == "bool":
            lines.append(f"- {s.name} ({s.label}): true or false")
            continue
        lo = s.min if s.learn_min is None else s.learn_min
        hi = s.max if s.learn_max is None else s.learn_max
        lines.append(f"- {s.name} ({s.label}): {s.kind} in [{lo}, {hi}], step {s.step}")
    return lines


async def _inherited(strategy) -> dict:
    """Working values of the INHERITED params the strategy leaves at None (the main bot's frozen values)."""
    async with db_session.AsyncSessionLocal() as session:
        acc = await session.get(ShadowAccount, strategy.key)
    eff = ShadowPortfolios._effective(strategy, acc or ShadowAccount(frozen_params={}))
    return {k: eff[k] for k in INHERITED if getattr(strategy, k) is None}


async def _locked_reversals(key: str, closed: List[ShadowPosition]) -> dict:
    """{param: value before the learner's last change} for params the learner may not move back yet.

    Hysteresis against flip-flopping on noise: a learner change is only undone once at least
    SHADOW_LEARN_MIN_TRADES trades opened after it have closed with a lower expectancy than the trades
    that motivated it. Positions opened before the change ran under the old value, so they do not count.
    A change a human made last is the human's call and is never locked."""
    async with db_session.AsyncSessionLocal() as session:
        history = (await session.execute(
            select(ShadowParamChange).where(ShadowParamChange.strategy == key)
            .order_by(ShadowParamChange.created_at.desc(), ShadowParamChange.id.desc())
        )).scalars().all()
    seen, locked = set(), {}
    for change in history:
        for name, diff in (change.changes or {}).items():
            if name in seen or not isinstance(diff, dict):
                continue
            seen.add(name)
            if change.source != "learner" or not change.created_at:
                continue
            before = ((change.stats or {}).get("since_last_learn") or {}).get("expectancy_pct")
            if before is None:  # nothing to compare with: locking would pin the parameter for good
                continue
            after = [p.realized_pnl_pct or 0.0 for p in closed if p.opened_at and p.opened_at >= change.created_at]
            worse = len(after) >= settings.SHADOW_LEARN_MIN_TRADES and sum(after) / len(after) < before
            if not worse:
                locked[name] = diff.get("old")
    return locked


def _is_reversal(old, new, before) -> bool:
    """True when old -> new moves back toward before (the value ahead of the learner's last change)."""
    if not (_is_num(old) and _is_num(new) and _is_num(before)):
        return new == before
    return (new - old) * (before - old) > 0


def _guard(reply: dict, params: dict, strength: int, inherited: dict = None, locked: dict = None) -> dict:
    """Clamped proposals that differ from the current value. Takes the first MAX_CHANGES entries before filtering,
    so junk entries cannot make room for a 4th real change. inherited: working values clamped from where a
    param is None. locked: from _locked_reversals; a move back toward the locked value is dropped."""
    inherited = inherited or {}
    locked = locked or {}
    kept = {}
    for name, value in list(reply["changes"].items())[:MAX_CHANGES]:
        if name not in SPECS or not SPECS[name].learnable:
            continue
        old = params.get(name)
        if old is None:
            old = inherited.get(name)
        new = clamp_step(name, old, value, strength)
        if name in locked and _is_reversal(old, new, locked[name]):
            logger.info(f"Shadow learner: {name} {old} -> {new} undoes its last change without evidence; kept {old}")
            continue
        if new != old:
            kept[name] = new
    return kept


class ShadowLearner:
    def __init__(self):
        self.llm = LLMClient()
        self._locks: Dict[str, asyncio.Lock] = {}

    async def learn(self, key: str, manual: bool = False) -> dict:
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():  # never queue a second run behind the first
            return _result(False, True, f"Learning for '{key}' is already running")
        async with lock:
            return await self._learn(key, manual)

    async def _learn(self, key: str, manual: bool) -> dict:
        strategy = next((s for s in get_strategies() if s.key == key), None)
        if strategy is None:
            return _result(False, True, f"Unknown shadow strategy '{key}'")
        strength = learning_strengths().get(key, 0)
        if strength <= 0:
            return _result(False, True, "Learning strength is 0")

        async with db_session.AsyncSessionLocal() as session:
            row = await session.get(ShadowStrategyRow, key)
            last = (row.closed_at_last_learn or 0) if row else 0
            closed = (await session.execute(
                select(ShadowPosition).where(ShadowPosition.strategy == key, ShadowPosition.status == "CLOSED")
                .order_by(ShadowPosition.closed_at.desc(), ShadowPosition.id.desc())
            )).scalars().all()
        total = len(closed)
        if last > total:  # the portfolio was reset; count from zero again
            last = 0
        new = total - last
        if manual and total < MANUAL_MIN_TRADES:
            return _result(False, True, f"Needs at least {MANUAL_MIN_TRADES} closed trades (has {total})")
        if not manual and new < settings.SHADOW_LEARN_MIN_TRADES:
            return _result(False, True, f"{new} new closed trades; learning starts at {settings.SHADOW_LEARN_MIN_TRADES}")

        stats = {"since_last_learn": compute_stats(closed[:new]), "all": compute_stats(closed)}
        params = {**strategy.to_params(), **await _inherited(strategy)}
        user_prompt = await self._prompt(key, params, stats)
        try:
            reply = await self.llm.generate_json(SYSTEM_PROMPT, user_prompt)
        except Exception as e:
            logger.warning(f"Shadow learner LLM call failed for '{key}': {e}")
            return _result(False, False, "LLM call failed")
        if not isinstance(reply, dict) or not isinstance(reply.get("changes"), dict):
            return _result(False, False, "LLM reply missing or malformed")

        # The call can take minutes: clamp against the params and strength as they are now, not as sent
        strategy = next((s for s in get_strategies() if s.key == key), None)
        strength = learning_strengths().get(key, 0)
        if strategy is None or strength <= 0:
            return _result(False, True, "Strategy was deleted, disabled or set to strength 0 during the LLM call")
        kept = _guard(reply, strategy.to_params(), strength, await _inherited(strategy),
                      await _locked_reversals(key, closed))
        reason = reply.get("reason")
        reason = reason[:500] if isinstance(reason, str) else ""
        changes = {}
        if kept:
            try:
                changes = await apply_param_change(key, kept, source="learner", reason=reason, stats=stats)
            except (LookupError, ValueError) as e:  # deleted mid-run, or a value the schema still rejects
                logger.warning(f"Shadow learner could not apply changes to '{key}': {e}")
                return _result(False, False, f"Could not apply changes: {e}")
        await self._advance(key, total)
        if changes:
            await self._remember(key, changes, reason, stats)
            return _result(True, False, f"Changed {', '.join(changes)}", changes)
        return _result(True, False, "LLM proposed no usable change")

    async def _prompt(self, key: str, params: dict, stats: dict) -> str:
        own = await memory_store.search_outcomes(strategy=key, limit=OWN_OUTCOMES)
        main = await memory_store.search_outcomes(source="main", limit=MAIN_OUTCOMES)
        trim = lambda rows: [{f: r.get(f) for f in OUTCOME_FIELDS} for r in rows]  # noqa: E731
        learnable = {k: v for k, v in params.items() if k in SPECS and SPECS[k].learnable}
        return "\n\n".join([
            f"Shadow strategy: {key}",
            "Tunable parameters (name: allowed range):\n" + "\n".join(_ranges()),
            "Current values: " + json.dumps(learnable),
            "Stats since last learning pass: " + json.dumps(stats["since_last_learn"]),
            "Stats over all closed trades: " + json.dumps(stats["all"]),
            f"This strategy's recent outcomes (newest first, up to {OWN_OUTCOMES}): " + json.dumps(trim(own)),
            f"Main bot's recent outcomes, for context (up to {MAIN_OUTCOMES}): " + json.dumps(trim(main)),
        ])

    @staticmethod
    async def _advance(key: str, total: int) -> None:
        async with db_session.AsyncSessionLocal() as session:
            row = await session.get(ShadowStrategyRow, key)
            if row:
                row.closed_at_last_learn = total
                await session.commit()

    @staticmethod
    async def _remember(key: str, changes: dict, reason: str, stats: dict) -> None:
        summary = "; ".join(f"{n} {c['old']} -> {c['new']}" for n, c in changes.items())
        content = f"Learner changed shadow '{key}': {summary}" + " ||| " + json.dumps(
            {"changes": changes, "reason": reason, "stats": stats})
        try:
            await memory_store.add_l1_atom(L1Atom(
                type="episodic", agent_id="outcomes", subject=key, predicate="param_change",
                object=f"shadow:{key}", content=content,
            ))
        except Exception as e:  # best effort: the param change already stands
            logger.warning(f"Could not record shadow param change in memory for '{key}': {e}")

    async def learn_due(self) -> List[dict]:
        results = []
        for key, strength in learning_strengths().items():
            if strength <= 0:
                continue
            try:
                results.append(await self.learn(key))
            except Exception as e:
                logger.exception(f"Shadow learning failed for '{key}': {e}")
        return results


shadow_learner = ShadowLearner()


_learn_task = None  # strong reference so the background run is not garbage-collected mid-flight


async def _run_learn_due() -> None:
    try:
        await shadow_learner.learn_due()
    except Exception as e:
        logger.exception(f"Background shadow learning failed: {e}")


def schedule_learn_due() -> None:
    """Runs learn_due() in the background; never raises, and never starts a second run while one is in flight."""
    global _learn_task
    try:
        if _learn_task is None or _learn_task.done():
            _learn_task = asyncio.get_running_loop().create_task(_run_learn_due())
    except Exception as e:
        logger.error(f"Could not schedule shadow learning: {e}")
