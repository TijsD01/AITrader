"""
Numeric track record of the bot's closed trades, for the committee prompt.

Numbers with their sample size replace free-text lessons: a handful of trade stories anchors the
LLM, while a count below MIN_N is stated as "too few to judge" instead of a win rate.
"""
from typing import Optional

from sqlalchemy import select

from src.db import session as db_session
from src.db.models import OrderTrigger, Position, Trade

MIN_N = 30

HEADER = "=== TRACK RECORD (closed bot trades, numbers only) ==="


def _too_few(label: str, n: int, min_n: int) -> str:
    return f"{label}: n={n}, below {min_n}, too few to judge"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_pct(pnl_pcts: list[float], min_n: int = MIN_N) -> str:
    """One line over realized P&L percentages. A win is pnl_pct > 0; 0.0 counts as a loss."""
    n = len(pnl_pcts)
    if n < min_n:
        return _too_few("All trades", n, min_n)
    wins = [p for p in pnl_pcts if p > 0]
    losses = [p for p in pnl_pcts if p <= 0]
    return (
        f"All trades: n={n}, win {len(wins) / n * 100:.0f}%, "
        f"avg win {_mean(wins):+.2f}%, avg loss {_mean(losses):+.2f}%, "
        f"expectancy {_mean(pnl_pcts):+.2f}% per trade"
    )


def _r_multiple(entry: float, exit_: float, stop: Optional[float]) -> Optional[float]:
    """R = (exit - entry) / (entry - stop); None when the stop is missing or not below entry."""
    if stop is None or stop >= entry:
        return None
    return (exit_ - entry) / (entry - stop)


def summarize_r(rows: list[tuple[str, float, float, float]], min_n: int = MIN_N) -> list[str]:
    """
    One line per trigger_type seen in rows (first-seen order). rows are (trigger_type, entry, exit,
    initial_stop); n counts only rows with a valid R. A win is R > 0.
    """
    groups: dict[str, list[float]] = {}
    for trigger_type, entry, exit_, stop in rows:
        rs = groups.setdefault(trigger_type, [])
        r = _r_multiple(entry, exit_, stop)
        if r is not None:
            rs.append(r)
    lines = []
    for trigger_type, rs in groups.items():
        n = len(rs)
        if n < min_n:
            lines.append(_too_few(trigger_type, n, min_n))
        else:
            win = sum(1 for r in rs if r > 0) / n * 100
            lines.append(f"{trigger_type}: n={n}, win {win:.0f}%, avg R {_mean(rs):+.2f}")
    return lines


async def track_record_block() -> str:
    """Header, the All-trades line from SELL trades and one R line per entry type from closed triggered positions."""
    # Read AsyncSessionLocal at call time so tests' rebinding to the test DB applies
    async with db_session.AsyncSessionLocal() as session:
        pcts = (await session.execute(
            select(Trade.realized_pnl_pct)
            .where(Trade.order_type == "SELL", Trade.realized_pnl_pct.is_not(None))
            .order_by(Trade.id)
        )).scalars().all()
        r_rows = (await session.execute(
            select(OrderTrigger.trigger_type, Position.entry_price_native,
                   Position.current_price_native, OrderTrigger.stop_loss)
            .join(OrderTrigger, OrderTrigger.id == Position.trigger_id)
            .where(Position.status == "CLOSED")
            .order_by(Position.id)
        )).all()
    lines = [HEADER, summarize_pct(list(pcts))]
    lines.extend(summarize_r([tuple(r) for r in r_rows]))
    return "\n".join(lines)
