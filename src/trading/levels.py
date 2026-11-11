"""Pure entry/exit level maths shared by the shadow portfolios and the main bot.

No DB, broker or shadow imports: callers pass the strategy as any object with the
ShadowStrategy attributes these functions read.
"""
import datetime
import math
from typing import Any, Dict, Optional

from config.settings import settings


def effective_breakeven_pct(strategy: Any, eff_pct: float, entry: float, initial_stop: Optional[float]) -> float:
    """Breakeven trigger as a gain fraction: breakeven_r x the initial stop distance when set, else eff_pct."""
    if strategy.breakeven_r and initial_stop and entry > initial_stop:
        return strategy.breakeven_r * (entry - initial_stop) / entry
    return eff_pct


def plan_entry(
    strategy: Any,
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


def _positive_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _naive_utc(dt: datetime.datetime) -> datetime.datetime:
    return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def pct_trail_stop(current_stop: Optional[float], highest: float, trailing_stop_pct: Optional[float]) -> Optional[float]:
    """Stop trailing the highest price by trailing_stop_pct; never lowers current_stop.
    Off (None/0), a pct outside (0, 1) or a bad highest price = unchanged."""
    if not _positive_number(trailing_stop_pct) or trailing_stop_pct >= 1 or not _positive_number(highest):
        return current_stop
    trail = round(highest * (1.0 - trailing_stop_pct), 2)
    return trail if current_stop is None or trail > current_stop else current_stop


def max_hold_exceeded(opened_at: Optional[datetime.datetime], now: datetime.datetime, max_hold_days: Optional[int]) -> bool:
    """True once a position has been open strictly longer than max_hold_days (None/0/negative = no limit).
    Timezone-aware datetimes are compared as naive UTC, like the DB stores them."""
    if not _positive_number(max_hold_days) or opened_at is None:
        return False
    return (_naive_utc(now) - _naive_utc(opened_at)) > datetime.timedelta(days=max_hold_days)
