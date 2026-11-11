import pytest_asyncio

from src.memory.outcomes import record_outcome
from src.memory.store import memory_store


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


async def test_record_then_search_by_strategy_and_source():
    assert await record_outcome(source="shadow", strategy="aggressive", ref_id=1, ticker="AAA", pnl_pct=4.2,
        r_multiple=1.4, hold_days=3.0, exit_reason="Take-Profit hit", score=84, entry_type="LIMIT_PULLBACK",
        params={"stop_scale": 1.25})
    await record_outcome(source="main_paper", strategy="main", ref_id=7, ticker="BBB", pnl_pct=-2.0,
        r_multiple=-1.0, hold_days=1.0, exit_reason="Stop-Loss hit", score=81, entry_type="BREAKOUT_BUY", params={})
    got = await memory_store.search_outcomes(strategy="aggressive")
    assert len(got) == 1 and got[0]["ticker"] == "AAA" and got[0]["params"] == {"stop_scale": 1.25}
    assert [o["ticker"] for o in await memory_store.search_outcomes(source="main")] == ["BBB"]
    assert await memory_store.search_outcomes(exit_reason="Stop-Loss") == await memory_store.search_outcomes(source="main")


async def test_record_is_idempotent():
    kw = dict(source="shadow", strategy="s", ref_id=5, ticker="CCC", pnl_pct=1.0, r_multiple=None, hold_days=1.0,
              exit_reason="x", score=None, entry_type="MARKET", params={})
    assert await record_outcome(**kw) is True
    assert await record_outcome(**kw) is False
    assert len(await memory_store.search_outcomes(strategy="s")) == 1


async def test_record_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(memory_store, "add_l1_atom", boom)
    assert await record_outcome(source="shadow", strategy="s", ref_id=9, ticker="DDD", pnl_pct=0.0, r_multiple=None,
        hold_days=0.0, exit_reason="x", score=None, entry_type="MARKET", params={}) is False


async def test_search_limit_since_ticker_and_bad_tail():
    for i, t in enumerate(["T1", "T2", "T3"]):
        await record_outcome(source="shadow", strategy="lim", ref_id=100 + i, ticker=t, pnl_pct=1.0, r_multiple=None,
            hold_days=1.0, exit_reason="Stop-Loss" if i else "Take-Profit", score=None, entry_type="MARKET", params={})
    assert len(await memory_store.search_outcomes(strategy="lim", limit=2)) == 2
    # limit applies to matching rows, not raw rows
    assert len(await memory_store.search_outcomes(strategy="lim", exit_reason="Take-Profit", limit=1)) == 1
    assert [o["ticker"] for o in await memory_store.search_outcomes(ticker="T2")] == ["T2"]
    assert await memory_store.search_outcomes(since=1e12) == []
    async with memory_store._get_conn() as db:
        await db.execute("UPDATE memory_l1_atoms SET content = 'no json ||| {bad' WHERE atom_id = 'outcome:shadow:100'")
        await db.commit()
    assert len(await memory_store.search_outcomes(strategy="lim")) == 2


# ---- shadow closes write outcomes ----
from tests.test_shadow import _data, _report, shadows  # noqa: E402,F401  (shadows is a fixture)
from src.trading.shadow import shadow_portfolios  # noqa: E402


async def _open_baseline_then_stop_out(prices):
    prices["OUT.AS"] = 100.0
    await shadow_portfolios.follow_report(_report("OUT.AS"), _data("OUT.AS"))
    prices["OUT.AS"] = 94.0
    assert len(await shadow_portfolios.check_triggers()) == 1
    prices["OUT.AS"] = 89.0
    return await shadow_portfolios.check_exits()


