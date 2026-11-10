import asyncio
import datetime
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from src.db.models import ShadowParamChange, ShadowPosition, ShadowStrategyRow
from src.db.session import AsyncSessionLocal
from src.memory.store import memory_store
from src.trading.shadow import PRESETS, create_strategy, get_strategies, update_strategy
from src.trading.shadow_learner import compute_stats, shadow_learner
from tests.test_shadow_store import _fresh_store  # noqa: F401  (autouse: resets the shadow store per test)

KEY = "learn_me"


@pytest.fixture(autouse=True)
async def _learner(_fresh_store, monkeypatch):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ShadowParamChange))
        await session.commit()
    await create_strategy("Learn Me", PRESETS["baseline"].to_params())
    await update_strategy(KEY, learning_strength=2)
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(shadow_learner.llm, "generate_json", mock)
    return mock


def _pos(pnl_pct, i=0, key=KEY):
    t = datetime.datetime(2026, 1, 1) + datetime.timedelta(days=i)
    return ShadowPosition(
        strategy=key, ticker=f"T{i}.AS", shares=1, entry_price_native=100.0, current_price_native=100.0,
        highest_price_native=100.0, initial_stop_native=95.0, entry_price_eur=100.0, current_price_eur=100.0,
        cost_basis_eur=100.0, fees_eur=1.0, status="CLOSED", exit_price_native=100.0 + pnl_pct,
        realized_pnl_pct=pnl_pct, realized_pnl_eur=pnl_pct, exit_reason="Stop loss" if pnl_pct < 0 else "Take profit",
        opened_at=t, closed_at=t + datetime.timedelta(days=2),
    )


async def _add_closed(n, start=0):
    async with AsyncSessionLocal() as session:
        session.add_all([_pos(2.0 if i % 2 else -1.0, start + i) for i in range(n)])
        await session.commit()


async def _row():
    async with AsyncSessionLocal() as session:
        return await session.get(ShadowStrategyRow, KEY)


async def _history():
    async with AsyncSessionLocal() as session:
        return (await session.execute(
            select(ShadowParamChange).where(ShadowParamChange.strategy == KEY))).scalars().all()


def _params():
    return next(s for s in get_strategies() if s.key == KEY).to_params()


async def test_good_proposal_is_clamped_and_saved(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {"stop_scale": 2.0}, "reason": "stops too tight"}
    res = await shadow_learner.learn(KEY)
    # medium strength = 10% of the 1.5 range = 0.15
    assert res["success"] is True and res["changes"] == {"stop_scale": {"old": 1.0, "new": 1.15}}
    assert _params()["stop_scale"] == 1.15
    [row] = await _history()
    assert row.source == "learner" and row.reason == "stops too tight" and row.stats["since_last_learn"]["trades"] == 10
    assert (await _row()).closed_at_last_learn == 10
    system, user = _learner.await_args.args
    assert "SIMULATED" in system and "JSON" in system
    assert "stop_scale" in user and "[0.5, 2.0]" in user and "[0.005, 0.1]" in user  # breakeven_pct learn range
    for frozen in ("accept_hold", "ignore_risk_veto", "entry_mode"):
        assert frozen not in user.split("Tunable parameters")[1].split("\n\n")[0]


async def test_memory_atom_written(_learner, monkeypatch):
    await _add_closed(10)
    added = []
    monkeypatch.setattr(memory_store, "add_l1_atom", AsyncMock(side_effect=lambda a: added.append(a) or 1))
    _learner.return_value = {"changes": {"stop_scale": 2.0}, "reason": "stops too tight"}
    await shadow_learner.learn(KEY)
    [atom] = added
    assert (atom.agent_id, atom.predicate, atom.object, atom.subject) == ("outcomes", "param_change", f"shadow:{KEY}", KEY)
    payload = json.loads(atom.content.rsplit(" ||| ", 1)[1])
    assert payload["changes"] == {"stop_scale": {"old": 1.0, "new": 1.15}} and payload["reason"] == "stops too tight"


