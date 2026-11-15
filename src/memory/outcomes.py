"""Closed-trade outcome atoms: written by trade-close paths, read via memory_store.search_outcomes."""
import asyncio
import json
import logging
from typing import Optional

from sqlalchemy import select

from src.db import session as db_session
from src.db.models import OrderTrigger
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


async def delete_shadow_outcomes(strategy_key: str) -> int:
    """Deletes one shadow strategy's outcome atoms and their monthly summaries. Returns the number deleted;
    0 on any error; never raises."""
    try:
        await memory_store.init_db()
        async with memory_store._get_conn() as db:
            cur = await db.execute(
                "DELETE FROM memory_l1_atoms WHERE agent_id = 'outcomes' AND object = ?", (f"shadow:{strategy_key}",))
            await db.commit()
            return cur.rowcount
    except Exception as e:
        logger.warning("delete_shadow_outcomes failed for %s: %s", strategy_key, e)
        return 0


# Strong references to in-flight outcome writes (the event loop only keeps weak ones).
_pending: set = set()


def record_main_outcome(pos, trade, reason: str) -> asyncio.Task:
    """Schedules the outcome write for a main-bot sell so it never holds the sell's lock or DB session.
    Reads the committed Position and SELL Trade rows now; the task gets plain values only and never raises.
    A manual trade writes nothing: its task returns False."""
    hold_days = 0.0
    if pos.opened_at and trade.executed_at:
        hold_days = round((trade.executed_at - pos.opened_at).total_seconds() / 86400.0, 2)
    # Same R as shadow outcomes: measured against the stop the position opened with, not the trailed one.
    entry, stop = pos.entry_price_native, pos.initial_stop_native
    r_multiple = round((trade.price_native - entry) / (entry - stop), 2) if stop and stop < entry else None
    # Trade ids restart after a trading-DB reset; the execution time keeps the atom id unique.
    ref_id = f"{trade.id}:{trade.executed_at.isoformat() if trade.executed_at else ''}"
    kw = dict(source="main_live" if trade.is_live else "main_paper", strategy="main", ref_id=ref_id,
              ticker=trade.ticker, pnl_pct=trade.realized_pnl_pct or 0.0, r_multiple=r_multiple, hold_days=hold_days,
              exit_reason=reason, score=trade.consensus_score, entry_type="UNKNOWN", params={})
    manual, trigger_id = bool(trade.is_manual), pos.trigger_id

    async def write() -> bool:
        if manual:
            return False
        try:
            if trigger_id is not None:
                try:  # score and entry type are labels; never lose the outcome over them
                    async with db_session.AsyncSessionLocal() as session:
                        row = (await session.execute(
                            select(OrderTrigger.consensus_score, OrderTrigger.trigger_type)
                            .where(OrderTrigger.id == trigger_id))).first()
                    if row:
                        kw["score"] = row[0] if row[0] is not None else kw["score"]
                        kw["entry_type"] = row[1] or "UNKNOWN"
                except Exception as e:
                    logger.warning("Outcome trigger lookup failed for %s: %s", kw["ref_id"], e)
            return await record_outcome(**kw)
        except Exception as e:
            logger.warning("Outcome write failed for %s:%s: %s", kw["source"], kw["ref_id"], e)
            return False

    task = asyncio.create_task(write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task
