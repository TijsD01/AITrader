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
