import datetime
from types import SimpleNamespace

from src.trading.risk import allocate_pending_triggers


def _trig(id, score, minutes_old=0, ticker="AAPL", currency="USD"):
    created = datetime.datetime(2026, 1, 1, 12, 0) - datetime.timedelta(minutes=minutes_old)
    return SimpleNamespace(id=id, ticker=ticker, currency=currency, consensus_score=score, created_at=created)


def test_highest_scores_are_funded_first_and_rest_get_zero():
    """€4,000 over 4 free slots with 6 triggers: the 4 best get €1,000, the 2 weakest €0."""
    triggers = [_trig(1, 70), _trig(2, 95), _trig(3, 80), _trig(4, 85), _trig(5, 60), _trig(6, 90)]
    allocs = allocate_pending_triggers(4000.0, 0, triggers, max_positions=4)
    assert allocs == {2: 1000.0, 6: 1000.0, 4: 1000.0, 3: 1000.0, 1: 0.0, 5: 0.0}


def test_total_reserved_never_exceeds_usable_cash():
    triggers = [_trig(i, 60 + i) for i in range(1, 10)]
    allocs = allocate_pending_triggers(1234.56, 1, triggers, max_positions=5)
    assert sum(allocs.values()) <= 1234.56


def test_equal_scores_fund_the_oldest_trigger_first():
    triggers = [_trig(1, 80, minutes_old=5), _trig(2, 80, minutes_old=60)]
    allocs = allocate_pending_triggers(1000.0, 0, triggers, max_positions=1)
    assert allocs == {2: 1000.0, 1: 0.0}


def test_open_positions_reduce_the_number_of_funded_triggers():
    """4 slots with 2 already held leaves 2 slots of €2,000; the third trigger is unfunded."""
    triggers = [_trig(1, 90), _trig(2, 85), _trig(3, 80)]
    allocs = allocate_pending_triggers(4000.0, 2, triggers, max_positions=4)
    assert allocs == {1: 2000.0, 2: 2000.0, 3: 0.0}


def test_no_free_slots_leaves_every_trigger_unfunded():
    triggers = [_trig(1, 90), _trig(2, 85)]
    allocs = allocate_pending_triggers(4000.0, 4, triggers, max_positions=4)
    assert allocs == {1: 0.0, 2: 0.0}


def test_missing_score_ranks_below_scored_triggers():
    triggers = [_trig(1, None), _trig(2, 75)]
    allocs = allocate_pending_triggers(1000.0, 0, triggers, max_positions=1)
    assert allocs == {2: 1000.0, 1: 0.0}


import pytest
from unittest.mock import patch, AsyncMock
from sqlalchemy import select, delete

from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import OrderTrigger, Position, PortfolioSnapshot
from src.trading.paper import paper_broker

STRONG, WEAK = "RANK_STRONG_TEST", "RANK_WEAK_TEST"


async def _arm_two_triggers_for_one_free_slot():
    """€4,000 cash, exactly one free slot, a score-99 and a score-98 trigger armed at €100 each."""
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_([STRONG, WEAK])))
        open_count = len((await session.execute(
            select(Position).where(Position.status == "OPEN", Position.is_live == False)
        )).scalars().all())
        session.add(PortfolioSnapshot(
            cash_eur=4000.0, equity_eur=0.0, total_value_eur=4000.0, open_positions_count=open_count, is_live=False,
            timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ))
        await session.commit()
    settings.MAX_POSITIONS = open_count + 1
    settings.FULL_BUDGET_UTILIZATION = True
    ids = {}
    for ticker, score in ((WEAK, 98), (STRONG, 99)):
        res = await paper_broker.create_price_trigger(
            ticker=ticker, name=ticker, currency="EUR", trigger_type="LIMIT_PULLBACK",
            target_price=90.0, current_price_at_creation=100.0, stop_loss=85.0, take_profit=110.0,
            allocation_eur=100.0, consensus_score=score, ai_rationale="Testing score ranking"
        )
        assert res["success"] is True
        ids[ticker] = res["trigger_id"]
    return ids


async def _cleanup(orig_max, orig_full):
    settings.MAX_POSITIONS, settings.FULL_BUDGET_UTILIZATION = orig_max, orig_full
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_([STRONG, WEAK])))
        await session.commit()


@pytest.mark.asyncio
async def test_paper_adjust_funds_the_best_trigger_and_zeroes_the_weaker_one():
    orig_max, orig_full = settings.MAX_POSITIONS, settings.FULL_BUDGET_UTILIZATION
    try:
        ids = await _arm_two_triggers_for_one_free_slot()
        await paper_broker.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(OrderTrigger).where(OrderTrigger.id.in_(ids.values())))).scalars().all()
            allocs = {t.ticker: t.allocation_eur for t in rows}
        assert allocs == {STRONG: 4000.0, WEAK: 0.0}
    finally:
        await _cleanup(orig_max, orig_full)


@pytest.mark.asyncio
async def test_unfunded_trigger_does_not_attempt_a_buy_when_its_price_is_hit():
    orig_max, orig_full = settings.MAX_POSITIONS, settings.FULL_BUDGET_UTILIZATION
    try:
        await _arm_two_triggers_for_one_free_slot()
        hit = {"ticker": WEAK, "price": 89.0, "currency": "EUR", "name": WEAK}
        buy = AsyncMock(return_value={"success": False, "message": "should not be reached"})
        with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
             patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=lambda sym: hit if sym == WEAK else None), \
             patch.object(paper_broker, "execute_buy", buy):
            await paper_broker.check_and_execute_triggers()
        buy.assert_not_awaited()
    finally:
        await _cleanup(orig_max, orig_full)
