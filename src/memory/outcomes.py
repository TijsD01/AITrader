"""Closed-trade outcome atoms: written by trade-close paths, read via memory_store.search_outcomes."""
import asyncio
import json
import logging
from typing import Optional

from src.memory.models import L1Atom
from src.memory.store import memory_store

logger = logging.getLogger(__name__)


async def record_outcome(*, source: str, strategy: str, ref_id: int | str, ticker: str, pnl_pct: float,
                         r_multiple: Optional[float], hold_days: float, exit_reason: str,
                         score: Optional[int], entry_type: str, params: dict) -> bool:
    """Store one closed trade. False if already stored or on any error; never raises."""
    try:
        data = dict(source=source, strategy=strategy, ref_id=ref_id, ticker=ticker, pnl_pct=pnl_pct,
                    r_multiple=r_multiple, hold_days=hold_days, exit_reason=exit_reason, score=score,
                    entry_type=entry_type, params=params)
        atom_id = f"outcome:{source}:{ref_id}"
        await memory_store.init_db()
        # add_l1_atom is INSERT OR REPLACE, so dedupe here.
        async with memory_store._get_conn() as db:
            async with await db.execute("SELECT 1 FROM memory_l1_atoms WHERE atom_id = ?", (atom_id,)) as cur:
                if await cur.fetchone():
                    return False
        summary = f"{source}:{strategy} {ticker} closed {pnl_pct:+.2f}% in {hold_days:.1f}d ({exit_reason})"
        await memory_store.add_l1_atom(L1Atom(
            atom_id=atom_id, agent_id="outcomes", type="episodic", subject=ticker, predicate="closed_trade",
            object=f"{source}:{strategy}", content=f"{summary} ||| {json.dumps(data)}"))
        return True
    except Exception as e:
        logger.warning("record_outcome failed for %s:%s: %s", source, ref_id, e)
        return False


# Strong references to in-flight outcome writes (the event loop only keeps weak ones).
_pending: set = set()


def record_main_outcome(pos, trade, reason: str) -> asyncio.Task:
    """Schedules the outcome write for a main-bot sell so it never holds the sell's lock or DB session.
    Reads the committed Position and SELL Trade rows now; the task gets plain values only and never raises."""
    hold_days = 0.0
    if pos.opened_at and trade.executed_at:
        hold_days = round((trade.executed_at - pos.opened_at).total_seconds() / 86400.0, 2)
    # r_multiple stays None: stop_loss_native is trailed after entry, so the entry stop is not on the row.
    kw = dict(source="main_live" if trade.is_live else "main_paper", strategy="main", ref_id=trade.id,
              ticker=trade.ticker, pnl_pct=trade.realized_pnl_pct or 0.0, r_multiple=None, hold_days=hold_days,
              exit_reason=reason, score=trade.consensus_score, entry_type="UNKNOWN", params={})

    async def write() -> bool:
        try:
            return await record_outcome(**kw)
        except Exception as e:
            logger.warning("Outcome write failed for %s:%s: %s", kw["source"], kw["ref_id"], e)
            return False

    task = asyncio.create_task(write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
