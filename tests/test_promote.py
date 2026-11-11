"""Manual promote of a shadow's rules to the main bot: preview, confirm, LIVE typed text, revert, resume."""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import ShadowParamChange, SystemConfig
from src.trading import promote as promote_module
from src.trading.learner import _main_learner_paused
from src.trading.main_strategy import get_main_rules, write_main_rules
from src.trading.shadow import apply_param_change, create_strategy
from tests.test_main_strategy import _isolated_rules, _row, _set_row  # noqa: F401  (autouse: no MAIN_* rows, settings restored)
from tests.test_shadow_store import _fresh_store  # noqa: F401  (autouse: clean shadow tables, baseline + aggressive)


@pytest.fixture
async def client():
    from src.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _no_memory_writes():
    """Keep promote atoms out of the shared test memory DB."""
    with patch("src.memory.store.memory_store.add_l1_atom", AsyncMock()) as add:
        yield add


@pytest.fixture(autouse=True)
async def _drop_new_history():
    """Shadow history rows outlive the shadow store fixture; drop the ones this test wrote."""
    async with db_session.AsyncSessionLocal() as s:
        last = (await s.execute(select(func.max(ShadowParamChange.id)))).scalar() or 0
    yield
    async with db_session.AsyncSessionLocal() as s:
        await s.execute(delete(ShadowParamChange).where(ShadowParamChange.id > last))
        await s.commit()


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)


def _param(preview, name):
    return next(p for p in preview["params"] if p["name"] == name)


async def _history(key):
    async with db_session.AsyncSessionLocal() as s:
        return (await s.execute(select(ShadowParamChange).where(
            ShadowParamChange.strategy == key, ShadowParamChange.source == "promote"))).scalars().all()


async def test_preview_resolves_offset_against_current_main(client):
    await _set_row("TRIGGER_MIN_SCORE", "82")
    await create_strategy("Score 76", {"score_offset": -6})
    preview = (await client.get("/api/shadow/score_76/promote/preview")).json()
    row = _param(preview, "trigger_min_score")
    assert (row["current"], row["after"], row["changed"]) == (82, 76, True)
    assert [p["name"] for p in preview["params"] if p["changed"]] == ["trigger_min_score"]
    assert preview["blocked"] is False and preview["live"] is False


async def test_preview_none_keeps_main_value(client):
    preview = (await client.get("/api/shadow/baseline/promote/preview")).json()
    assert len(preview["params"]) == 16
    assert not any(p["changed"] for p in preview["params"])
    assert preview["has_changes"] is False and preview["risky_flags"] == []
    assert _param(preview, "max_positions")["after"] == settings.MAX_POSITIONS


async def test_promote_requires_confirm(client):
    for body in ({"confirm": False}, {}, {"confirm": "true"}):
        r = await client.post("/api/shadow/aggressive/promote", json=body)
        assert r.status_code == 400
    rules = await get_main_rules()
    assert rules.version == 0 and rules.promoted_from is None
    assert await _row("MAIN_LEARNER_PAUSED") is None


async def test_promote_unknown_key_is_404(client):
    assert (await client.get("/api/shadow/nope/promote/preview")).status_code == 404
    assert (await client.post("/api/shadow/nope/promote", json={"confirm": True})).status_code == 404


async def test_promote_writes_rules_pauses_learner_logs_history(client):
    r = await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    assert r.status_code == 200, r.text
    rules = await get_main_rules()
    assert rules.promoted_from == "aggressive" and rules.version == 1
    assert (rules.trigger_min_score, rules.pullback_scale, rules.stop_scale, rules.reward_risk) == (70, 0.5, 1.25, 3.0)
    assert (rules.trailing_stop_pct, rules.max_positions, rules.risk_per_trade_pct) == (0.06, 5, 1.0)
    assert await _main_learner_paused() is True
    [hist] = await _history("aggressive")
    assert hist.changes["trigger_min_score"] == {"old": 80, "new": 70}
    assert "pullback_scale" in hist.changes and "entry_mode" not in hist.changes
    assert hist.reason == "Promoted to main bot (rules v1)"
    status = (await client.get("/api/main/rules")).json()
    assert status["promoted_from"] == "aggressive" and status["promoted_label"] == "Aggressive"
    assert status["learner_paused"] is True and status["can_revert"] is True and status["live"] is False
    assert status["rules"]["max_positions"] == 5 and len(status["rules"]) == 16 and status["version"] == 1