async def test_memory_failure_keeps_change(_learner, monkeypatch):
    await _add_closed(10)
    monkeypatch.setattr(memory_store, "add_l1_atom", AsyncMock(side_effect=RuntimeError("db down")))
    _learner.return_value = {"changes": {"stop_scale": 2.0}}
    res = await shadow_learner.learn(KEY)
    assert res["success"] is True and _params()["stop_scale"] == 1.15
    assert (await _row()).closed_at_last_learn == 10


async def test_non_learnable_and_unknown_params_dropped(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {"ignore_risk_veto": True, "accept_hold": True,
                                         "entry_mode": "market", "bogus": 1}}
    res = await shadow_learner.learn(KEY)
    assert res["success"] is True and res["changes"] == {}
    assert await _history() == []
    assert _params()["ignore_risk_veto"] is False and _params()["entry_mode"] == "trigger"
    assert (await _row()).closed_at_last_learn == 10  # valid reply: do not re-ask on every exit


async def test_at_most_three_params(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {"stop_scale": 2.0, "pullback_scale": 1.5, "score_offset": 10,
                                         "max_hold_days": 100, "trailing_stop_pct": 0.2}}
    # baseline has max_hold_days / trailing_stop_pct = None, so give them values a learner can move
    await shadow_learner_set(max_hold_days=20, trailing_stop_pct=0.05)
    res = await shadow_learner.learn(KEY)
    assert set(res["changes"]) == {"stop_scale", "pullback_scale", "score_offset"}
    p = _params()
    assert p["max_hold_days"] == 20 and p["trailing_stop_pct"] == 0.05


async def test_junk_first_three_cannot_sneak_in_a_fourth(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {"bogus": 1, "accept_hold": True, "entry_mode": "market", "stop_scale": 2.0}}
    res = await shadow_learner.learn(KEY)
    assert res["success"] is True and res["changes"] == {} and _params()["stop_scale"] == 1.0
    assert (await _row()).closed_at_last_learn == 10


async def shadow_learner_set(**params):
    from src.trading.shadow import apply_param_change
    await apply_param_change(KEY, params, source="user")
    async with AsyncSessionLocal() as session:
        await session.execute(delete(ShadowParamChange))
        await session.commit()


async def test_below_trade_gate_skips_without_llm_call(_learner):
    await _add_closed(9)
    res = await shadow_learner.learn(KEY)
    assert res["skipped"] is True and res["success"] is False
    _learner.assert_not_awaited()


async def test_gate_counts_only_trades_since_last_learn(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {}}
    assert (await shadow_learner.learn(KEY))["success"] is True
    await _add_closed(9, start=10)
    assert (await shadow_learner.learn(KEY))["skipped"] is True
    assert _learner.await_count == 1
    await _add_closed(1, start=19)
    assert (await shadow_learner.learn(KEY))["success"] is True
    assert (await _row()).closed_at_last_learn == 20


async def test_manual_needs_five_trades(_learner):
    await _add_closed(4)
    res = await shadow_learner.learn(KEY, manual=True)
    assert res["skipped"] is True
    _learner.assert_not_awaited()
    await _add_closed(1, start=4)
    _learner.return_value = {"changes": {}}
    res = await shadow_learner.learn(KEY, manual=True)
    assert res["success"] is True and res["skipped"] is False
    _learner.assert_awaited_once()


async def test_strength_zero_skips(_learner):
    await _add_closed(20)
    await update_strategy(KEY, learning_strength=0)
    for manual in (False, True):
        res = await shadow_learner.learn(KEY, manual=manual)
        assert res["skipped"] is True and res["success"] is False
    _learner.assert_not_awaited()


async def test_unknown_key_skips(_learner):
    res = await shadow_learner.learn("nope")
    assert res == {"success": False, "skipped": True, "message": res["message"], "changes": {}}
    _learner.assert_not_awaited()


