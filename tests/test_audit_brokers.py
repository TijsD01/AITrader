"""Audit fixes on the broker path: live sells reach the learner, manual and adopted trades are flagged and kept
out of it, paper viability uses the fees paper execution charges, the promoted % trail runs on every check,
and Alpaca cash is converted to EUR.
"""
import asyncio
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, desc, select

from config.settings import settings
from src.agents.risk_manager import RiskManagerAgent
from src.data.market import market_data_provider
from src.db.models import OrderTrigger, PortfolioSnapshot, Position, Trade
from src.db.session import AsyncSessionLocal
from src.trading.learner import trade_learner
from src.trading.live import LiveBroker
from src.trading.main_strategy import write_main_rules
from src.trading.paper import PaperBroker
from tests.test_agent_robustness import _stock
from tests.test_main_parity_entry import TICKER, _isolated  # noqa: F401  (autouse: clean ticker, paper cash, default rules)
from tests.test_main_parity_exit import _add_position, _monitor, _pos, broker  # noqa: F401  (paper + live, learner mocked)


async def _sell(b, price=105.0, **kwargs):
    mod = "src.trading.paper" if b.kind == "paper" else "src.trading.live"
    with patch(f"{mod}.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()), \
         patch.object(market_data_provider, "get_stock_data", return_value={"price": price, "technicals": {}}):
        res = await b.execute_sell(TICKER, reason="test close", **kwargs)
    await _drain(b)
    return res


async def _drain(b):
    """Live runs the learner as its own task; wait for it."""
    await asyncio.gather(*list(getattr(b, "_learner_tasks", ())))


async def _trades(order_type):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(Trade).where(Trade.ticker == TICKER, Trade.order_type == order_type))).scalars().all()


async def _set_manual(pid):
    async with AsyncSessionLocal() as s:
        (await s.get(Position, pid)).is_manual = True
        await s.commit()


# --- 1. The learner runs after every close, live as well as paper ---------------------------------

async def test_sell_runs_post_mortem_and_calibration(broker):
    await _add_position(broker)
    assert (await _sell(broker))["success"]
    trade_id = (await _trades("SELL"))[0].id
    trade_learner.conduct_trade_post_mortem.assert_awaited_once_with(trade_id)
    trade_learner.calibrate_internal_parameters.assert_awaited_once_with(trade_id, automatic=True)


async def test_broker_side_close_found_by_the_sync_runs_the_learner(broker):
    if broker.kind != "live":
        pytest.skip("live only")
    await _add_position(broker)
    async with broker._t212_lock:  # the sync books the exit while it holds the lock
        res = await broker.execute_sell(TICKER, reason="Closed at Trading 212", broker_flat=True, exit_price_native=95.0)
    await _drain(broker)
    assert res["success"]
    trade_learner.conduct_trade_post_mortem.assert_awaited_once_with((await _trades("SELL"))[0].id)
    trade_learner.calibrate_internal_parameters.assert_awaited_once()


async def test_live_learner_does_not_hold_the_t212_lock(broker, monkeypatch):
    if broker.kind != "live":
        pytest.skip("live only")
    gate = asyncio.Event()

    async def slow_post_mortem(_trade_id):
        await gate.wait()

    monkeypatch.setattr(trade_learner, "conduct_trade_post_mortem", AsyncMock(side_effect=slow_post_mortem))
    await _add_position(broker)
    with patch("src.trading.live.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()), \
         patch.object(market_data_provider, "get_stock_data", return_value={"price": 105.0, "technicals": {}}):
        res = await broker.execute_sell(TICKER, reason="test close")  # returns while the post-mortem still runs
    assert res["success"] and not broker._t212_lock.locked()
    trade_learner.calibrate_internal_parameters.assert_not_awaited()
    gate.set()
    await _drain(broker)
    trade_learner.calibrate_internal_parameters.assert_awaited_once()


async def test_learner_failure_never_fails_the_sell(broker, monkeypatch):
    monkeypatch.setattr(trade_learner, "conduct_trade_post_mortem", AsyncMock(side_effect=RuntimeError("llm down")))
    pid = await _add_position(broker)
    assert (await _sell(broker))["success"]
    assert (await _pos(pid)).status == "CLOSED"


# --- 2. Manual and adopted trades are flagged and stay out of the learner --------------------------

@pytest.mark.parametrize("is_manual", [True, False])
async def test_buy_flags_position_and_trade(broker, monkeypatch, is_manual):
    if broker.kind == "live":
        monkeypatch.setattr(broker, "_plan_live_buy", AsyncMock(return_value={"success": True, "shares": 1.0}))
        monkeypatch.setattr(broker, "_dispatch_real_order", AsyncMock(return_value=("t212_1", 1.0, 100.0)))
    mod = "src.trading.paper" if broker.kind == "paper" else "src.trading.live"
    with patch(f"{mod}.check_market_hours", return_value=(True, "Market is OPEN")):
        res = await broker.execute_buy(TICKER, "Main Parity", 100.0, "EUR", 95.0, 115.0, 500.0, 85, "test buy", is_manual=is_manual)
    assert res["success"], res
    async with AsyncSessionLocal() as s:
        pos = (await s.execute(select(Position).where(Position.ticker == TICKER))).scalars().one()
    assert bool(pos.is_manual) is is_manual
    assert bool((await _trades("BUY"))[0].is_manual) is is_manual


async def test_manual_position_sell_is_flagged_and_skips_the_learner(broker):
    await _set_manual(await _add_position(broker))
    assert (await _sell(broker))["success"]
    assert (await _trades("SELL"))[0].is_manual is True
    trade_learner.conduct_trade_post_mortem.assert_not_awaited()
    trade_learner.calibrate_internal_parameters.assert_not_awaited()


async def test_bot_position_sell_is_not_flagged(broker):
    await _add_position(broker)
    assert (await _sell(broker))["success"]
    assert not (await _trades("SELL"))[0].is_manual


async def test_adopted_holding_is_manual(broker):
    if broker.kind != "live":
        pytest.skip("live only")
    async with AsyncSessionLocal() as s:
        await broker._adopt_holding(s, TICKER, "Main Parity", "EUR", 1.0, 100.0, 0.0, [], [], levels=(95.0, 115.0))
        await s.commit()
        pos = (await s.execute(select(Position).where(Position.ticker == TICKER))).scalars().one()
    assert pos.is_manual is True


# --- 3. Viability checks use the fees paper execution charges --------------------------------------

async def test_paper_trigger_viability_uses_the_live_broker_fees(monkeypatch):
    from src.trading import fees
    seen = []
    real = fees.evaluate_trade_economic_viability
    monkeypatch.setattr(fees, "evaluate_trade_economic_viability", lambda **kw: seen.append(kw["broker"]) or real(**kw))
    try:
        await PaperBroker().create_price_trigger(
            TICKER, "Main Parity", "EUR", "LIMIT_PULLBACK", 95.0, 100.0, 90.0, 110.0, 500.0, 85, "test")
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == TICKER))
            await s.commit()
    assert seen == [settings.LIVE_BROKER]