async def test_revert_restores_and_resumes(client):
    before = (await get_main_rules()).rules()
    await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    assert (await client.post("/api/main/rules/revert")).json() == {"success": True}
    rules = await get_main_rules()
    assert rules.rules() == before and rules.promoted_from is None
    assert await _main_learner_paused() is False
    assert settings.TRIGGER_MIN_SCORE == before["trigger_min_score"]
    assert (await client.get("/api/main/rules")).json()["can_revert"] is False
    r = await client.post("/api/main/rules/revert")
    assert r.status_code == 400 and "revert" in r.json()["detail"].lower()


async def test_resume_keeps_promoted_rules(client):
    await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    assert (await client.post("/api/main/learner/resume")).json() == {"success": True}
    assert await _main_learner_paused() is False
    rules = await get_main_rules()
    assert rules.promoted_from == "aggressive" and rules.trigger_min_score == 70


async def test_live_risky_promote_needs_typed_text(client, live):
    await create_strategy("Veto Off", {"ignore_risk_veto": True})
    preview = (await client.get("/api/shadow/veto_off/promote/preview")).json()
    assert preview["live"] is True and preview["risky_flags"] == ["ignore_risk_veto"] and preview["needs_confirm_text"] is True
    for text in ("", "promote live", " PROMOTE LIVE"):
        r = await client.post("/api/shadow/veto_off/promote", json={"confirm": True, "confirm_text": text})
        assert r.status_code == 400
    assert (await get_main_rules()).version == 0
    r = await client.post("/api/shadow/veto_off/promote", json={"confirm": True, "confirm_text": "PROMOTE LIVE"})
    assert r.status_code == 200, r.text
    assert (await get_main_rules()).ignore_risk_veto is True


async def test_live_non_risky_promote_needs_no_text(client, live):
    await create_strategy("Wide Stop", {"stop_scale": 1.5})
    preview = (await client.get("/api/shadow/wide_stop/promote/preview")).json()
    assert preview["risky_flags"] == [] and preview["needs_confirm_text"] is False
    assert (await client.post("/api/shadow/wide_stop/promote", json={"confirm": True})).status_code == 200
    assert (await get_main_rules()).stop_scale == 1.5


async def test_paper_risky_promote_needs_no_text(client):
    await create_strategy("Holds Too", {"accept_hold": True, "entry_mode": "market"})
    preview = (await client.get("/api/shadow/holds_too/promote/preview")).json()
    assert preview["risky_flags"] == ["accept_hold", "entry_mode"] and preview["needs_confirm_text"] is False
    assert (await client.post("/api/shadow/holds_too/promote", json={"confirm": True})).status_code == 200
    rules = await get_main_rules()
    assert rules.accept_hold is True and rules.entry_mode == "market"


async def test_later_shadow_edits_do_not_reach_main(client):
    await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    await apply_param_change("aggressive", {"stop_scale": 1.8, "score_offset": -15}, source="user")
    rules = await get_main_rules()
    assert rules.stop_scale == 1.25 and rules.trigger_min_score == 70


async def test_telegram_failure_does_not_fail_promote(client):
    with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock(side_effect=RuntimeError("down"))) as send:
        r = await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    assert r.status_code == 200 and send.await_count == 1
    assert (await get_main_rules()).promoted_from == "aggressive"


async def test_memory_failure_does_not_fail_promote(client):
    with patch("src.memory.store.memory_store.add_l1_atom", AsyncMock(side_effect=RuntimeError("db"))):
        r = await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    assert r.status_code == 200


