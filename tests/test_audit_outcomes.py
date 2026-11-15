"""Audit fixes for outcome memory and the track record: reset-safe ids, shadow cleanup, R/score/entry type,
manual trades left out, paper and live kept apart."""
import datetime
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import delete

from config.settings import settings
from src.db import session as db_session
from src.db.models import OrderTrigger, Position, Trade
from src.db.session import init_db
from src.memory import outcomes
from src.memory.cold_start import ColdStartEngine
from src.memory.outcomes import delete_shadow_outcomes, record_main_outcome, record_outcome
from src.memory.store import memory_store
from src.trading.trade_stats import track_record_block

T0 = datetime.datetime(2026, 1, 5, 10, 0, 0)


@pytest_asyncio.fixture(autouse=True)
async def clean_outcomes():
    async def wipe():
        await memory_store.init_db()
        async with memory_store._get_conn() as db:
            await db.execute("DELETE FROM memory_l1_atoms WHERE agent_id = 'outcomes'")
            await db.commit()
    await wipe()
    yield
    await wipe()


@pytest.fixture
def live_mode():
    orig = (settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED)
    settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED = "LIVE", True
    yield
    settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED = orig


def _pos(**kw):
    base = dict(opened_at=T0, entry_price_native=100.0, initial_stop_native=90.0, trigger_id=None)
    return SimpleNamespace(**{**base, **kw})


def _trade(**kw):
    base = dict(id=1, ticker="AUD.AS", is_live=False, is_manual=False, realized_pnl_pct=5.0, consensus_score=None,
                price_native=105.0, executed_at=T0 + datetime.timedelta(days=2))
    return SimpleNamespace(**{**base, **kw})


# ---- 1. trade ids restart after a trading DB reset ----
async def test_main_outcome_survives_trade_id_reuse():
    assert await record_main_outcome(_pos(), _trade(), "Take-Profit hit") is True
    # Same row again is still a duplicate
    assert await record_main_outcome(_pos(), _trade(), "Take-Profit hit") is False
    later = _trade(executed_at=T0 + datetime.timedelta(days=30), ticker="NEW.AS")
    assert await record_main_outcome(_pos(), later, "Stop-Loss hit") is True
    assert {o["ticker"] for o in await memory_store.search_outcomes(source="main")} == {"AUD.AS", "NEW.AS"}


# ---- 2. shadow reset/delete cleanup ----
async def test_delete_shadow_outcomes_removes_one_strategy_only():
    kw = dict(pnl_pct=1.0, r_multiple=None, hold_days=1.0, exit_reason="x", score=None, entry_type="MARKET", params={})
    await record_outcome(source="shadow", strategy="aggr", ref_id="a1", ticker="A", **kw)
    await record_outcome(source="shadow", strategy="aggr", ref_id="a2", ticker="A", **kw)
    await record_outcome(source="shadow", strategy="aggr2", ref_id="b1", ticker="B", **kw)
    await record_outcome(source="main_paper", strategy="aggr", ref_id="m1", ticker="M", **kw)
    assert await delete_shadow_outcomes("aggr") == 2
    assert await memory_store.search_outcomes(strategy="aggr", source="shadow") == []
    assert len(await memory_store.search_outcomes(strategy="aggr2")) == 1
    assert len(await memory_store.search_outcomes(source="main")) == 1


async def test_delete_shadow_outcomes_never_raises():
    with patch.object(memory_store, "init_db", AsyncMock(side_effect=RuntimeError("memory down"))):
        assert await delete_shadow_outcomes("aggr") == 0


# ---- 3. R multiple, score and entry type on main atoms ----
async def test_main_outcome_carries_r_score_and_entry_type():
    await init_db()
    async with db_session.AsyncSessionLocal() as s:
        trig = OrderTrigger(ticker="AUD.AS", trigger_type="BREAKOUT_BUY", target_price=100.0,
                            current_price_at_creation=99.0, allocation_eur=100.0, consensus_score=83)
        s.add(trig)
        await s.commit()
        trig_id = trig.id
    try:
        assert await record_main_outcome(_pos(trigger_id=trig_id), _trade(), "Take-Profit hit") is True
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.id == trig_id))
            await s.commit()
    o = (await memory_store.search_outcomes(source="main"))[0]
    # entry 100, exit 105, initial stop 90
    assert o["r_multiple"] == 0.5 and o["score"] == 83 and o["entry_type"] == "BREAKOUT_BUY"