async def test_shadow_stop_loss_close_records_outcome(shadows):
    enable, reset_all = shadows
    prices = enable(["baseline"])
    await reset_all()
    closed = await _open_baseline_then_stop_out(prices)
    assert len(closed) == 1 and closed[0]["ticker"] == "OUT.AS" and closed[0]["reason"].startswith("Stop-Loss hit")
    rows = await memory_store.search_outcomes(strategy="baseline")
    assert len(rows) == 1
    o = rows[0]
    assert o["source"] == "shadow" and o["ticker"] == "OUT.AS" and o["exit_reason"].startswith("Stop-Loss hit")
    assert o["r_multiple"] is not None and abs(o["r_multiple"] + 1.0) < 0.3 and o["pnl_pct"] < 0
    assert o["entry_type"] == "LIMIT_PULLBACK" and o["score"] == 85 and isinstance(o["params"], dict)


async def test_shadow_close_survives_record_failure(shadows, monkeypatch):
    enable, reset_all = shadows
    prices = enable(["baseline"])
    await reset_all()

    async def boom(**kw):
        raise RuntimeError("memory down")
    monkeypatch.setattr("src.trading.shadow.record_outcome", boom)
    closed = await _open_baseline_then_stop_out(prices)
    assert len(closed) == 1 and closed[0]["realized_pnl_eur"] < 0


async def test_shadow_outcome_kept_when_position_id_is_reused_after_reset(shadows):
    enable, reset_all = shadows
    prices = enable(["baseline"])
    await reset_all()
    first = await _open_baseline_then_stop_out(prices)
    await reset_all()  # deletes the shadow's positions, so SQLite hands out the same id again
    second = await _open_baseline_then_stop_out(prices)
    assert first[0]["position_id"] == second[0]["position_id"]
    assert len(await memory_store.search_outcomes(strategy="baseline")) == 2


# ---- main closes write outcomes ----
import datetime  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from sqlalchemy import delete, desc, select  # noqa: E402

from config.settings import settings  # noqa: E402
from src.db.session import AsyncSessionLocal, init_db  # noqa: E402
from src.db.models import PortfolioSnapshot, Position, Trade  # noqa: E402
from src.trading.learner import trade_learner  # noqa: E402
from src.trading.live import live_broker  # noqa: E402
from src.trading.paper import paper_broker  # noqa: E402


@contextmanager
def _quiet_sell(module, price):
    """No market hours, fixed quote, no learner/LLM."""
    orig = settings.ENFORCE_MARKET_HOURS
    settings.ENFORCE_MARKET_HOURS = False
    try:
        with patch(f"src.trading.{module}.market_data_provider.get_stock_data", return_value={"price": price}), \
             patch.object(trade_learner, "conduct_trade_post_mortem", AsyncMock(return_value=None)), \
             patch.object(trade_learner, "calibrate_internal_parameters", AsyncMock(return_value=None)):
            yield
    finally:
        settings.ENFORCE_MARKET_HOURS = orig


async def _open_position(ticker, is_live, days_ago=4.0):
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Position).where(Position.ticker == ticker))
        session.add(Position(
            ticker=ticker, name=ticker, currency="EUR", shares=10.0, entry_price_native=100.0,
            current_price_native=100.0, stop_loss_native=95.0, take_profit_native=110.0, highest_price_native=100.0,
            entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=1000.0, fees_eur=0.0, status="OPEN",
            is_live=is_live,
            opened_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(days=days_ago)))
        await session.commit()


async def _paper_cash():
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False)
                                      .order_by(desc(PortfolioSnapshot.timestamp)))).scalars().first()
        return snap.cash_eur if snap else settings.INITIAL_PAPER_CAPITAL


async def test_paper_sell_records_main_paper_outcome():
    await _open_position("OUTP.AS", is_live=False)
    with _quiet_sell("paper", 105.0):
        res = await paper_broker.execute_sell("OUTP.AS", reason="Take-Profit hit")
    assert res["success"] is True
    await _drain()
    rows = await memory_store.search_outcomes(source="main")
    assert len(rows) == 1
    o = rows[0]
    assert o["source"] == "main_paper" and o["strategy"] == "main" and o["ticker"] == "OUTP.AS"
    assert o["pnl_pct"] == res["realized_pnl_pct"] and o["exit_reason"] == "Take-Profit hit"
    assert 3.9 < o["hold_days"] < 4.1 and o["entry_type"] == "UNKNOWN" and o["params"] == {}
    async with AsyncSessionLocal() as session:
        trade = (await session.execute(select(Trade).where(Trade.ticker == "OUTP.AS", Trade.order_type == "SELL")
                                       .order_by(desc(Trade.id)))).scalars().first()
    assert o["ref_id"] == trade.id


