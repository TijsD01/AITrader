"""
Deposits and withdrawals on the live account.

The broker only reports balances, so money Tijs moves into the account showed up as portfolio gain.
This module finds those cash flows in the live snapshot history and keeps them in the cash_flows
table, so the gain shown is value minus everything that was put in.

A snapshot pair counts as a deposit (or withdrawal) when cash and total account value both jump the
same way: a deposit raises cash and total alike, while a fill only swaps cash for shares (total about
flat) and a market move changes shares, not cash. That holds for fills the bot never recorded too,
such as a broker stop that closed a position between syncs. Cash the bot's own booked trades moved
between the two snapshots is taken out first, so a deposit next to a buy is still found at its full size.

With Trading 212 the broker's own transaction list is used instead: exact amounts and times, no guessing.
Those flows replace the guessed ones. The guess only runs again while that list cannot be read.
"""
import asyncio
import datetime
import logging
import time
from typing import Dict, Any, List, Optional

from sqlalchemy import select, func, delete

from config.settings import settings
from src.db import session as db_session
from src.db.models import CashFlow, PortfolioSnapshot, SystemConfig, Trade

logger = logging.getLogger(__name__)

SCAN_KEY = "cash_flow_scan_snapshot_id"
# The total must move at least this share of the cash jump, in the same direction, to call it a flow
TOTAL_CONFIRM_RATIO = 0.5

# The T212 transactions endpoint allows 6 requests a minute; one sync can page a few times
BROKER_SYNC_INTERVAL = 60.0
# Broker transaction types that move money in or out. FEE is a cost and stays in the gain.
BROKER_FLOW_SIGN = {"DEPOSIT": 1.0, "WITHDRAW": -1.0, "TRANSFER": 0.0}

_scan_lock = asyncio.Lock()
_broker_synced_at = 0.0  # time.monotonic() of the last broker sync attempt, 0 = never


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


def broker_flow_amount(item: Dict[str, Any]) -> Optional[float]:
    """EUR deposit (positive) or withdrawal (negative) for one T212 transaction, or None when it is no flow."""
    kind = (item.get("type") or "").upper()
    if kind not in BROKER_FLOW_SIGN:
        return None
    try:
        amount = float(item.get("amount") or 0.0)
    except (TypeError, ValueError):
        return None
    # The sign of a withdrawal amount is not documented: trust the type. A transfer carries its own sign.
    sign = BROKER_FLOW_SIGN[kind]
    if sign:
        amount = sign * abs(amount)
    ccy = (item.get("currency") or settings.BASE_CURRENCY).upper()
    if ccy != settings.BASE_CURRENCY.upper():
        from src.data.currency import currency_service
        amount = currency_service.to_eur(amount, ccy)
    amount = round(amount, 2)
    return amount or None


def _parse_utc(value: Any) -> Optional[datetime.datetime]:
    try:
        ts = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return ts


async def sync_broker_cash_flows(client) -> int:
    """
    Records T212 deposits and withdrawals made after the first live snapshot. Earlier ones are already
    inside that snapshot's value, the starting capital. Replaces flows guessed from balances. Returns how many were added.
    """
    async with db_session.AsyncSessionLocal() as session:
        first = (await session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
            .order_by(PortfolioSnapshot.timestamp, PortfolioSnapshot.id).limit(1)
        )).scalars().first()
        known = set((await session.execute(
            select(CashFlow.broker_ref).where(CashFlow.broker_ref.is_not(None))
        )).scalars().all())
        if first is None:
            return 0
        items = await client.history_transactions(known_refs=known, since=first.timestamp)

        found = 0
        for item in items:
            ref = str(item.get("reference") or "")
            ts = _parse_utc(item.get("dateTime"))
            amount = broker_flow_amount(item)
            if not ref or ref in known or ts is None or amount is None or ts <= first.timestamp:
                continue
            session.add(CashFlow(
                timestamp=ts, amount_eur=amount, source="broker", broker_ref=ref[:64],
                note=f"Trading 212 {item.get('type', '').lower()}", is_live=True,
            ))
            known.add(ref)
            found += 1
            logger.info(f"Live {'deposit' if amount > 0 else 'withdrawal'} from Trading 212: €{amount:+.2f} at {ts}")

        # The broker list is the truth now: drop the guesses and skip the snapshots they came from
        await session.execute(delete(CashFlow).where(CashFlow.is_live == True, CashFlow.source == "auto"))
        last = (await session.execute(
            select(func.max(PortfolioSnapshot.id)).where(PortfolioSnapshot.is_live == True)
        )).scalar()
        cfg = await session.get(SystemConfig, SCAN_KEY)
        if last is not None:
            if cfg is None:
                session.add(SystemConfig(key=SCAN_KEY, value=str(last)))
            else:
                cfg.value = str(last)
        await session.commit()
        return found


async def scan_live_cash_flows() -> int:
    """
    Records new live deposits and withdrawals. Trading 212: read from its transaction list.
    Else (or when that list fails): guessed from the live snapshots added since the last scan.
    The first run walks the whole history, so deposits made before this existed are found as well.
    Returns how many flows were recorded.
    """
    global _broker_synced_at
    async with _scan_lock:
        if settings.LIVE_BROKER.lower() == "trading212":
            from src.trading import trading212
            client = trading212.get_client()
            if client is not None:
                if _broker_synced_at and time.monotonic() - _broker_synced_at < BROKER_SYNC_INTERVAL:
                    return 0
                # Set before the call: after a failure (e.g. 429) the next page load must not retry and wait again
                _broker_synced_at = time.monotonic()
                try:
                    return await sync_broker_cash_flows(client)
                except Exception as e:
                    logger.warning(f"Trading 212 transactions unavailable, guessing deposits from balances: {e}")
        return await _scan_snapshots()


async def _scan_snapshots() -> int:
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
    """Removes a flow, e.g. one the scan mistook for a deposit. A guessed one is not detected again; a Trading 212 one is."""
    async with db_session.AsyncSessionLocal() as session:
        flow = await session.get(CashFlow, flow_id)
        if not flow:
            return False
        await session.delete(flow)
        await session.commit()
        return True
