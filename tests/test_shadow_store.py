from config.settings import settings
from src.db.models import ShadowStrategyRow, ShadowParamChange

def test_tables_and_settings_exist():
    assert ShadowStrategyRow.__tablename__ == "shadow_strategies"
    assert {"key", "params", "learning_strength", "enabled", "copied_from", "is_preset",
            "closed_at_last_learn"} <= set(ShadowStrategyRow.__table__.columns.keys())
    assert ShadowParamChange.__tablename__ == "shadow_param_changes"
    assert settings.SHADOW_LEARN_MIN_TRADES == 10 and settings.SHADOW_MAX_STRATEGIES == 12

import pytest
from sqlalchemy import delete, select

from src.db.models import SystemConfig, ShadowAccount, ShadowPosition, ShadowSnapshot, ShadowTrigger
from src.db.session import AsyncSessionLocal
from src.trading import shadow as shadow_module
from src.trading.shadow import (
    PRESETS, apply_param_change, copy_strategy, create_strategy, delete_strategy, get_strategies,
    learning_strengths, load_strategies, shadow_portfolios, undo_param_change,
)


async def _rows(model):
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(model).order_by(model.id))).scalars().all()


@pytest.fixture(autouse=True)
async def _fresh_store(monkeypatch):
    monkeypatch.setattr(settings, "SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "SHADOW_STRATEGIES", "baseline,aggressive")
    monkeypatch.setattr(settings, "SHADOW_CUSTOM_STRATEGIES", "")
    monkeypatch.setattr(settings, "SHADOW_INITIAL_CAPITAL", 10000.0)
    async with AsyncSessionLocal() as session:
        for model in (ShadowStrategyRow, ShadowPosition, ShadowTrigger, ShadowSnapshot, ShadowAccount):
            await session.execute(delete(model))
        await session.execute(delete(SystemConfig).where(SystemConfig.key == "SHADOW_SEEDED"))
        await session.commit()
    await load_strategies()


async def test_seed_from_env_then_db_is_truth():
    assert [s.key for s in get_strategies()] == ["baseline", "aggressive"]
    settings.SHADOW_STRATEGIES = "momentum"
    await load_strategies()
    assert [s.key for s in get_strategies()] == ["baseline", "aggressive"]
    settings.SHADOW_ENABLED = False
    assert get_strategies() == []


async def test_create_copy_delete():
    key = await create_strategy("My Test", {**PRESETS["baseline"].to_params(), "stop_scale": 1.5})
    assert key == "my_test"
    copy = await copy_strategy(key, "My Copy")
    assert next(s for s in get_strategies() if s.key == copy).stop_scale == 1.5
    assert learning_strengths()[copy] == 0
    assert await delete_strategy(key) is True
    assert await delete_strategy(key) is False
    assert key not in [s.key for s in get_strategies()]
    with pytest.raises(LookupError):
        await copy_strategy("nope", "X")


async def test_create_rejects_duplicate_main_invalid_and_cap():
    with pytest.raises(ValueError):
        await create_strategy("main", PRESETS["baseline"].to_params())
    with pytest.raises(ValueError):
        await create_strategy("Bad", {"stop_scale": 99})
    await create_strategy("Dup", PRESETS["baseline"].to_params())
    with pytest.raises(ValueError):
        await create_strategy("Dup", PRESETS["baseline"].to_params())
    settings.SHADOW_MAX_STRATEGIES = len(get_strategies())
    with pytest.raises(ValueError):
        await create_strategy("Over", PRESETS["baseline"].to_params())


async def test_apply_param_change_writes_history_and_undo_restores():
    changes = await apply_param_change("baseline", {"stop_scale": 1.2}, source="user", reason="try")
    assert changes == {"stop_scale": {"old": 1.0, "new": 1.2}}
    row = (await _rows(ShadowParamChange))[-1]
    assert row.source == "user" and row.reason == "try"
    await undo_param_change(row.id)
    assert next(s for s in get_strategies() if s.key == "baseline").stop_scale == 1.0
    assert await apply_param_change("baseline", {"stop_scale": 1.0}, source="user") == {}
    with pytest.raises(LookupError):
        await apply_param_change("nope", {"stop_scale": 1.0}, source="user")
    with pytest.raises(LookupError):
        await undo_param_change(999999)


async def _open_position(monkeypatch, key, ticker):
    prices = {ticker: 100.0}
    monkeypatch.setattr(shadow_module, "check_market_hours", lambda t: (True, "open"))
    monkeypatch.setattr(shadow_module.market_data_provider, "get_stock_data",
                        lambda t: {"price": prices[t]} if t in prices else None)
    report = {"ticker": ticker, "action": "BUY", "consensus_score": 95, "entry_trigger_price": 95.0,
              "trigger_type": "LIMIT_PULLBACK",
              "risk": {"approved": True, "stop_loss_price": 90.0, "take_profit_price": 110.0}}
    await shadow_portfolios.follow_report(report, {"ticker": ticker, "name": ticker, "price": 100.0, "currency": "EUR"})
    prices[ticker] = 94.0
    assert len(await shadow_portfolios.check_triggers()) >= 1
    async with AsyncSessionLocal() as session:
        return (await session.execute(select(ShadowPosition).where(
            ShadowPosition.strategy == key, ShadowPosition.ticker == ticker))).scalars().one()


async def test_edit_does_not_move_open_position_levels(monkeypatch):
    before = await _open_position(monkeypatch, "baseline", "STO1.AS")
    await apply_param_change("baseline", {"stop_scale": 2.0, "reward_risk": 4.0}, source="user")
    async with AsyncSessionLocal() as session:
        after = await session.get(ShadowPosition, before.id)
    assert after.stop_loss_native == before.stop_loss_native
    assert after.take_profit_native == before.take_profit_native
    assert after.initial_stop_native == before.initial_stop_native


async def test_delete_removes_all_rows_and_exit_check_survives(monkeypatch):
    key = await create_strategy("Gone", PRESETS["baseline"].to_params())
    await _open_position(monkeypatch, key, "STO2.AS")
    await delete_strategy(key)
    await shadow_portfolios.check_exits()
    for model, col in ((ShadowAccount, ShadowAccount.strategy), (ShadowPosition, ShadowPosition.strategy),
                       (ShadowTrigger, ShadowTrigger.strategy), (ShadowSnapshot, ShadowSnapshot.strategy)):
        async with AsyncSessionLocal() as session:
            assert (await session.execute(select(model).where(col == key))).scalars().all() == []


async def test_delete_all_stays_empty_after_reload():
    for s in list(get_strategies()):
        assert await delete_strategy(s.key) is True
    assert get_strategies() == []
    await load_strategies()
    assert get_strategies() == []


async def test_edit_survives_unknown_stored_param_key():
    async with AsyncSessionLocal() as session:
        row = await session.get(ShadowStrategyRow, "baseline")
        row.params = {**row.params, "retired_param": 7}
        await session.commit()
    changes = await apply_param_change("baseline", {"stop_scale": 1.3}, source="user")
    assert changes == {"stop_scale": {"old": 1.0, "new": 1.3}}
    assert (await _rows(ShadowParamChange))[-1].changes == changes
    async with AsyncSessionLocal() as session:
        saved = (await session.get(ShadowStrategyRow, "baseline")).params
    assert "retired_param" not in saved and saved["stop_scale"] == 1.3


async def test_job_waiting_on_lock_does_not_resurrect_deleted_strategy():
    import asyncio
    key = await create_strategy("Race", PRESETS["baseline"].to_params())
    report = {"ticker": "RACE.AS", "action": "BUY", "consensus_score": 95, "entry_trigger_price": 95.0,
              "trigger_type": "LIMIT_PULLBACK",
              "risk": {"approved": True, "stop_loss_price": 90.0, "take_profit_price": 110.0}}
    await shadow_portfolios._lock.acquire()
    delete_task = asyncio.create_task(delete_strategy(key))
    await asyncio.sleep(0.05)  # delete now waits first in the lock queue
    follow_task = asyncio.create_task(shadow_portfolios.follow_report(
        report, {"ticker": "RACE.AS", "name": "Race", "price": 100.0, "currency": "EUR"}))
    await asyncio.sleep(0.05)
    shadow_portfolios._lock.release()
    await asyncio.gather(delete_task, follow_task)
    for model in (ShadowAccount, ShadowTrigger):
        async with AsyncSessionLocal() as session:
            assert (await session.execute(select(model).where(model.strategy == key))).scalars().all() == []