async def test_paper_sell_survives_record_failure():
    async def boom(**kw):
        raise RuntimeError("memory down")

    deltas = []
    for ticker, failing in (("OUTOK.AS", False), ("OUTERR.AS", True)):
        await _open_position(ticker, is_live=False)
        before = await _paper_cash()
        with _quiet_sell("paper", 105.0), patch("src.memory.outcomes.record_outcome", boom) if failing else _noop():
            res = await paper_broker.execute_sell(ticker, reason="Take-Profit hit")
            await _drain()
        assert res["success"] is True
        deltas.append(round(await _paper_cash() - before, 2))
        async with AsyncSessionLocal() as session:
            pos = (await session.execute(select(Position).where(Position.ticker == ticker))).scalars().first()
        assert pos.status == "CLOSED"
    assert deltas[0] == deltas[1]
    assert [o["ticker"] for o in await memory_store.search_outcomes(source="main")] == ["OUTOK.AS"]


@contextmanager
def _noop():
    yield


async def _drain():
    """Waits for scheduled outcome writes."""
    import asyncio
    from src.memory import outcomes
    await asyncio.gather(*list(outcomes._pending))


async def test_live_sell_records_main_live_outcome():
    orig = (settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER)
    settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER = "LIVE", True, "trading212"
    try:
        await _open_position("OUTL.AS", is_live=True, days_ago=2.0)
        with _quiet_sell("live", 95.0), patch.object(live_broker, "_dispatch_real_close", return_value="t212_exit_77"):
            res = await live_broker.execute_sell("OUTL.AS", reason="Stop-Loss hit")
        assert res["success"] is True
        await _drain()
        rows = await memory_store.search_outcomes(source="main_live")
        assert len(rows) == 1 and rows[0]["ticker"] == "OUTL.AS" and rows[0]["pnl_pct"] == res["realized_pnl_pct"]
        assert rows[0]["pnl_pct"] < 0 and 1.9 < rows[0]["hold_days"] < 2.1
    finally:
        settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER = orig
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == "OUTL.AS"))
            await session.commit()


async def _sell_with_blocked_write(broker, module, ticker, is_live):
    import asyncio
    gate = asyncio.Event()

    async def blocked(**kw):
        await gate.wait()
        return True

    await _open_position(ticker, is_live=is_live)
    try:
        with _quiet_sell(module, 105.0), patch("src.memory.outcomes.record_outcome", blocked), \
             patch.object(live_broker, "_dispatch_real_close", return_value="t212_exit_88"):
            res = await asyncio.wait_for(broker.execute_sell(ticker, reason="Take-Profit hit"), timeout=10)
            assert res["success"] is True
            async with AsyncSessionLocal() as session:
                pos = (await session.execute(select(Position).where(Position.ticker == ticker))).scalars().first()
            assert pos.status == "CLOSED"
            gate.set()
            await _drain()
    finally:
        gate.set()
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.commit()


async def test_paper_sell_does_not_wait_for_outcome_write():
    await _sell_with_blocked_write(paper_broker, "paper", "OUTBP.AS", is_live=False)


async def test_live_sell_does_not_wait_for_outcome_write():
    orig = (settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER)
    settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER = "LIVE", True, "trading212"
    try:
        await _sell_with_blocked_write(live_broker, "live", "OUTBL.AS", is_live=True)
    finally:
        settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.LIVE_BROKER = orig


# ---- cold start sees main outcomes only ----
from src.memory.cold_start import ColdStartEngine  # noqa: E402
from src.memory.models import L1Atom  # noqa: E402


