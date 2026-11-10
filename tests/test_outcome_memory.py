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