async def test_llm_none_or_exception_changes_nothing_and_retries_later(_learner):
    await _add_closed(10)
    _learner.return_value = None
    res = await shadow_learner.learn(KEY)
    assert res["success"] is False and res["skipped"] is False and res["changes"] == {}
    _learner.side_effect = RuntimeError("cli crashed")
    res = await shadow_learner.learn(KEY)
    assert res["success"] is False and res["changes"] == {}
    assert await _history() == []
    assert (await _row()).closed_at_last_learn == 0
    assert _params()["stop_scale"] == 1.0
    assert _learner.await_count == 2  # not skipped: the gate still lets it retry


@pytest.mark.parametrize("reply", [
    [], "text", {"changes": []}, {"changes": {"stop_scale": "wide"}},
    {"changes": {"stop_scale": float("nan")}}, {"changes": {"stop_scale": None}}, {},
    {"changes": {"stop_scale": 10**400}}, {"changes": {"stop_scale": True}}, {"changes": {"stop_scale": [1.2]}},
    {"changes": {"stop_scale": float("inf")}}, {"changes": {"stop_scale": {"v": 1.2}}}, {"changes": None},
    {"changes": {"stop_scale": 1.2}, "reason": {"evil": 1}},
])
async def test_hostile_replies_change_nothing(_learner, reply):
    await _add_closed(10)
    _learner.return_value = reply
    before = _params()
    res = await shadow_learner.learn(KEY)
    if reply == {"changes": {"stop_scale": 1.2}, "reason": {"evil": 1}}:
        # a valid change with a non-string reason: the change stands, the reason is blanked
        assert res["success"] is True and (await _history())[0].reason == ""
        return
    assert res["changes"] == {} and _params() == before
    assert await _history() == []
    valid = isinstance(reply, dict) and isinstance(reply.get("changes"), dict)
    assert res["success"] is valid
    assert (await _row()).closed_at_last_learn == (10 if valid else 0)


async def test_reason_truncated(_learner):
    await _add_closed(10)
    _learner.return_value = {"changes": {"stop_scale": 1.1}, "reason": "x" * 2000}
    await shadow_learner.learn(KEY)
    assert len((await _history())[0].reason) == 500


def test_compute_stats():
    positions = [_pos(2.0, 0), _pos(4.0, 1), _pos(6.0, 2), _pos(-3.0, 3)]
    s = compute_stats(positions)
    assert s["trades"] == 4 and s["win_rate_pct"] == 75.0
    assert s["avg_win_pct"] == 4.0 and s["avg_loss_pct"] == -3.0
    assert s["expectancy_pct"] == 2.25
    assert s["exit_reasons"] == {"Take profit": 3, "Stop loss": 1}
    assert s["avg_hold_days"] == 2.0 and s["fees_eur"] == 4.0
    # R = (exit - entry) / (entry - initial stop) = pnl / 5
    assert s["avg_r"] == pytest.approx((0.4 + 0.8 + 1.2 - 0.6) / 4)
    assert compute_stats([])["trades"] == 0


async def test_concurrent_learn_runs_once(_learner):
    await _add_closed(10)

    async def slow(*_):
        await asyncio.sleep(0.05)
        return {"changes": {}}

    _learner.side_effect = slow
    results = await asyncio.gather(shadow_learner.learn(KEY), shadow_learner.learn(KEY))
    _learner.assert_awaited_once()
    assert sorted(r["skipped"] for r in results) == [False, True]


async def test_learn_due_runs_each_strength_and_survives_errors(_learner, monkeypatch):
    other = await create_strategy("Other One", PRESETS["baseline"].to_params())
    await update_strategy(other, learning_strength=1)
    seen = []
    real = shadow_learner.learn

    async def fake(key, manual=False):
        seen.append(key)
        if key == KEY:
            raise RuntimeError("boom")
        return await real(key, manual)

    monkeypatch.setattr(shadow_learner, "learn", fake)
    results = await shadow_learner.learn_due()
    assert sorted(seen) == sorted([KEY, other])  # baseline / aggressive have strength 0
    assert len(results) == 1 and results[0]["skipped"] is True