async def test_loadout_shows_main_outcomes_not_shadow():
    kw = dict(r_multiple=None, hold_days=2.0, exit_reason="Stop-Loss hit", score=None, entry_type="MARKET", params={})
    await record_outcome(source="shadow", strategy="aggr", ref_id=501, ticker="SHDWQ", pnl_pct=-3.0, **kw)
    await record_outcome(source="main_paper", strategy="main", ref_id=502, ticker="MAINQ", pnl_pct=2.5, **kw)
    await memory_store.add_l1_atom(L1Atom(atom_id="test_shadow_param", agent_id="learner", subject="SHDWP",
        predicate="param_change", object="shadow:aggr", content="SHDWP shadow param change"))
    try:
        with patch.object(ColdStartEngine, "_fetch_portfolio_status", AsyncMock(return_value={"available": False})):
            overall = (await ColdStartEngine.generate_cold_start_loadout(query="", char_budget=100000)).full_prompt_context
            by_shadow = (await ColdStartEngine.generate_cold_start_loadout(query="SHDWQ", char_budget=100000)).full_prompt_context
            by_main = (await ColdStartEngine.generate_cold_start_loadout(query="MAINQ", char_budget=100000)).full_prompt_context
    finally:
        async with memory_store._get_conn() as db:
            await db.execute("DELETE FROM memory_l1_atoms WHERE atom_id = 'test_shadow_param'")
            await db.commit()
    assert "MAINQ" in overall and "MAINQ" in by_main
    for text in (overall, by_shadow, by_main):
        assert "SHDWQ +" not in text and "shadow:" not in text and "SHDWP" not in text


# ---- outcome compaction ----
async def test_prune_compacts_old_outcomes_by_month():
    base = dict(source="shadow", strategy="cmp", r_multiple=1.0, hold_days=1.0, score=None, entry_type="MARKET", params={})
    for i in range(505):
        await record_outcome(ref_id=i, ticker="CMPX", pnl_pct=1.0 if i % 2 else -1.0,
                             exit_reason="Take-Profit hit" if i % 2 else "Stop-Loss hit", **base)
    jan, feb, mar = (datetime.datetime(2026, m, 10, tzinfo=datetime.timezone.utc).timestamp() for m in (1, 2, 3))
    async with memory_store._get_conn() as db:
        await db.executemany("UPDATE memory_l1_atoms SET created_at = ? WHERE atom_id = ?",
            [((jan if i < 3 else feb if i < 5 else mar) + i, f"outcome:shadow:{i}") for i in range(505)])
        await db.commit()
    await memory_store.add_l1_atom(L1Atom(atom_id="test_keep_me", subject="KEEPME", predicate="note",
                                          object="x", content="non-outcome atom"))
    try:
        stats = await memory_store.prune_and_compact()
        assert stats["outcomes_compacted"] == 5 and {"l1_deleted", "l2_deleted", "l0_deleted"} <= set(stats)
        assert len(await memory_store.search_outcomes(strategy="cmp", limit=1000)) == 500
        async with memory_store._get_conn() as db:
            async with await db.execute("SELECT atom_id, content FROM memory_l1_atoms WHERE predicate = 'outcome_summary' "
                                        "AND object = 'shadow:cmp' ORDER BY atom_id") as cur:
                summaries = [tuple(r) for r in await cur.fetchall()]
            async with await db.execute("SELECT COUNT(*) FROM memory_l1_atoms WHERE atom_id = 'test_keep_me'") as cur:
                assert (await cur.fetchone())[0] == 1
        assert len(summaries) == 2 and "count 3" in summaries[0][1] and "count 2" in summaries[1][1]
        again = await memory_store.prune_and_compact()
        assert again["outcomes_compacted"] == 0
        async with memory_store._get_conn() as db:
            async with await db.execute("SELECT atom_id, content FROM memory_l1_atoms WHERE predicate = 'outcome_summary' "
                                        "AND object = 'shadow:cmp' ORDER BY atom_id") as cur:
                assert [tuple(r) for r in await cur.fetchall()] == summaries
        assert len(await memory_store.search_outcomes(strategy="cmp", limit=1000)) == 500
    finally:
        async with memory_store._get_conn() as db:
            await db.execute("DELETE FROM memory_l1_atoms WHERE atom_id = 'test_keep_me'")
            await db.commit()
