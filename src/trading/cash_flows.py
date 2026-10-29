"""
Deposits and withdrawals on the live account.

The broker only reports balances, so money Tijs moves into the account showed up as portfolio gain.
This module finds those cash flows in the live snapshot history and keeps them in the cash_flows
table, so the gain shown is value minus everything that was put in.

A snapshot pair counts as a deposit (or withdrawal) when cash and total account value both jump the
same way: a deposit raises cash and total alike, while a fill only swaps cash for shares (total about
flat) and a market move changes shares, not cash. That holds for fills the bot never recorded too,
such as an IBKR stop that closed a position between syncs. Cash the bot's own booked trades moved
between the two snapshots is taken out first, so a deposit next to a buy is still found at its full size.
"""
import asyncio
import datetime
import logging
from typing import Dict, Any, List, Optional

from sqlalchemy import select, func

from config.settings import settings
from src.db import session as db_session
from src.db.models import CashFlow, PortfolioSnapshot, SystemConfig, Trade

logger = logging.getLogger(__name__)

SCAN_KEY = "cash_flow_scan_snapshot_id"
# The total must move at least this share of the cash jump, in the same direction, to call it a flow
TOTAL_CONFIRM_RATIO = 0.5

_scan_lock = asyncio.Lock()


def detect_flow(
    prev: PortfolioSnapshot, curr: PortfolioSnapshot, min_eur: Optional[float] = None, traded_cash_eur: float = 0.0
) -> Optional[float]:
    """
    Returns the deposit (positive) or withdrawal (negative) between two snapshots, or None.
    traded_cash_eur: net cash the bot's booked trades took out between the snapshots (buys minus sells).
    """
    min_eur = settings.CASH_FLOW_MIN_EUR if min_eur is None else min_eur
    d_cash = (curr.cash_eur or 0.0) - (prev.cash_eur or 0.0) + traded_cash_eur
    d_total = (curr.total_value_eur or 0.0) - (prev.total_value_eur or 0.0)
    if abs(d_cash) < max(min_eur, 0.01):
        return None
    if d_cash * d_total <= 0 or abs(d_total) < TOTAL_CONFIRM_RATIO * abs(d_cash):
        return None
    return round(d_cash, 2)


async def scan_live_cash_flows() -> int:
    """
    Checks live snapshots added since the last scan for deposits and withdrawals and records them.
    The first run walks the whole history, so deposits made before this existed are found as well.
    Returns how many flows were recorded.
    """
    async with _scan_lock:
        async with db_session.AsyncSessionLocal() as session:
            cfg = await session.get(SystemConfig, SCAN_KEY)
            try:
                last_id = int(cfg.value) if cfg else 0
            except ValueError:
                last_id = 0

            prev = None
            if last_id:
                prev_res = await session.execute(
                    select(PortfolioSnapshot)
                    .where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.id <= last_id)
                    .order_by(PortfolioSnapshot.id.desc()).limit(1)
                )
                prev = prev_res.scalars().first()
            res = await session.execute(
                select(PortfolioSnapshot)
                .where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.id > last_id)
                .order_by(PortfolioSnapshot.id)
            )
            new_snaps = res.scalars().all()
            if not new_snaps:
                return 0

            trade_res = await session.execute(
                select(Trade).where(Trade.is_live == True, Trade.executed_at > (prev or new_snaps[0]).timestamp)
            )
            trades = trade_res.scalars().all()
            found = 0
            for snap in new_snaps:
                if prev is not None:
                    traded = sum(
                        (t.total_eur or 0.0) + (t.fee_eur or 0.0) if t.order_type == "BUY" else -((t.total_eur or 0.0) - (t.fee_eur or 0.0))
                        for t in trades if prev.timestamp < t.executed_at <= snap.timestamp
                    )
                    amount = detect_flow(prev, snap, traded_cash_eur=traded)
                    if amount is not None:
                        session.add(CashFlow(
                            timestamp=snap.timestamp,
                            amount_eur=amount,
                            source="auto",
                            snapshot_id=snap.id,
                            note="Detected from broker cash balance",
                            is_live=True,
                        ))
                        found += 1
                        logger.info(f"Live {'deposit' if amount > 0 else 'withdrawal'} detected: €{amount:+.2f} at {snap.timestamp}")
                prev = snap

            if cfg is None:
                session.add(SystemConfig(key=SCAN_KEY, value=str(new_snaps[-1].id)))
            else:
                cfg.value = str(new_snaps[-1].id)
            await session.commit()
            return found


async def net_cash_flows_eur(is_live: bool = True,
                             since: Optional[datetime.datetime] = None,
                             until: Optional[datetime.datetime] = None) -> float:
    """Sum of deposits minus withdrawals, optionally only those after `since` and up to `until`."""
    async with db_session.AsyncSessionLocal() as session:
        q = select(func.coalesce(func.sum(CashFlow.amount_eur), 0.0)).where(CashFlow.is_live == is_live)
        if since is not None:
            q = q.where(CashFlow.timestamp > since)
        if until is not None:
            q = q.where(CashFlow.timestamp <= until)
        return round(float((await session.execute(q)).scalar() or 0.0), 2)


async def list_cash_flows(is_live: bool = True) -> List[Dict[str, Any]]:
    async with db_session.AsyncSessionLocal() as session:
        res = await session.execute(
            select(CashFlow).where(CashFlow.is_live == is_live).order_by(CashFlow.timestamp.desc())
        )
        return [
            {
                "id": f.id,
                "timestamp": f.timestamp.strftime("%Y-%m-%d %H:%M") if f.timestamp else "",
                "amount_eur": round(f.amount_eur, 2),
                "source": f.source,
                "note": f.note or "",
            }
            for f in res.scalars().all()
        ]


async def list_cash_flow_points(is_live: bool = True) -> List[tuple]:
    """(timestamp, amount) pairs in time order, for adjusting a value series."""
    async with db_session.AsyncSessionLocal() as session:
        res = await session.execute(
            select(CashFlow.timestamp, CashFlow.amount_eur)
            .where(CashFlow.is_live == is_live).order_by(CashFlow.timestamp)
        )
        return [(t, a) for t, a in res.all()]


async def add_cash_flow(amount_eur: float, note: Optional[str] = None,
                        timestamp: Optional[datetime.datetime] = None, is_live: bool = True) -> Dict[str, Any]:
    """Records a deposit or withdrawal by hand (for one the scan missed, or below CASH_FLOW_MIN_EUR)."""
    async with db_session.AsyncSessionLocal() as session:
        flow = CashFlow(
            timestamp=timestamp or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
            amount_eur=round(amount_eur, 2),
            source="manual",
            note=(note or "").strip()[:200] or None,
            is_live=is_live,
        )
        session.add(flow)
        await session.commit()
        return {"id": flow.id, "amount_eur": flow.amount_eur}


async def delete_cash_flow(flow_id: int) -> bool:
    """Removes a flow, e.g. one the scan mistook for a deposit. It is not detected again."""
    async with db_session.AsyncSessionLocal() as session:
        flow = await session.get(CashFlow, flow_id)
        if not flow:
            return False
        await session.delete(flow)
        await session.commit()
        return True
