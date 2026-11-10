"""
Shadow paper portfolios.

Each shadow strategy is a fully simulated portfolio with its own cash, triggers and positions. It sees the
same committee verdicts as the main bot (paper or live), but applies its own entry, exit and sizing rules,
so the results show whether other parameter values would have done better. Shadows never call a broker,
never touch the main portfolio's tables, and never feed the learner, agent memory or Telegram.

Unless a strategy sets its own value, a shadow uses the main bot's rules: trigger threshold, breakeven %,
max positions, risk per trade, ATR trailing stop and market regime filter. Those values are frozen when the
shadow starts, so the learner (or an .env edit) changing the main bot does not change the experiment halfway.
Resetting a shadow restarts it on the main bot's values of that moment. Like the main bot, shadows buy whole
shares when WHOLE_SHARES_ONLY is on.
"""
import asyncio
import dataclasses
import datetime
import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import delete, desc, func, select, update

from config.settings import settings
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.data.market_hours import check_market_hours
from src.db.models import (
    OrderTrigger, Position, ShadowAccount, ShadowParamChange, ShadowPosition, ShadowSnapshot, ShadowStrategyRow,
    ShadowTrigger, SystemConfig, Trade,
)
from src.db.session import AsyncSessionLocal
from src.memory.outcomes import record_outcome
from src.trading.cash_flows import list_cash_flow_points
from src.trading.strategy_params import SPECS, validate as validate_params
from src.trading.risk import (
    apply_risk_budget,
    calculate_position_allocation,
    compute_trailing_stop,
    derive_trigger_levels,
    evaluate_trigger_condition,
    get_effective_deployable_cash,
    is_trigger_expired,
    regime_index_for_ticker,
    whole_share_count,
)

logger = logging.getLogger(__name__)

MAIN_KEY = "MAIN"
SNAPSHOT_INTERVAL = datetime.timedelta(minutes=55)


def _return_pct(value: float, start: float, flows: float) -> float:
    """Gain over everything put in: a deposit adds to the base, so it neither counts as gain nor inflates the %."""
    base = start + flows
    return round((value - flows - start) / base * 100, 2) if base > 0 else 0.0


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ShadowStrategy:
    """Rules a shadow portfolio applies on top of the main bot's committee verdicts."""
    key: str
    label: str
    description: str
    score_offset: int = 0                     # Added to the (frozen) main TRIGGER_MIN_SCORE (negative = takes more setups)
    accept_hold: bool = False                 # Also act on HOLD verdicts that clear the score threshold
    ignore_risk_veto: bool = False            # Act even when the Risk Officer rejected the trade
    entry_mode: str = "trigger"               # "trigger": main bot's pullback/breakout level; "market": buy right away
    pullback_scale: float = 1.0               # Fraction of the main bot's pullback depth to wait for (0.5 = half as deep)
    stop_scale: float = 1.0                   # Multiplier on the stop distance below the entry
    reward_risk: Optional[float] = None       # Take profit at entry + R x stop distance (None = main bot's target)
    use_take_profit: bool = True              # False = no fixed target; exits come from the stop / trailing stop / time
    breakeven_pct: Optional[float] = None     # Gain at which the stop moves to entry +1% (None = main bot's, frozen)
    breakeven_r: Optional[float] = None       # Gain, in multiples of the initial stop distance, at which the stop moves to entry +1%; overrides breakeven_pct
    trailing_stop_pct: Optional[float] = None # Stop trails the highest price by this fraction
    max_positions: Optional[int] = None       # None = main bot's MAX_POSITIONS (frozen)
    max_hold_days: Optional[int] = None       # Close positions older than this many days
    risk_per_trade_pct: Optional[float] = None  # Loss at the stop as % of the portfolio (None = main bot's, 0 = off)
    trailing_atr_mult: Optional[float] = None   # Past breakeven, trail at high - N x ATR (None = main bot's, 0 = off)
    regime_filter: Optional[bool] = None        # No new entries while the index is below its average (None = main bot's)

    def threshold(self, main_threshold: int) -> int:
        return max(0, min(100, main_threshold + self.score_offset))

    def to_params(self) -> Dict[str, Any]:
        return {k: v for k, v in dataclasses.asdict(self).items() if k not in ("key", "label", "description")}


def effective_breakeven_pct(strategy: "ShadowStrategy", eff_pct: float, entry: float, initial_stop: Optional[float]) -> float:
    """Breakeven trigger as a gain fraction: breakeven_r x the initial stop distance when set, else eff_pct."""
    if strategy.breakeven_r and initial_stop and entry > initial_stop:
        return strategy.breakeven_r * (entry - initial_stop) / entry
    return eff_pct


PRESETS: Dict[str, ShadowStrategy] = {
    "aggressive": ShadowStrategy(
        key="aggressive",
        label="Aggressive",
        description="Takes setups 10 points below the main threshold, enters at half the pullback depth, "
                    "uses a 25% wider stop, aims for 3R and trails the stop 6% under the high. "
                    "Risks 1% of the portfolio per trade, in at most 5 positions.",
        score_offset=-10,
        risk_per_trade_pct=1.0,
        pullback_scale=0.5,
        stop_scale=1.25,
        reward_risk=3.0,
        trailing_stop_pct=0.06,
        max_positions=5,
    ),
    "momentum": ShadowStrategy(
        key="momentum",
        label="Momentum",
        description="Buys at market as soon as the committee says buy (no waiting for a pullback), "
                    "has no fixed target, trails the stop 8% under the high and exits after 20 days.",
        entry_mode="market",
        use_take_profit=False,
        trailing_stop_pct=0.08,
        max_hold_days=20,
    ),
    "baseline": ShadowStrategy(
        key="baseline",
        label="Baseline",
        description="Same rules as the main bot (as they were when this shadow started). A control group: "
                    "differences against the main portfolio come from the simulation itself (fills, cash timing), "
                    "not from parameters.",
    ),
    "breakeven_1r": ShadowStrategy(
        key="breakeven_1r",
        label="Breakeven at 1R",
        description="Baseline rules plus one change: the stop moves to entry +1% only after the gain equals the "
                    "initial stop distance (1R), instead of the main bot's fixed breakeven percentage.",
        breakeven_r=1.0,
    ),
    "no_breakeven": ShadowStrategy(
        key="no_breakeven",
        label="No breakeven",
        description="Baseline rules plus one change: the breakeven ratchet is off (it needs a 1000% gain, never "
                    "reached), so positions run to the stop or target. The ATR trail never starts either.",
        breakeven_pct=10.0,
    ),
    "score_76": ShadowStrategy(
        key="score_76",
        label="Score 76",
        description="Baseline rules plus one change: takes setups 6 points below the main threshold frozen at "
                    "start, 76 when the main bot is at 82.",
        score_offset=-6,
    ),
}