async def test_blocked_threshold_is_reported_and_refused(client):
    await _set_row("TRIGGER_MIN_SCORE", "55")
    await create_strategy("Low Bar", {"score_offset": -10})
    preview = (await client.get("/api/shadow/low_bar/promote/preview")).json()
    assert preview["blocked"] is True and "trigger_min_score" in preview["blocked_reason"]
    assert _param(preview, "trigger_min_score")["after"] == 45
    r = await client.post("/api/shadow/low_bar/promote", json={"confirm": True})
    assert r.status_code == 400 and r.json()["detail"] == preview["blocked_reason"]
    assert (await get_main_rules()).version == 0 and await _row("MAIN_LEARNER_PAUSED") is None


async def test_none_valued_exit_params_are_written_as_none(client):
    await write_main_rules({"reward_risk": 2.0, "breakeven_r": 1.0}, promoted_from=None)
    await create_strategy("Mom Like", {"entry_mode": "market", "use_take_profit": False,
                                       "trailing_stop_pct": 0.08, "max_hold_days": 20})
    preview = (await client.get("/api/shadow/mom_like/promote/preview")).json()
    assert _param(preview, "reward_risk") == {"name": "reward_risk", "label": "Reward / risk",
                                               "current": 2.0, "after": None, "changed": True}
    assert (await client.post("/api/shadow/mom_like/promote", json={"confirm": True})).status_code == 200
    rules = await get_main_rules()
    assert rules.reward_risk is None and rules.breakeven_r is None and rules.use_take_profit is False
    assert (rules.trailing_stop_pct, rules.max_hold_days) == (0.08, 20)


async def test_live_preview_shows_capped_max_positions(client, live):
    await create_strategy("More Slots", {"max_positions": 12})
    preview = (await client.get("/api/shadow/more_slots/promote/preview")).json()
    row = _param(preview, "max_positions")
    assert (row["current"], row["after"], row["changed"]) == (8, 8, False)
    assert (await client.post("/api/shadow/more_slots/promote", json={"confirm": True})).status_code == 200
    assert (await get_main_rules()).max_positions == 8


async def test_paper_preview_raises_max_positions(client):
    await create_strategy("More Slots", {"max_positions": 12})
    row = _param((await client.get("/api/shadow/more_slots/promote/preview")).json(), "max_positions")
    assert (row["after"], row["changed"]) == (12, True)


@pytest.mark.parametrize("paused_before", [None, "true"])
async def test_history_failure_rolls_rules_and_pause_back(monkeypatch, paused_before):
    if paused_before:
        await _set_row("MAIN_LEARNER_PAUSED", paused_before)
    before = (await get_main_rules()).rules()

    async def broken(*a, **kw):
        raise RuntimeError("db locked")
    monkeypatch.setattr(promote_module, "_record_history", broken)
    with pytest.raises(RuntimeError):
        await promote_module.promote("aggressive", confirm=True)
    rules = await get_main_rules()
    assert rules.rules() == before and rules.promoted_from is None
    assert await _row("MAIN_LEARNER_PAUSED") == paused_before


@pytest.mark.parametrize("paused_before", [None, "true"])
async def test_write_failure_leaves_pause_as_it_was(paused_before):
    if paused_before:
        await _set_row("MAIN_LEARNER_PAUSED", paused_before)
    await _set_row("TRIGGER_MIN_SCORE", "55")
    await create_strategy("Low Bar", {"score_offset": -10})
    with pytest.raises(ValueError):
        await promote_module.promote("low_bar", confirm=True)
    assert await _row("MAIN_LEARNER_PAUSED") == paused_before and (await get_main_rules()).version == 0


async def test_pause_is_set_before_rules_are_written(monkeypatch):
    seen = []
    real_write = promote_module.write_main_rules

    async def spy(*a, **kw):
        seen.append(await _row("MAIN_LEARNER_PAUSED"))
        return await real_write(*a, **kw)
    monkeypatch.setattr(promote_module, "write_main_rules", spy)
    await promote_module.promote("aggressive", confirm=True)
    assert seen == ["true"] and await _row("MAIN_LEARNER_PAUSED") == "true"


