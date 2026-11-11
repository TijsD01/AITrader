"""Main rule set resolver: defaults, write/revert, bad rows, LIVE max-positions cap."""
import json

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db import session as db_session
from src.db.models import Position, SystemConfig
from src.trading.main_strategy import (
    MainRules,
    effective_max_positions,
    get_main_rules,
    is_default_shape,
    revert_main_rules,
    sync_settings_from_main_rules,
    write_main_rules,
)

_REUSED = ("TRIGGER_MIN_SCORE", "BREAKEVEN_TRIGGER_PCT", "MAX_POSITIONS")
_SETTINGS = ("TRIGGER_MIN_SCORE", "MAX_POSITIONS", "RISK_PER_TRADE_PCT", "TRAILING_STOP_ATR_MULT", "REGIME_FILTER_ENABLED")


async def _clear_rows():
    async with db_session.AsyncSessionLocal() as s:
        await s.execute(delete(SystemConfig).where(SystemConfig.key.like("MAIN\\_%", escape="\\")))
        await s.execute(delete(SystemConfig).where(SystemConfig.key.in_(_REUSED)))
        await s.commit()


@pytest.fixture(autouse=True)
async def _isolated_rules(monkeypatch):
    """Start from no rule rows; afterwards put back the reused rows and settings other tests may rely on."""
    for name in _SETTINGS:
        monkeypatch.setattr(settings, name, getattr(settings, name))  # monkeypatch restores it after the test
    async with db_session.AsyncSessionLocal() as s:
        res = await s.execute(select(SystemConfig).where(SystemConfig.key.in_(_REUSED)))
        saved = {r.key: r.value for r in res.scalars().all()}
    await _clear_rows()
    yield
    await _clear_rows()
    async with db_session.AsyncSessionLocal() as s:
        for k, v in saved.items():
            s.add(SystemConfig(key=k, value=v))
        await s.commit()


async def _set_row(key, value):
    async with db_session.AsyncSessionLocal() as s:
        await s.merge(SystemConfig(key=key, value=value))
        await s.commit()


async def _row(key):
    async with db_session.AsyncSessionLocal() as s:
        row = await s.get(SystemConfig, key)
        return row.value if row else None


async def test_defaults_reproduce_today():
    r = await get_main_rules()
    assert (r.pullback_scale, r.stop_scale, r.entry_mode) == (1.0, 1.0, "trigger")
    assert (r.accept_hold, r.ignore_risk_veto, r.use_take_profit) == (False, False, True)
    assert r.breakeven_r is None and r.trailing_stop_pct is None and r.max_hold_days is None and r.reward_risk is None
    assert r.trigger_min_score == settings.TRIGGER_MIN_SCORE and r.max_positions == settings.MAX_POSITIONS
    assert r.breakeven_pct == 0.025
    assert r.risk_per_trade_pct == settings.RISK_PER_TRADE_PCT
    assert r.trailing_atr_mult == settings.TRAILING_STOP_ATR_MULT
    assert r.regime_filter == settings.REGIME_FILTER_ENABLED
    assert r.version == 0 and r.promoted_from is None and is_default_shape(r)


async def test_existing_rows_are_read():
    await _set_row("TRIGGER_MIN_SCORE", "72")
    await _set_row("BREAKEVEN_TRIGGER_PCT", "0.03")
    await _set_row("MAX_POSITIONS", "4")
    await _set_row("MAIN_REGIME_FILTER", "TRUE")
    r = await get_main_rules()
    assert (r.trigger_min_score, r.breakeven_pct, r.max_positions, r.regime_filter) == (72, 0.03, 4, True)
    assert is_default_shape(r)  # ordinary tunables do not change the shape