_FIELD_NAMES = {f.name for f in dataclasses.fields(ShadowStrategy)} - {"key"}


def _custom_strategies() -> Dict[str, ShadowStrategy]:
    raw = (settings.SHADOW_CUSTOM_STRATEGIES or "").strip()
    if not raw:
        return {}
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"SHADOW_CUSTOM_STRATEGIES is not valid JSON, ignoring it: {e}")
        return {}
    if not isinstance(spec, dict):
        logger.warning("SHADOW_CUSTOM_STRATEGIES must be a JSON object, ignoring it.")
        return {}

    custom: Dict[str, ShadowStrategy] = {}
    for key, fields in spec.items():
        key = str(key).strip().lower()[:40]
        if not key or key == MAIN_KEY.lower() or not isinstance(fields, dict):
            continue
        base = PRESETS.get(str(fields.get("base", "baseline")).lower(), PRESETS["baseline"])
        overrides = {k: v for k, v in fields.items() if k in _FIELD_NAMES}
        unknown = set(fields) - _FIELD_NAMES - {"base"}
        if unknown:
            logger.warning(f"Shadow strategy '{key}': ignoring unknown fields {sorted(unknown)}")
        try:
            custom[key] = dataclasses.replace(
                base, key=key, label=overrides.pop("label", key.title()),
                description=overrides.pop("description", f"Custom variant of {base.label}."), **overrides
            )
        except TypeError as e:
            logger.warning(f"Shadow strategy '{key}' is invalid, skipping it: {e}")
    return custom


def _env_strategies() -> List[ShadowStrategy]:
    """Strategies named in SHADOW_STRATEGIES: presets plus SHADOW_CUSTOM_STRATEGIES. Seeds the table; fallback before it is loaded."""
    available = {**PRESETS, **_custom_strategies()}
    result = []
    for key in (settings.SHADOW_STRATEGIES or "").split(","):
        key = key.strip().lower()
        if not key:
            continue
        if key not in available:
            logger.warning(f"Unknown shadow strategy '{key}' in SHADOW_STRATEGIES, skipping it.")
            continue
        if all(s.key != key for s in result):
            result.append(available[key])
    return result


# (strategy, learning_strength) of the enabled DB rows; None = never loaded (env-based behaviour)
_CACHE: Optional[List[tuple]] = None


def reset_strategy_cache() -> None:
    global _CACHE
    _CACHE = None


def _row_to_strategy(row: ShadowStrategyRow) -> ShadowStrategy:
    params = {k: v for k, v in (row.params or {}).items() if k in _FIELD_NAMES - {"label", "description"}}
    return ShadowStrategy(key=row.key, label=row.label, description=row.description or "", **params)


async def load_strategies() -> None:
    """Seeds the table from the env settings when it is empty, then refreshes the cache from the enabled rows."""
    global _CACHE
    async with AsyncSessionLocal() as session:
        # Seed once per database: later an empty table means the user deleted everything
        if await session.get(SystemConfig, "SHADOW_SEEDED") is None:
            session.add(SystemConfig(key="SHADOW_SEEDED", value="true"))
            now = _utcnow()
            for i, s in enumerate(_env_strategies()):
                session.add(ShadowStrategyRow(
                    key=s.key, label=s.label, description=s.description, params=s.to_params(),
                    is_preset=s.key in PRESETS, created_at=now + datetime.timedelta(microseconds=i),
                ))
            await session.commit()
        rows = (await session.execute(
            select(ShadowStrategyRow).where(ShadowStrategyRow.enabled.is_(True)).order_by(ShadowStrategyRow.created_at)
        )).scalars().all()
        _CACHE = [(_row_to_strategy(r), r.learning_strength or 0) for r in rows]


def get_strategies() -> List[ShadowStrategy]:
    """The enabled shadow strategies (from the DB once loaded, else in the order of SHADOW_STRATEGIES)."""
    if not settings.SHADOW_ENABLED:
        return []
    if _CACHE is None:
        return _env_strategies()
    return [s for s, _ in _CACHE]


def learning_strengths() -> Dict[str, int]:
    return {s.key: strength for s, strength in (_CACHE or [])}


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", (label or "").lower()).strip("_")[:40]


async def _get_row(session, key: str) -> ShadowStrategyRow:
    row = await session.get(ShadowStrategyRow, key)
    if not row:
        raise LookupError(f"Unknown shadow strategy '{key}'")
    return row


async def create_strategy(label: str, params: dict, description: str = "", copied_from: Optional[str] = None,
                          learning_strength: int = 0) -> str:
    key = slug(label)
    if not key or key == MAIN_KEY.lower():
        raise ValueError("Strategy name is empty or reserved")
    # Defaults for unspecified fields, so the stored row is complete
    full = ShadowStrategy(key=key, label=label, description=description, **validate_params(params)).to_params()
    async with AsyncSessionLocal() as session:
        if await session.get(ShadowStrategyRow, key):
            raise ValueError(f"A strategy with key '{key}' already exists")
        count = (await session.execute(select(func.count()).select_from(ShadowStrategyRow))).scalar_one()
        if count >= settings.SHADOW_MAX_STRATEGIES:
            raise ValueError(f"At most {settings.SHADOW_MAX_STRATEGIES} strategies are allowed")
        session.add(ShadowStrategyRow(key=key, label=_clean_label(label), description=description, params=full,
                                      copied_from=copied_from, learning_strength=learning_strength))
        await session.commit()
    await load_strategies()
    return key


async def copy_strategy(key: str, label: str) -> str:
    async with AsyncSessionLocal() as session:
        row = await _get_row(session, key)
        params, description = dict(row.params), row.description or ""
    return await create_strategy(label, params, description, copied_from=key)