async def test_promote_with_main_max_positions_above_shadow_bound(client):
    await _set_row("MAX_POSITIONS", "30")
    assert (await client.get("/api/shadow/baseline/promote/preview")).json()["blocked"] is False
    r = await client.post("/api/shadow/baseline/promote", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert (await get_main_rules()).max_positions == 30


async def _main_regime_on():
    await write_main_rules({"regime_filter": True}, promoted_from=None)


@pytest.mark.parametrize("params,flag,setup", [
    ({"risk_per_trade_pct": 1.0}, "risk_per_trade_pct", None),   # main 0.5 -> 1.0
    ({"risk_per_trade_pct": 0.0}, "risk_per_trade_pct", None),   # 0 turns the risk cap off
    ({"regime_filter": False}, "regime_filter", _main_regime_on),
    ({"score_offset": -5}, "trigger_min_score", None),
])
async def test_live_new_risky_flags_need_typed_text(client, live, params, flag, setup):
    if setup:
        await setup()
    await create_strategy("Risky One", params)
    preview = (await client.get("/api/shadow/risky_one/promote/preview")).json()
    assert preview["risky_flags"] == [flag] and preview["needs_confirm_text"] is True
    assert (await client.post("/api/shadow/risky_one/promote", json={"confirm": True})).status_code == 400
    r = await client.post("/api/shadow/risky_one/promote", json={"confirm": True, "confirm_text": "PROMOTE LIVE"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("params", [
    {"score_offset": 5},            # stricter threshold
    {"score_offset": -4},           # drop under 5 points
    {"risk_per_trade_pct": 0.2},    # less risk
    {"regime_filter": True},        # main False -> True
])
async def test_live_safer_changes_are_not_risky(client, live, params):
    await create_strategy("Safe One", params)
    preview = (await client.get("/api/shadow/safe_one/promote/preview")).json()
    assert preview["risky_flags"] == [] and preview["has_changes"] is True
    assert (await client.post("/api/shadow/safe_one/promote", json={"confirm": True})).status_code == 200


async def test_paper_new_risky_flags_need_no_text(client):
    await _main_regime_on()
    await create_strategy("Risky All", {"risk_per_trade_pct": 0.0, "regime_filter": False, "score_offset": -10})
    preview = (await client.get("/api/shadow/risky_all/promote/preview")).json()
    assert preview["risky_flags"] == ["risk_per_trade_pct", "regime_filter", "trigger_min_score"]
    assert preview["needs_confirm_text"] is False
    assert (await client.post("/api/shadow/risky_all/promote", json={"confirm": True})).status_code == 200


async def test_second_promote_then_revert_restores_first(client):
    await create_strategy("Wide Stop", {"stop_scale": 1.5})
    await client.post("/api/shadow/aggressive/promote", json={"confirm": True})
    first = (await get_main_rules()).rules()
    await client.post("/api/shadow/wide_stop/promote", json={"confirm": True})
    assert (await get_main_rules()).stop_scale == 1.5
    assert (await client.post("/api/main/rules/revert")).status_code == 200
    rules = await get_main_rules()
    assert rules.rules() == first and rules.promoted_from == "aggressive"
    status = (await client.get("/api/main/rules")).json()
    assert status["learner_paused"] is True and status["can_revert"] is False  # one level of undo
    # (a single promote then revert -> promoted_from None and pause cleared: test_revert_restores_and_resumes)


async def test_promote_equal_rules_still_records_and_pauses(client):
    preview = (await client.get("/api/shadow/baseline/promote/preview")).json()
    assert preview["has_changes"] is False
    r = await client.post("/api/shadow/baseline/promote", json={"confirm": True})
    assert r.status_code == 200
    assert (await get_main_rules()).promoted_from == "baseline" and await _main_learner_paused() is True
