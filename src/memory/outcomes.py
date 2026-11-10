"""Closed-trade outcome atoms: written by trade-close paths, read via memory_store.search_outcomes."""
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