def _clean_label(label: str) -> str:
    label = (label or "").strip()
    if not label:
        raise ValueError("Strategy name is empty")
    return label[:60]


async def edit_strategy(key: str, *, label=None, description=None, learning_strength=None, enabled=None,
                        params: Optional[dict] = None, source: str = "user", reason: str = "",
                        stats: Optional[dict] = None) -> dict:
    """Saves the given fields, changed params and their history row in one commit; returns the param changes.

    Open positions keep their levels: nothing else is touched. Nothing is written when any input is invalid.
    """
    if learning_strength is not None and not 0 <= learning_strength <= 3:
        raise ValueError("learning_strength must be 0..3")
    if label is not None:
        label = _clean_label(label)
    changes: dict = {}
    async with AsyncSessionLocal() as session:
        row = await _get_row(session, key)
        if params is not None:
            old = {k: v for k, v in row.params.items() if k in SPECS}  # drop keys no longer in the schema
            merged = {**old, **validate_params(params)}
            changes = {k: {"old": old.get(k), "new": merged[k]} for k in params if old.get(k) != merged[k]}
            if changes:
                row.params = merged
                session.add(ShadowParamChange(strategy=key, source=source, changes=changes, reason=reason, stats=stats))
        for name, value in (("label", label), ("description", description),
                            ("learning_strength", learning_strength), ("enabled", enabled)):
            if value is not None:
                setattr(row, name, value)
        await session.commit()
    await load_strategies()
    return changes


async def apply_param_change(key: str, new_params: dict, source: str, reason: str = "",
                             stats: Optional[dict] = None) -> dict:
    return await edit_strategy(key, params=new_params, source=source, reason=reason, stats=stats)


async def update_strategy(key: str, *, label=None, description=None, learning_strength=None, enabled=None) -> None:
    await edit_strategy(key, label=label, description=description, learning_strength=learning_strength, enabled=enabled)


async def delete_strategy(key: str) -> bool:
    async with shadow_portfolios._lock:
        async with AsyncSessionLocal() as session:
            row = await session.get(ShadowStrategyRow, key)
            if not row:
                return False
            for model in (ShadowPosition, ShadowTrigger, ShadowSnapshot):
                await session.execute(delete(model).where(model.strategy == key))
            await session.execute(delete(ShadowAccount).where(ShadowAccount.strategy == key))
            await session.delete(row)
            await session.commit()
        # Reload before releasing the lock, so a waiting job never sees the deleted strategy
        await load_strategies()
    return True


async def undo_param_change(change_id: int) -> dict:
    async with AsyncSessionLocal() as session:
        change = await session.get(ShadowParamChange, change_id)
        if not change:
            raise LookupError(f"Unknown param change {change_id}")
        key, old = change.strategy, {k: v["old"] for k, v in (change.changes or {}).items()}
    return await apply_param_change(key, old, source="undo", reason=f"Undo change {change_id}")


def plan_entry(
    strategy: ShadowStrategy,
    trigger_type: str,
    trigger_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    current_price: float,
) -> Dict[str, Any]:
    """Maps the main bot's entry/stop/target onto a strategy's own levels (all in native currency)."""
    if strategy.entry_mode == "market":
        entry_type, entry = "MARKET", current_price
    elif trigger_type == "LIMIT_PULLBACK":
        entry_type = trigger_type
        entry = current_price - (current_price - trigger_price) * strategy.pullback_scale
        if entry >= current_price:  # pullback_scale 0 means "buy now"
            entry_type, entry = "MARKET", current_price
    else:
        entry_type, entry = trigger_type, trigger_price

    stop_distance = (trigger_price - stop_loss) if stop_loss else trigger_price * settings.DEFAULT_STOP_LOSS_PCT
    stop_distance = max(stop_distance, entry * 0.005) * strategy.stop_scale
    stop = entry - stop_distance

    if not strategy.use_take_profit:
        target = None
    elif strategy.reward_risk:
        target = entry + strategy.reward_risk * stop_distance
    else:
        target = take_profit if take_profit and take_profit > entry else entry * (1 + settings.DEFAULT_TAKE_PROFIT_PCT)

    return {
        "trigger_type": entry_type,
        "target_price": round(entry, 2),
        "stop_loss": round(stop, 2) if stop > 0 else None,
        "take_profit": round(target, 2) if target else None,
    }