async def test_write_then_revert_roundtrip():
    before = settings.MAX_POSITIONS
    v = await write_main_rules({"stop_scale": 1.25, "entry_mode": "market", "max_positions": 3}, promoted_from="aggressive")
    r = await get_main_rules()
    assert v == 1 and r.stop_scale == 1.25 and r.entry_mode == "market" and r.promoted_from == "aggressive"
    assert r.version == 1
    assert settings.MAX_POSITIONS == 3 and not is_default_shape(r)
    prev = json.loads(await _row("MAIN_RULES_PREVIOUS"))
    assert set(prev) == set(MainRules.RULE_FIELDS) and prev["stop_scale"] == 1.0
    assert await revert_main_rules() is True
    r = await get_main_rules()
    assert r.stop_scale == 1.0 and r.entry_mode == "trigger" and r.version == 2 and r.promoted_from is None
    assert r.max_positions == before and settings.MAX_POSITIONS == before
    assert await revert_main_rules() is False


async def test_write_sets_in_memory_settings():
    await write_main_rules(
        {"trigger_min_score": 70, "risk_per_trade_pct": 1.0, "trailing_atr_mult": 3.0, "regime_filter": True,
         "reward_risk": 3.0, "trailing_stop_pct": None},
        promoted_from=None,
    )
    assert settings.TRIGGER_MIN_SCORE == 70 and settings.RISK_PER_TRADE_PCT == 1.0
    assert settings.TRAILING_STOP_ATR_MULT == 3.0 and settings.REGIME_FILTER_ENABLED is True
    r = await get_main_rules()
    assert r.reward_risk == 3.0 and r.trailing_stop_pct is None and r.promoted_from is None


@pytest.mark.parametrize("values", [
    {"stop_scale": 9.0},                # out of schema bounds
    {"no_such_rule": 1},                # unknown key
    {"trigger_min_score": 101},
    {"trigger_min_score": 49},          # a promoted threshold below 50 would arm almost every report
    {"trigger_min_score": True},
    {"max_positions": None},            # rule that may not be empty
    {"stop_scale": 1.5, "entry_mode": "limit"},  # one bad value blocks the whole write
])
async def test_invalid_values_write_nothing(values):
    with pytest.raises(ValueError):
        await write_main_rules(values, promoted_from="x")
    r = await get_main_rules()
    assert r.version == 0 and r.stop_scale == 1.0 and await _row("MAIN_RULES_PREVIOUS") is None


@pytest.mark.parametrize("key,raw,field", [
    ("MAIN_STOP_SCALE", "1e9", "stop_scale"),
    ("MAIN_STOP_SCALE", "0", "stop_scale"),
    ("MAIN_STOP_SCALE", "-1", "stop_scale"),
    ("MAIN_STOP_SCALE", "nan", "stop_scale"),
    ("MAX_POSITIONS", "0", "max_positions"),
    ("MAX_POSITIONS", "-3", "max_positions"),
    ("MAX_POSITIONS", "999", "max_positions"),
    ("MAIN_REWARD_RISK", "-1", "reward_risk"),
    ("MAIN_PULLBACK_SCALE", "-2", "pullback_scale"),
    ("MAIN_TRAILING_STOP_PCT", "5", "trailing_stop_pct"),
    ("MAIN_RISK_PER_TRADE_PCT", "-1", "risk_per_trade_pct"),
    ("TRIGGER_MIN_SCORE", "-5", "trigger_min_score"),
    ("TRIGGER_MIN_SCORE", "150", "trigger_min_score"),
    ("BREAKEVEN_TRIGGER_PCT", "0", "breakeven_pct"),
])
async def test_out_of_range_row_falls_back(key, raw, field):
    default = getattr(await get_main_rules(), field)
    await _set_row(key, raw)
    assert getattr(await get_main_rules(), field) == default


async def test_max_positions_read_allows_settings_route_range():
    await _set_row("MAX_POSITIONS", "50")  # the settings route accepts up to 50
    assert (await get_main_rules()).max_positions == 50


async def test_revert_never_restores_out_of_range_values():
    await write_main_rules({"stop_scale": 1.5}, promoted_from="x")
    prev = json.loads(await _row("MAIN_RULES_PREVIOUS"))
    prev["stop_scale"] = 1e9
    await _set_row("MAIN_RULES_PREVIOUS", json.dumps(prev))
    assert await revert_main_rules() is True
    assert (await get_main_rules()).stop_scale == 1.0 and await _row("MAIN_STOP_SCALE") == "1.0"