async def test_risk_manager_viability_uses_the_live_broker_fees_in_paper_mode(monkeypatch):
    from src.trading import fees
    seen = []
    real = fees.evaluate_trade_economic_viability
    monkeypatch.setattr(fees, "evaluate_trade_economic_viability", lambda **kw: seen.append(kw["broker"]) or real(**kw))
    assert settings.TRADING_MODE == "PAPER"
    await RiskManagerAgent.evaluate_risk(_stock(), {"bias": "NEUTRAL"}, {"health": "STRONG"}, 10000.0)
    assert seen == [settings.LIVE_BROKER]


# --- 4. The promoted % trail runs on every regular-session check, like the shadow ------------------

async def test_pct_trail_applies_without_a_new_high(broker):
    v = await write_main_rules({"trailing_stop_pct": 0.02}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, stop=95.0, initial_stop=95.0)
    await _monitor(broker, 99.0)  # the price only falls: no new high
    assert (await _pos(pid)).stop_loss_native == 98.0
    await _monitor(broker, 98.5)
    pos = await _pos(pid)
    assert pos.stop_loss_native == 98.0 and pos.status == "OPEN"  # never moves down
    if broker.kind == "live":
        broker._t212_move_stop.assert_awaited_once_with(TICKER, "EUR", 1.0, 98.0)


async def test_pct_trail_waits_for_the_regular_session(broker):
    v = await write_main_rules({"trailing_stop_pct": 0.02}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, stop=95.0, initial_stop=95.0)
    await _monitor(broker, 99.0, market_open=False)
    assert (await _pos(pid)).stop_loss_native == 95.0


async def test_pct_trail_without_a_new_high_skips_unshaped_positions(broker):
    await write_main_rules({"trailing_stop_pct": 0.02}, promoted_from="test")
    pid = await _add_position(broker, stop=95.0, initial_stop=95.0)
    await _monitor(broker, 99.0)
    assert (await _pos(pid)).stop_loss_native == 95.0


# --- 5. Alpaca reports USD: the snapshot is in EUR -------------------------------------------------

async def test_alpaca_sync_converts_cash_to_eur(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_BROKER", "alpaca")
    monkeypatch.setattr(settings, "ALPACA_API_KEY", "k")
    monkeypatch.setattr(settings, "ALPACA_SECRET_KEY", "s")
    fake = types.ModuleType("alpaca.trading.client")
    fake.TradingClient = lambda **kw: types.SimpleNamespace(
        get_account=lambda: types.SimpleNamespace(cash="1000", portfolio_value="1500"))
    monkeypatch.setitem(sys.modules, "alpaca.trading.client", fake)
    b = LiveBroker()
    monkeypatch.setattr(b, "refresh_positions_pnl", AsyncMock())
    monkeypatch.setattr(b, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0}))
    async with AsyncSessionLocal() as s:
        before = (await s.execute(select(PortfolioSnapshot.id).order_by(desc(PortfolioSnapshot.id)))).scalars().first() or 0
    try:
        with patch("src.trading.live.currency_service.to_eur", side_effect=lambda v, c: v * 0.9 if c == "USD" else v):
            await b.sync_account_state()
        async with AsyncSessionLocal() as s:
            snap = (await s.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.id > before))).scalars().one()
        assert (snap.cash_eur, snap.equity_eur, snap.total_value_eur) == pytest.approx((900.0, 450.0, 1350.0))
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id > before))
            await s.commit()