class ShadowPortfolios:
    def __init__(self):
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup

    async def _initial_capital(self) -> float:
        """SHADOW_INITIAL_CAPITAL when set, else the main account's current value, so both start equal."""
        if settings.SHADOW_INITIAL_CAPITAL:
            return settings.SHADOW_INITIAL_CAPITAL
        try:
            from src.trading.broker import get_active_broker
            main_value = (await get_active_broker().get_portfolio_summary()).get("total_value_eur")
        except Exception as e:
            logger.warning(f"Shadow start capital: main account value unavailable ({e}), using INITIAL_PAPER_CAPITAL.")
            main_value = None
        return main_value or settings.INITIAL_PAPER_CAPITAL

    async def _main_params(self, session) -> Dict[str, Any]:
        """The main bot's current values for the parameters a shadow builds on (as the learner left them)."""
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
        row = res.scalars().first()
        try:
            trigger_min_score = int(row.value) if row and row.value else settings.TRIGGER_MIN_SCORE
        except ValueError:
            trigger_min_score = settings.TRIGGER_MIN_SCORE
        return {
            "trigger_min_score": trigger_min_score,
            "breakeven_pct": await self._breakeven_pct(session),
            "max_positions": settings.MAX_POSITIONS,
            "risk_per_trade_pct": settings.RISK_PER_TRADE_PCT,
            "trailing_atr_mult": settings.TRAILING_STOP_ATR_MULT,
            "regime_filter": settings.REGIME_FILTER_ENABLED,
        }

    @staticmethod
    def _effective(s: ShadowStrategy, acc: ShadowAccount) -> Dict[str, Any]:
        """A strategy's working values: its own settings, else the main bot's values frozen at the shadow's start."""
        frozen = acc.frozen_params or {}

        def own_or_main(own, key, fallback):
            # Shadows started before a main parameter existed fall back to the main bot's current value
            return own if own is not None else frozen.get(key, fallback)

        return {
            "trigger_min_score": s.threshold(frozen.get("trigger_min_score", settings.TRIGGER_MIN_SCORE)),
            "breakeven_pct": own_or_main(s.breakeven_pct, "breakeven_pct", 0.025),
            "max_positions": s.max_positions or frozen.get("max_positions") or settings.MAX_POSITIONS,
            "risk_per_trade_pct": own_or_main(s.risk_per_trade_pct, "risk_per_trade_pct", settings.RISK_PER_TRADE_PCT),
            "trailing_atr_mult": own_or_main(s.trailing_atr_mult, "trailing_atr_mult", settings.TRAILING_STOP_ATR_MULT),
            "regime_filter": own_or_main(s.regime_filter, "regime_filter", settings.REGIME_FILTER_ENABLED),
        }

    @staticmethod
    def _regime_allows(ticker: str, currency: Optional[str]) -> bool:
        """Same test as the main bot's regime filter, but switched by the shadow's own setting. Fails open."""
        regime = market_data_provider.get_index_regime(regime_index_for_ticker(ticker, currency), settings.REGIME_SMA_DAYS)
        return not regime or bool(regime.get("above_sma"))

    async def _ensure_accounts(self, session, strategies: List[ShadowStrategy]) -> Dict[str, ShadowAccount]:
        res = await session.execute(select(ShadowAccount))
        accounts = {a.strategy: a for a in res.scalars().all()}
        changed = False
        main_params = None
        for s in strategies:
            if s.key not in accounts:
                main_params = main_params or await self._main_params(session)
                cap = await self._initial_capital()
                acc = ShadowAccount(strategy=s.key, cash_eur=cap, initial_capital_eur=cap,
                                    frozen_params=dict(main_params), started_at=_utcnow())
                session.add(acc)
                accounts[s.key] = acc
                changed = True
                logger.info(f"Shadow portfolio '{s.key}' started with €{cap:.2f}.")
            else:
                # A shadow started before a main parameter existed freezes that parameter from now on
                acc = accounts[s.key]
                frozen = acc.frozen_params or {}
                main_params = main_params or await self._main_params(session)
                missing = {k: v for k, v in main_params.items() if k not in frozen}
                if missing:
                    acc.frozen_params = {**frozen, **missing}
                    changed = True
        changed = await self._apply_main_cash_flows(accounts.values()) or changed
        if changed:
            await session.commit()
        return accounts

    @staticmethod
    async def _apply_main_cash_flows(accounts) -> bool:
        """Books the main account's deposits and withdrawals since each shadow's start into its cash, so both
        work with the same money. Only the change since the last call is booked, so a flow deleted by hand is
        taken back. A withdrawal can push cash below zero; the shadow then buys nothing until a sale refills it."""
        main_is_live = settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED
        flows = await list_cash_flow_points(is_live=main_is_live)
        changed = False
        for acc in accounts:
            total = round(sum(a for t, a in flows if acc.started_at is None or t > acc.started_at), 2)
            delta = round(total - (acc.cash_flows_eur or 0.0), 2)
            if delta:
                acc.cash_eur = round(acc.cash_eur + delta, 2)
                acc.cash_flows_eur = total
                changed = True
                logger.info(f"Shadow '{acc.strategy}' followed a main deposit/withdrawal of €{delta:.2f}.")
        return changed

    async def _breakeven_pct(self, session) -> float:
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT"))
        row = res.scalars().first()
        try:
            return float(row.value) if row else 0.025
        except ValueError:
            return 0.025

    @staticmethod
    async def _quote(ticker: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(market_data_provider.get_stock_data, ticker)

    # ------------------------------------------------------------------ entries

    async def follow_report(
        self,
        report: Dict[str, Any],
        data: Dict[str, Any],
        followed_main: bool = False,
    ) -> List[Dict[str, Any]]:
        """Lets every shadow strategy decide on a committee verdict the main bot just received."""
        if not get_strategies():
            return []
        try:
            async with self._lock:
                # Read under the lock: a strategy deleted while this job waited must not get an account back
                armed = await self._follow_report(report, data, followed_main, get_strategies())
            if any(a["trigger_type"] == "MARKET" for a in armed):
                await self.check_triggers()
            return armed
        except Exception as e:
            logger.error(f"Shadow portfolios could not follow the report for {report.get('ticker')}: {e}", exc_info=True)
            return []

    async def _follow_report(self, report, data, followed_main, strategies) -> List[Dict[str, Any]]:
        ticker = (report.get("ticker") or data.get("ticker") or "").upper()
        price = data.get("price")
        if not ticker or not price or price <= 0:
            return []

        action = report.get("action")
        approved = report.get("risk", {}).get("approved", True)
        score = report.get("consensus_score") or 0
        trigger_type, trigger_price, sl, tp = derive_trigger_levels(report, price)

        armed = []
        async with AsyncSessionLocal() as session:
            accounts = await self._ensure_accounts(session, strategies)
            for s in strategies:
                if score < self._effective(s, accounts[s.key])["trigger_min_score"]:
                    continue
                if not (action in ("BUY", "STRONG_BUY") or (s.accept_hold and action == "HOLD")):
                    continue
                if not approved and not s.ignore_risk_veto:
                    continue

                held = await session.execute(select(ShadowPosition.id).where(
                    ShadowPosition.strategy == s.key, ShadowPosition.ticker == ticker, ShadowPosition.status == "OPEN"))
                pending = await session.execute(select(ShadowTrigger.id).where(
                    ShadowTrigger.strategy == s.key, ShadowTrigger.ticker == ticker, ShadowTrigger.status == "PENDING"))
                if held.first() or pending.first():
                    continue

                plan = plan_entry(s, trigger_type, trigger_price, sl, tp, price)
                trig = ShadowTrigger(
                    strategy=s.key,
                    ticker=ticker,
                    name=data.get("name", ticker),
                    currency=data.get("currency", "USD"),
                    trigger_type=plan["trigger_type"],
                    target_price=plan["target_price"],
                    current_price_at_creation=round(price, 2),
                    stop_loss=plan["stop_loss"],
                    take_profit=plan["take_profit"],
                    consensus_score=score,
                    followed_main=followed_main,
                    status="PENDING",
                    created_at=_utcnow(),
                )
                session.add(trig)
                armed.append({"strategy": s.key, "ticker": ticker, **plan})
            if armed:
                await session.commit()
                logger.info(f"Shadow triggers armed for {ticker}: {[a['strategy'] for a in armed]}")
        return armed

    async def check_triggers(self) -> List[Dict[str, Any]]:
        """Fills pending shadow triggers whose price level is hit while the market is open."""
        if not get_strategies():
            return []
        try:
            async with self._lock:
                return await self._check_triggers({s.key: s for s in get_strategies()})
        except Exception as e:
            logger.error(f"Shadow trigger check failed: {e}", exc_info=True)
            return []

    async def _check_triggers(self, strategies: Dict[str, ShadowStrategy]) -> List[Dict[str, Any]]:
        filled = []
        quotes: Dict[str, Optional[Dict[str, Any]]] = {}
        regimes: Dict[str, bool] = {}
        async with AsyncSessionLocal() as session:
            accounts = await self._ensure_accounts(session, list(strategies.values()))
            res = await session.execute(select(ShadowTrigger).where(ShadowTrigger.status == "PENDING").order_by(
                desc(ShadowTrigger.consensus_score), ShadowTrigger.created_at))
            for t in res.scalars().all():
                s = strategies.get(t.strategy)
                if not s:
                    continue  # strategy switched off: keep its state, stop trading it
                if is_trigger_expired(t.created_at):
                    t.status = "EXPIRED"
                    continue
                is_open, _ = check_market_hours(t.ticker)
                if not is_open:
                    continue
                if t.ticker not in quotes:
                    quotes[t.ticker] = await self._quote(t.ticker)
                data = quotes[t.ticker]
                if not data or not data.get("price"):
                    continue
                price = data["price"]

                if t.trigger_type == "MARKET":
                    met = True
                    invalid = f"price {price} already at/below stop-loss {t.stop_loss}" if t.stop_loss and price <= t.stop_loss else None
                else:
                    met, invalid = evaluate_trigger_condition(t.trigger_type, t.target_price, price, t.stop_loss, t.take_profit)
                if invalid:
                    t.status = "CANCELLED"
                    t.note = invalid
                    continue
                if not met:
                    continue

                if self._effective(s, accounts[s.key])["regime_filter"]:
                    index = regime_index_for_ticker(t.ticker, t.currency)
                    if index not in regimes:
                        regimes[index] = await asyncio.to_thread(self._regime_allows, t.ticker, t.currency)
                    if not regimes[index]:
                        continue  # stays pending (and expires as usual) while the market is in a downtrend

                fill = await self._open_position(session, s, accounts[s.key], t, price)
                if fill == "UNAFFORDABLE":
                    t.status = "CANCELLED"
                    t.note = "Not even one whole share fits the allocation."
                elif fill:
                    t.status = "TRIGGERED"
                    t.triggered_at = _utcnow()
                    filled.append(fill)
            await session.commit()
        return filled

    async def _open_position(self, session, s: ShadowStrategy, account: ShadowAccount, t: ShadowTrigger, price: float):
        res = await session.execute(select(ShadowPosition).where(
            ShadowPosition.strategy == s.key, ShadowPosition.status == "OPEN"))
        open_positions = res.scalars().all()
        max_positions = self._effective(s, account)["max_positions"]
        if len(open_positions) >= max_positions or any(p.ticker == t.ticker for p in open_positions):
            return None  # stays pending until a slot frees up (or it expires)

        eff = self._effective(s, account)
        usable_cash, _ = get_effective_deployable_cash(account.cash_eur)
        alloc = calculate_position_allocation(
            available_cash=usable_cash,
            current_positions_count=len(open_positions),
            full_budget_utilization=True,
            max_positions=max_positions,
            ticker=t.ticker,
            currency=t.currency,
        )
        # Same risk budget as the main bot's automated buys: a wide stop gets a smaller position
        portfolio_value = account.cash_eur + sum(p.current_price_eur * p.shares for p in open_positions)
        alloc = apply_risk_budget(alloc, price, t.stop_loss, portfolio_value, risk_pct=eff["risk_per_trade_pct"])
        price_eur = currency_service.to_eur(price, t.currency)
        if alloc < 20.0 or price_eur <= 0:
            return None

        from src.trading.fees import calculate_broker_fee

        def fee_for(n: float) -> float:
            return calculate_broker_fee(
                ticker=t.ticker, shares=n, price_native=price, currency=t.currency,
                broker=settings.LIVE_BROKER, order_action="BUY",
            )["fee_eur"]

        if settings.WHOLE_SHARES_ONLY:
            # Like the main bot: the whole number of shares closest to the allocation
            shares = float(whole_share_count(alloc, price_eur, max_eur=usable_cash))
            if shares < 1:
                return "UNAFFORDABLE"
            fee = fee_for(shares)
            while shares >= 1 and shares * price_eur + fee > account.cash_eur:
                shares -= 1
                fee = fee_for(shares)
            if shares < 1:
                return None
        else:
            shares = round(alloc / price_eur, 4)
            fee = fee_for(shares)
            if shares * price_eur + fee > account.cash_eur:
                shares = math.floor(((account.cash_eur - fee) / price_eur) * 10000.0) / 10000.0
        cost = round(shares * price_eur, 2)
        if shares <= 0 or cost < 20.0:
            return None

        account.cash_eur = round(account.cash_eur - cost - fee, 2)
        pos = ShadowPosition(
            strategy=s.key,
            trigger_id=t.id,
            ticker=t.ticker,
            name=t.name,
            currency=t.currency,
            shares=shares,
            entry_price_native=price,
            current_price_native=price,
            highest_price_native=price,
            stop_loss_native=t.stop_loss,
            initial_stop_native=t.stop_loss,
            take_profit_native=t.take_profit,
            entry_price_eur=price_eur,
            current_price_eur=price_eur,
            cost_basis_eur=cost,
            fees_eur=fee,
            consensus_score=t.consensus_score,
            followed_main=t.followed_main,
            status="OPEN",
            opened_at=_utcnow(),
        )
        session.add(pos)
        logger.info(f"Shadow '{s.key}' bought {shares} {t.ticker} @ {price} {t.currency} (€{cost:.2f}, fee €{fee:.2f})")
        return {"strategy": s.key, "ticker": t.ticker, "shares": shares, "price_native": price, "cost_eur": cost}

    # ------------------------------------------------------------------ exits

    async def check_exits(self) -> List[Dict[str, Any]]:
        """Updates prices, ratchets stops and closes shadow positions that hit their exit rules."""
        if not get_strategies():
            return []
        try:
            async with self._lock:
                closed = await self._check_exits({s.key: s for s in get_strategies()})
        except Exception as e:
            logger.error(f"Shadow exit check failed: {e}", exc_info=True)
            return []
        # Outside the lock and after the commit, so a slow memory DB never blocks shadow monitoring
        for c in closed:
            try:
                await record_outcome(
                    source="shadow", strategy=c["strategy"], ref_id=c["position_id"], ticker=c["ticker"],
                    pnl_pct=c["pnl_pct"], r_multiple=c["r_multiple"], hold_days=c["hold_days"],
                    exit_reason=c["reason"], score=c["score"], entry_type=c["entry_type"], params=c["params"],
                )
            except Exception as e:
                logger.warning(f"Shadow outcome not recorded for {c['ticker']}: {e}")
        return closed

    async def _check_exits(self, strategies: Dict[str, ShadowStrategy]) -> List[Dict[str, Any]]:
        closed = []
        trigger_ids: Dict[int, Optional[int]] = {}
        now = _utcnow()
        quotes: Dict[str, Optional[Dict[str, Any]]] = {}
        async with AsyncSessionLocal() as session:
            accounts = await self._ensure_accounts(session, list(strategies.values()))
            res = await session.execute(select(ShadowPosition).where(ShadowPosition.status == "OPEN"))
            for p in res.scalars().all():
                s = strategies.get(p.strategy)
                if not s:
                    continue
                is_open, _ = check_market_hours(p.ticker)
                if not is_open:
                    continue
                if p.ticker not in quotes:
                    quotes[p.ticker] = await self._quote(p.ticker)
                data = quotes[p.ticker]
                if not data or not data.get("price"):
                    continue
                price = data["price"]
                p.current_price_native = price
                p.current_price_eur = currency_service.to_eur(price, p.currency)
                p.highest_price_native = max(p.highest_price_native or price, price)

                eff = self._effective(s, accounts[s.key])
                # Main bot's ratchet: breakeven +1% past breakeven_pct, then high - N x ATR
                p.stop_loss_native = compute_trailing_stop(
                    p.entry_price_native, p.stop_loss_native, p.highest_price_native,
                    (data.get("technicals") or {}).get("atr14"),
                    effective_breakeven_pct(s, eff["breakeven_pct"], p.entry_price_native, p.initial_stop_native),
                    atr_mult=eff["trailing_atr_mult"],
                )
                if s.trailing_stop_pct:
                    trail = round(p.highest_price_native * (1.0 - s.trailing_stop_pct), 2)
                    if p.stop_loss_native is None or trail > p.stop_loss_native:
                        p.stop_loss_native = trail

                reason = None
                if p.stop_loss_native and price <= p.stop_loss_native:
                    reason = f"Stop-Loss hit at {price} <= {p.stop_loss_native}"
                elif p.take_profit_native and price >= p.take_profit_native:
                    reason = f"Take-Profit hit at {price} >= {p.take_profit_native}"
                elif s.max_hold_days and p.opened_at and (now - p.opened_at) > datetime.timedelta(days=s.max_hold_days):
                    reason = f"Time exit after {s.max_hold_days} days"
                if reason:
                    closed.append(self._close_position(accounts[s.key], p, price, reason, now))
                    closed[-1]["params"] = s.to_params()
                    trigger_ids[p.id] = p.trigger_id
            if trigger_ids:
                wanted = {t for t in trigger_ids.values() if t is not None}
                types = {}
                if wanted:
                    rows = await session.execute(
                        select(ShadowTrigger.id, ShadowTrigger.trigger_type).where(ShadowTrigger.id.in_(wanted)))
                    types = {i: t for i, t in rows.all()}
                for c in closed:
                    c["entry_type"] = types.get(trigger_ids[c["position_id"]]) or "UNKNOWN"
            await session.commit()
        return closed

    @staticmethod
    def _close_position(account: ShadowAccount, p: ShadowPosition, price: float, reason: str, now) -> Dict[str, Any]:
        from src.trading.fees import calculate_broker_fee
        price_eur = currency_service.to_eur(price, p.currency)
        proceeds = round(p.shares * price_eur, 2)
        fee = calculate_broker_fee(
            ticker=p.ticker, shares=p.shares, price_native=price, currency=p.currency,
            broker=settings.LIVE_BROKER, order_action="SELL",
        )["fee_eur"]
        total_fees = round((p.fees_eur or 0.0) + fee, 2)
        pnl = round(proceeds - p.cost_basis_eur - total_fees, 2)

        account.cash_eur = round(account.cash_eur + proceeds - fee, 2)
        p.status = "CLOSED"
        p.exit_price_native = price
        p.current_price_native = price
        p.current_price_eur = price_eur
        p.fees_eur = total_fees
        p.realized_pnl_eur = pnl
        p.realized_pnl_pct = round(pnl / p.cost_basis_eur * 100, 2) if p.cost_basis_eur > 0 else 0.0
        p.exit_reason = reason
        p.closed_at = now
        logger.info(f"Shadow '{p.strategy}' sold {p.shares} {p.ticker} @ {price}: €{pnl:+.2f} ({reason})")
        stop = p.initial_stop_native
        r_multiple = round((price - p.entry_price_native) / (p.entry_price_native - stop), 2) \
            if stop and stop < p.entry_price_native else None
        hold_days = (now - p.opened_at).total_seconds() / 86400 if p.opened_at else 0.0
        return {
            "strategy": p.strategy, "ticker": p.ticker, "realized_pnl_eur": pnl, "reason": reason,
            "position_id": p.id, "pnl_pct": p.realized_pnl_pct, "r_multiple": r_multiple, "hold_days": hold_days,
            "score": p.consensus_score, "entry_type": "UNKNOWN", "params": {},
        }

    # ------------------------------------------------------------------ reporting

    async def record_snapshots(self, main_summary_fn: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None) -> None:
        """Stores roughly hourly values of every shadow and of the main portfolio, for the comparison chart."""
        if not get_strategies():
            return
        try:
            async with self._lock:
                strategies = get_strategies()
                async with AsyncSessionLocal() as session:
                    accounts = await self._ensure_accounts(session, strategies)
                    now = _utcnow()
                    last = await session.execute(
                        select(ShadowSnapshot.strategy, func.max(ShadowSnapshot.timestamp)).group_by(ShadowSnapshot.strategy))
                    last_ts = dict(last.all())
                    due_keys = [k for k in [MAIN_KEY] + [st.key for st in strategies]
                                if not last_ts.get(k) or now - last_ts[k] >= SNAPSHOT_INTERVAL]
                    due = bool(due_keys)
                    needs_start = any(a.main_value_at_start_eur is None for a in accounts.values())
                    if not due and not needs_start:
                        return

                    # Only ask the main broker for its value when it is needed (a live broker is a network call)
                    main_value = None
                    if main_summary_fn:
                        try:
                            main_value = (await main_summary_fn()).get("total_value_eur")
                        except Exception as e:
                            logger.debug(f"Shadow snapshot: main portfolio value unavailable: {e}")
                    if main_value:
                        for acc in accounts.values():
                            if acc.main_value_at_start_eur is None:
                                acc.main_value_at_start_eur = main_value
                    if due:
                        if main_value and MAIN_KEY in due_keys:
                            session.add(ShadowSnapshot(strategy=MAIN_KEY, timestamp=now, total_value_eur=round(main_value, 2)))
                        for s in strategies:
                            if s.key not in due_keys:
                                continue
                            acc = accounts[s.key]
                            value = await self._total_value(session, acc)
                            ret = _return_pct(value, acc.initial_capital_eur, acc.cash_flows_eur or 0.0)
                            session.add(ShadowSnapshot(strategy=s.key, timestamp=now, total_value_eur=value, return_pct=ret))
                    await session.commit()
        except Exception as e:
            logger.error(f"Shadow snapshot failed: {e}", exc_info=True)

    @staticmethod
    async def _total_value(session, acc: ShadowAccount) -> float:
        res = await session.execute(select(ShadowPosition).where(
            ShadowPosition.strategy == acc.strategy, ShadowPosition.status == "OPEN"))
        equity = sum(p.current_price_eur * p.shares for p in res.scalars().all())
        return round(acc.cash_eur + equity, 2)

    @staticmethod
    def _trade_stats(pnls: List[float]) -> Dict[str, Any]:
        wins = sum(1 for x in pnls if x > 0)
        losses = sum(1 for x in pnls if x < 0)
        return {
            "closed_trades": len(pnls),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wins / len(pnls) * 100, 1) if pnls else None,
            "realized_pnl_eur": round(sum(pnls), 2),
            "avg_trade_eur": round(sum(pnls) / len(pnls), 2) if pnls else None,
        }

    @staticmethod
    async def _mark_followed_by_main(session, main_is_live: bool) -> None:
        """Clears 'Shadow only' once the main bot holds or arms the same ticker later (manual triggers included)."""
        main_tickers = select(OrderTrigger.ticker).where(
            OrderTrigger.status == "PENDING", OrderTrigger.is_live == main_is_live
        ).union(select(Position.ticker).where(Position.status == "OPEN", Position.is_live == main_is_live))
        for model, status in ((ShadowTrigger, "PENDING"), (ShadowPosition, "OPEN")):
            await session.execute(update(model).where(
                model.status == status, model.followed_main.isnot(True), model.ticker.in_(main_tickers)
            ).values(followed_main=True))
        await session.commit()

    async def get_comparison(self, main_summary: Dict[str, Any], main_is_live: bool) -> Dict[str, Any]:
        """Side-by-side numbers for the main portfolio and every enabled shadow, each over the shadow's own period."""
        strategies = get_strategies()
        result: Dict[str, Any] = {
            "enabled": bool(strategies),
            "configured": settings.SHADOW_STRATEGIES,
            "presets": [{"key": p.key, "label": p.label, "description": p.description} for p in PRESETS.values()],
            "main": {
                "label": "Live" if main_is_live else "Paper",
                "trading_mode": main_summary.get("trading_mode"),
                "total_value_eur": main_summary.get("total_value_eur"),
                "open_positions_count": main_summary.get("open_positions_count"),
            },
            "strategies": [],
        }
        if not strategies:
            return result

        main_value = main_summary.get("total_value_eur")
        main_flows = await list_cash_flow_points(is_live=main_is_live)
        async with AsyncSessionLocal() as session:
            # Locked: booking a deposit into cash must not race a shadow buy on the same account
            async with self._lock:
                strategies = get_strategies()
                accounts = await self._ensure_accounts(session, strategies)
                await self._mark_followed_by_main(session, main_is_live)
            current_main = await self._main_params(session)
            meta = {r.key: r for r in (await session.execute(select(ShadowStrategyRow))).scalars().all()}
            for s in strategies:
                acc = accounts[s.key]
                since = acc.started_at
                if acc.main_value_at_start_eur is None and main_value:
                    acc.main_value_at_start_eur = main_value
                    await session.commit()

                open_res = await session.execute(select(ShadowPosition).where(
                    ShadowPosition.strategy == s.key, ShadowPosition.status == "OPEN").order_by(desc(ShadowPosition.opened_at)))
                open_positions = open_res.scalars().all()
                closed_res = await session.execute(select(ShadowPosition).where(
                    ShadowPosition.strategy == s.key, ShadowPosition.status == "CLOSED").order_by(desc(ShadowPosition.closed_at)))
                closed = closed_res.scalars().all()
                trig_res = await session.execute(select(ShadowTrigger).where(
                    ShadowTrigger.strategy == s.key, ShadowTrigger.status == "PENDING").order_by(desc(ShadowTrigger.created_at)))
                pending = trig_res.scalars().all()

                equity = sum(p.current_price_eur * p.shares for p in open_positions)
                value = round(acc.cash_eur + equity, 2)

                main_sells = await session.execute(select(Trade.realized_pnl_eur).where(
                    Trade.order_type == "SELL", Trade.is_live == main_is_live, Trade.executed_at >= since))
                main_pnls = [x for x in main_sells.scalars().all() if x is not None]
                main_start = acc.main_value_at_start_eur
                # Deposits into the main account after the shadow started are not main-bot gain
                main_flows_since = sum(a for t, a in main_flows if since is None or t > since)
                main_return = _return_pct(main_value, main_start, main_flows_since) if main_value and main_start else None

                hist_res = await session.execute(select(ShadowSnapshot).where(
                    ShadowSnapshot.strategy.in_([s.key, MAIN_KEY]), ShadowSnapshot.timestamp >= since
                ).order_by(ShadowSnapshot.timestamp))
                history = {"shadow": [], "main": []}
                for snap in hist_res.scalars().all():
                    if snap.strategy == MAIN_KEY:
                        if main_start:
                            flows = sum(a for t, a in main_flows if (since is None or t > since) and t <= snap.timestamp)
                            history["main"].append([snap.timestamp.strftime("%Y-%m-%d %H:%M"),
                                                    _return_pct(snap.total_value_eur, main_start, flows)])
                    else:
                        history["shadow"].append([snap.timestamp.strftime("%Y-%m-%d %H:%M"), snap.return_pct])

                params = s.to_params()
                eff = self._effective(s, acc)
                params["effective_breakeven_pct"] = eff["breakeven_pct"]
                params["effective_max_positions"] = eff["max_positions"]
                params["effective_trigger_min_score"] = eff["trigger_min_score"]
                params["effective_risk_per_trade_pct"] = eff["risk_per_trade_pct"]
                params["effective_trailing_atr_mult"] = eff["trailing_atr_mult"]
                params["effective_regime_filter"] = eff["regime_filter"]
                params["whole_shares"] = settings.WHOLE_SHARES_ONLY
                params["frozen_main_params"] = acc.frozen_params or {}
                params["current_main_params"] = current_main

                result["strategies"].append({
                    "key": s.key,
                    "label": s.label,
                    "description": s.description,
                    "params": params,
                    "learning_strength": (meta[s.key].learning_strength or 0) if s.key in meta else 0,
                    "copied_from": meta[s.key].copied_from if s.key in meta else None,
                    "is_preset": bool(meta[s.key].is_preset) if s.key in meta else s.key in PRESETS,
                    "started_at": since.strftime("%Y-%m-%d %H:%M") if since else None,
                    "initial_capital_eur": round(acc.initial_capital_eur, 2),
                    "cash_eur": round(acc.cash_eur, 2),
                    "equity_eur": round(equity, 2),
                    "total_value_eur": value,
                    "return_pct": _return_pct(value, acc.initial_capital_eur, acc.cash_flows_eur or 0.0),
                    "unrealized_pnl_eur": round(sum(p.current_price_eur * p.shares - p.cost_basis_eur for p in open_positions), 2),
                    "fees_eur": round(sum(p.fees_eur or 0.0 for p in list(open_positions) + list(closed)), 2),
                    **self._trade_stats([p.realized_pnl_eur for p in closed if p.realized_pnl_eur is not None]),
                    "shadow_only_trades": sum(1 for p in list(open_positions) + list(closed) if not p.followed_main),
                    "main_same_period": {
                        "return_pct": main_return,
                        **self._trade_stats(main_pnls),
                    },
                    "open_positions": [{
                        "id": p.id,
                        "ticker": p.ticker,
                        "name": p.name,
                        "currency": p.currency,
                        "shares": round(p.shares, 4),
                        "entry_price_native": round(p.entry_price_native, 2),
                        "current_price_native": round(p.current_price_native, 2),
                        "stop_loss_native": round(p.stop_loss_native, 2) if p.stop_loss_native else None,
                        "take_profit_native": round(p.take_profit_native, 2) if p.take_profit_native else None,
                        "unrealized_pnl_eur": round(p.current_price_eur * p.shares - p.cost_basis_eur, 2),
                        "unrealized_pnl_pct": round((p.current_price_eur / p.entry_price_eur - 1.0) * 100, 2) if p.entry_price_eur else 0.0,
                        "followed_main": bool(p.followed_main),
                        "opened_at": p.opened_at.strftime("%Y-%m-%d %H:%M") if p.opened_at else "",
                    } for p in open_positions],
                    "pending_triggers": [{
                        "id": t.id,
                        "ticker": t.ticker,
                        "trigger_type": t.trigger_type,
                        "target_price": t.target_price,
                        "current_price_at_creation": t.current_price_at_creation,
                        "currency": t.currency,
                        "consensus_score": t.consensus_score,
                        "followed_main": bool(t.followed_main),
                        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
                    } for t in pending],
                    "recent_closed": [{
                        "id": p.id,
                        "ticker": p.ticker,
                        "realized_pnl_eur": p.realized_pnl_eur,
                        "realized_pnl_pct": p.realized_pnl_pct,
                        "exit_reason": p.exit_reason,
                        "followed_main": bool(p.followed_main),
                        "closed_at": p.closed_at.strftime("%Y-%m-%d %H:%M") if p.closed_at else "",
                    } for p in closed[:10]],
                    "history": history,
                })
        return result

    async def reset(self, strategy_key: str) -> bool:
        """Wipes one shadow portfolio's simulated state; it restarts with fresh capital on the next check."""
        async with self._lock:
            async with AsyncSessionLocal() as session:
                acc = await session.get(ShadowAccount, strategy_key)
                if not acc:
                    return False
                await session.execute(delete(ShadowPosition).where(ShadowPosition.strategy == strategy_key))
                await session.execute(delete(ShadowTrigger).where(ShadowTrigger.strategy == strategy_key))
                await session.execute(delete(ShadowSnapshot).where(ShadowSnapshot.strategy == strategy_key))
                await session.delete(acc)
                await session.execute(update(ShadowStrategyRow).where(ShadowStrategyRow.key == strategy_key)
                                      .values(closed_at_last_learn=0))
                await session.commit()
        logger.info(f"Shadow portfolio '{strategy_key}' was reset.")
        return True


shadow_portfolios = ShadowPortfolios()