async def test_main_outcome_without_trigger_or_valid_stop():
    await record_main_outcome(_pos(initial_stop_native=None), _trade(id=2, consensus_score=71), "x")
    await record_main_outcome(_pos(initial_stop_native=100.0), _trade(id=3), "x")
    # A trigger row that is gone must not lose the outcome
    await record_main_outcome(_pos(trigger_id=987654321), _trade(id=4), "x")
    rows = await memory_store.search_outcomes(source="main")
    assert len(rows) == 3
    by_id = {int(str(o["ref_id"]).split(":")[0]): o for o in rows}
    assert by_id[2]["r_multiple"] is None and by_id[2]["score"] == 71 and by_id[2]["entry_type"] == "UNKNOWN"
    assert by_id[3]["r_multiple"] is None
    assert by_id[4]["r_multiple"] == 0.5 and by_id[4]["entry_type"] == "UNKNOWN"


# ---- 4. manual trades ----
async def test_manual_trade_writes_no_outcome():
    task = record_main_outcome(_pos(), _trade(is_manual=True), "Manual sell")
    assert task in outcomes._pending or task.done()
    assert await task is False
    assert await memory_store.search_outcomes(source="main") == []


def _n(block: str) -> int:
    return int(re.search(r"All trades: n=(\d+)", block).group(1))


async def _add_sells(ticker, specs):
    async with db_session.AsyncSessionLocal() as s:
        s.add_all([Trade(ticker=ticker, order_type="SELL", shares=1.0, currency="EUR", price_native=105.0,
                         price_eur=105.0, total_eur=105.0, realized_pnl_eur=5.0, realized_pnl_pct=5.0, **kw)
                   for kw in specs])
        await s.commit()


async def test_track_record_skips_manual_and_other_mode(live_mode):
    ticker = f"AUD_{int(time.time() * 1000)}"
    ttype = f"AUDT{int(time.time() * 1000) % 10**12}"
    live_before = _n(await track_record_block())
    settings.LIVE_TRADING_ENABLED = False
    paper_before = _n(await track_record_block())
    await _add_sells(ticker, [dict(is_live=False), dict(is_live=False, is_manual=True),
                              dict(is_live=True), dict(is_live=True)])
    async with db_session.AsyncSessionLocal() as s:
        trig = OrderTrigger(ticker=ticker, trigger_type=ttype, target_price=100.0, current_price_at_creation=105.0,
                            stop_loss=90.0, allocation_eur=100.0, status="TRIGGERED")
        s.add(trig)
        await s.flush()
        s.add(Position(ticker=ticker, currency="EUR", shares=1.0, entry_price_native=100.0,
                       current_price_native=105.0, highest_price_native=105.0, entry_price_eur=100.0,
                       current_price_eur=105.0, cost_basis_eur=100.0, status="CLOSED", trigger_id=trig.id,
                       is_live=True))
        await s.commit()
        trig_id = trig.id
    try:
        paper = await track_record_block()
        assert _n(paper) == paper_before + 1 and ttype not in paper
        settings.LIVE_TRADING_ENABLED = True
        assert settings.TRADING_MODE == "LIVE"
        live = await track_record_block()
        assert _n(live) == live_before + 2 and ttype in live
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(Trade).where(Trade.ticker == ticker))
            await s.execute(delete(Position).where(Position.ticker == ticker))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.id == trig_id))
            await s.commit()


# ---- 5. cold start shows the active mode only ----
async def _loadout() -> str:
    with patch.object(ColdStartEngine, "_fetch_portfolio_status", AsyncMock(return_value={"available": False})):
        return (await ColdStartEngine.generate_cold_start_loadout(query="", char_budget=100000)).full_prompt_context


async def test_loadout_shows_active_mode_outcomes_only(live_mode):
    kw = dict(strategy="main", r_multiple=None, hold_days=2.0, exit_reason="x", score=None, entry_type="MARKET",
              params={})
    await record_outcome(source="main_paper", ref_id=1, ticker="PAPRQ", pnl_pct=2.5, **kw)
    await record_outcome(source="main_live", ref_id=1, ticker="LIVEQ", pnl_pct=-1.5, **kw)
    text = await _loadout()
    assert "LIVEQ" in text and "PAPRQ" not in text
    settings.LIVE_TRADING_ENABLED = False
    text = await _loadout()
    assert "PAPRQ" in text and "LIVEQ" not in text