async def test_startup_sync_applies_rules_to_settings():
    await write_main_rules({"risk_per_trade_pct": 1.0, "trailing_atr_mult": 3.0, "regime_filter": True}, promoted_from="x")
    settings.RISK_PER_TRADE_PCT, settings.TRAILING_STOP_ATR_MULT, settings.REGIME_FILTER_ENABLED = 0.2, 1.0, False
    await sync_settings_from_main_rules()
    r = await get_main_rules()
    assert (settings.RISK_PER_TRADE_PCT, settings.TRAILING_STOP_ATR_MULT, settings.REGIME_FILTER_ENABLED) == (1.0, 3.0, True)
    assert (r.risk_per_trade_pct, r.trailing_atr_mult, r.regime_filter) == (1.0, 3.0, True)
    assert (settings.TRIGGER_MIN_SCORE, settings.MAX_POSITIONS) == (r.trigger_min_score, r.max_positions)


async def test_startup_sync_with_default_rules_changes_nothing():
    before = {a: getattr(settings, a) for a in _SETTINGS}
    await sync_settings_from_main_rules()
    assert {a: getattr(settings, a) for a in _SETTINGS} == before


async def test_init_db_syncs_main_rules(monkeypatch):
    called = []

    async def fake_sync():
        called.append(True)
    monkeypatch.setattr("src.trading.main_strategy.sync_settings_from_main_rules", fake_sync)
    await db_session.init_db()
    assert called == [True]


async def test_live_write_cannot_raise_max_positions():
    await _set_row("MAX_POSITIONS", "5")
    await write_main_rules({"max_positions": 9}, promoted_from="momentum", live=True)
    assert (await get_main_rules()).max_positions == 5 and settings.MAX_POSITIONS == 5
    await write_main_rules({"max_positions": 3}, promoted_from="momentum", live=True)
    assert (await get_main_rules()).max_positions == 3
    await write_main_rules({"max_positions": 9}, promoted_from="momentum")  # paper: no cap
    assert (await get_main_rules()).max_positions == 9


async def test_garbage_row_falls_back():
    await _set_row("MAIN_STOP_SCALE", "abc")
    await _set_row("MAIN_ACCEPT_HOLD", "maybe")
    await _set_row("MAIN_RULES_VERSION", "x")
    r = await get_main_rules()
    assert r.stop_scale == 1.0 and r.accept_hold is False and r.version == 0


async def test_db_error_returns_defaults(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    with monkeypatch.context() as m:  # undo before the fixture teardown needs the DB again
        m.setattr(db_session, "AsyncSessionLocal", boom)
        r = await get_main_rules()
    assert r.version == 0 and r.max_positions == settings.MAX_POSITIONS and is_default_shape(r)


def test_live_max_positions_never_exceeds_env_cap():
    assert effective_max_positions(9, True, 5) == 5
    assert effective_max_positions(9, False, 5) == 9
    assert effective_max_positions(3, True, 5) == 3


async def test_as_strategy_carries_rules():
    s = (await get_main_rules()).as_strategy()
    assert (s.key, s.label, s.score_offset) == ("main", "Main", 0)
    assert s.stop_scale == 1.0 and s.entry_mode == "trigger" and s.max_positions == settings.MAX_POSITIONS


async def test_rules_version_column_exists():
    async with db_session.AsyncSessionLocal() as s:
        p = Position(ticker="RV.TEST", shares=1, entry_price_native=1, current_price_native=1, highest_price_native=1,
                     entry_price_eur=1, current_price_eur=1, cost_basis_eur=1, status="CLOSED", rules_version=1)
        s.add(p)
        await s.commit()
        pid = p.id
    async with db_session.AsyncSessionLocal() as s:
        assert (await s.get(Position, pid)).rules_version == 1
        await s.execute(delete(Position).where(Position.id == pid))
        await s.commit()
