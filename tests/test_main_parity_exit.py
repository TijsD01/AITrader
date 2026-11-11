"""Task 14: positions opened under a promoted rule set follow its exit rules (breakeven at R, % trailing stop,
maximum hold time) in paper and live alike. Every other position keeps today's stop path exactly.
"""
import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.data.market import market_data_provider
from src.db.models import PortfolioSnapshot, Position, SystemConfig, Trade
from src.db.session import AsyncSessionLocal
from src.trading import live as live_module
from src.trading import trading212
from src.trading.learner import trade_learner
from src.trading.live import LiveBroker
from src.trading.main_strategy import write_main_rules
from src.trading.paper import PaperBroker
from tests.test_main_parity_entry import TICKER, _isolated  # noqa: F401  (autouse: default rules restored after each test)
from tests.test_t212_resting_orders import _positions as _asml_positions, _seed, _sync, asml, rest  # noqa: F401
from tests.test_trading212_broker import clean_snapshots  # noqa: F401  (used by the rest fixture)


@pytest.fixture(params=["paper", "live"])
async def broker(request, monkeypatch):
    """A fresh broker of each kind with every outside call stubbed; live sells and stop moves are recorded."""
    monkeypatch.setattr(settings, "TRAILING_STOP_ATR_MULT", 3.0)
    monkeypatch.setattr(trade_learner, "conduct_trade_post_mortem", AsyncMock(return_value=None))
    monkeypatch.setattr(trade_learner, "calibrate_internal_parameters", AsyncMock(return_value=None))
    started = datetime.datetime.utcnow()
    if request.param == "paper":
        b = PaperBroker()
    else:
        monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
        monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
        monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", False)
        monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
        monkeypatch.setattr(settings, "T212_RESTING_ORDERS", False)
        monkeypatch.setattr(trading212, "get_client", lambda: None)
        b = LiveBroker()
        monkeypatch.setattr(b, "enforce_daily_loss_limit", AsyncMock(return_value=None))
        monkeypatch.setattr(b, "adjust_triggers_dynamically", AsyncMock(return_value=[]))
        monkeypatch.setattr(b, "_t212_move_stop", AsyncMock())
        monkeypatch.setattr(b, "_dispatch_real_close", AsyncMock(return_value="t212_99"))
    b.kind = request.param
    yield b
    async with AsyncSessionLocal() as s:
        await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.timestamp >= started))
        await s.commit()


async def _add_position(b, rules_version=None, stop=92.0, initial_stop=92.0, take_profit=150.0, days_old=1, ticker=TICKER):
    async with AsyncSessionLocal() as s:
        p = Position(
            ticker=ticker, name="Main Parity", currency="EUR", shares=1.0,
            entry_price_native=100.0, current_price_native=100.0, highest_price_native=100.0,
            stop_loss_native=stop, take_profit_native=take_profit, initial_stop_native=initial_stop,
            entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=100.0,
            status="OPEN", is_live=b.kind == "live", rules_version=rules_version,
            opened_at=datetime.datetime.utcnow() - datetime.timedelta(days=days_old),
        )
        s.add(p)
        await s.commit()
        return p.id


async def _pos(pid):
    async with AsyncSessionLocal() as s:
        return await s.get(Position, pid)


async def _monitor(b, price, atr=None, market_open=True):
    """One position-monitor run at `price` for every ticker."""
    b._last_pnl_refresh = 0.0
    hours = (True, "Market is OPEN") if market_open else (False, "Market is CLOSED")
    mod = "src.trading.paper" if b.kind == "paper" else "src.trading.live"
    with patch(f"{mod}.check_market_hours", return_value=hours), \
         patch(f"{mod}.quotes_moving", return_value=True), \
         patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()), \
         patch.object(market_data_provider, "get_stock_data", return_value={"price": price, "technicals": {"atr14": atr}}):
        return await b.check_stops_and_targets()


async def _stop_path(b, pid, prices, atr=2.0):
    stops = []
    for price in prices:
        await _monitor(b, price, atr=atr)
        stops.append((await _pos(pid)).stop_loss_native)
    return stops


# --- Positions not opened under the active rules: today's path ---------------------------------

SNAPSHOT_PRICES = [102.0, 107.0, 120.0, 116.0]
SNAPSHOT_STOPS = [92.0, 101.0, 114.0, 114.0]  # today: breakeven +1% past 2.5%, then high - 3 x ATR(2)


async def test_default_rules_stop_path_snapshot(broker):
    pid = await _add_position(broker)
    assert await _stop_path(broker, pid, SNAPSHOT_PRICES) == SNAPSHOT_STOPS


@pytest.mark.parametrize("version_of", [lambda v: None, lambda v: v - 1, lambda v: v + 5])
async def test_null_or_stale_version_ignores_promoted_exit_rules(broker, version_of):
    v = await write_main_rules({"trailing_stop_pct": 0.02, "breakeven_r": 3.0, "max_hold_days": 1}, promoted_from="test")
    pid = await _add_position(broker, rules_version=version_of(v), days_old=30)
    assert await _stop_path(broker, pid, SNAPSHOT_PRICES) == SNAPSHOT_STOPS
    assert (await _pos(pid)).status == "OPEN"  # no time exit either


# --- Positions opened under the active rules ----------------------------------------------------

async def test_pct_trailing_stop_trails_the_high_and_never_falls(broker):
    v = await write_main_rules({"trailing_stop_pct": 0.10}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v)
    assert await _stop_path(broker, pid, [120.0, 112.0, 110.0], atr=None) == [108.0, 108.0, 108.0]
    assert (await _pos(pid)).status == "OPEN"
    if broker.kind == "live":
        broker._t212_move_stop.assert_awaited_once_with(TICKER, "EUR", 1.0, 108.0)


async def test_breakeven_r_waits_for_one_r(broker):
    v = await write_main_rules({"breakeven_r": 1.0}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, stop=92.0, initial_stop=92.0)
    assert await _stop_path(broker, pid, [107.0], atr=None) == [92.0]
    assert await _stop_path(broker, pid, [108.0], atr=None) == [101.0]


async def test_breakeven_r_without_initial_stop_falls_back_to_the_percentage(broker):
    v = await write_main_rules({"breakeven_r": 1.0}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, initial_stop=None)
    assert await _stop_path(broker, pid, [103.0], atr=None) == [101.0]


async def test_max_hold_sells_with_time_exit_reason(broker):
    v = await write_main_rules({"max_hold_days": 20}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, days_old=21)
    young = await _add_position(broker, rules_version=v, days_old=19, ticker=TICKER + "Y")
    try:
        sold = await _monitor(broker, 100.0)
        assert [s["ticker"] for s in sold] == [TICKER]
        assert sold[0]["reason"] == "Time exit after 20 days"
        assert (await _pos(pid)).status == "CLOSED"
        assert (await _pos(young)).status == "OPEN"
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Position).where(Position.ticker == TICKER + "Y"))
            await s.commit()


async def test_time_exit_waits_for_market_hours_like_a_stop_exit(broker):
    v = await write_main_rules({"max_hold_days": 20}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, days_old=21)
    assert await _monitor(broker, 100.0, market_open=False) == []
    assert (await _pos(pid)).status == "OPEN"
    sold = await _monitor(broker, 100.0)
    assert sold[0]["reason"].startswith("Time exit")


async def test_stop_exit_wins_over_time_exit(broker):
    v = await write_main_rules({"max_hold_days": 20}, promoted_from="test")
    await _add_position(broker, rules_version=v, days_old=21)
    sold = await _monitor(broker, 90.0)
    assert "Stop-Loss" in sold[0]["reason"]


async def test_kill_switch_never_blocks_time_or_stop_exits(broker, monkeypatch):
    monkeypatch.setattr(settings, "KILL_SWITCH_ACTIVE", True)
    v = await write_main_rules({"max_hold_days": 20}, promoted_from="test")
    old = await _add_position(broker, rules_version=v, days_old=21)
    sold = await _monitor(broker, 100.0)
    assert sold[0]["reason"].startswith("Time exit") and (await _pos(old)).status == "CLOSED"
    stopped = await _add_position(broker)
    sold = await _monitor(broker, 90.0)
    assert "Stop-Loss" in sold[0]["reason"] and (await _pos(stopped)).status == "CLOSED"


async def test_position_without_take_profit_is_monitored_and_stops_out(broker):
    v = await write_main_rules({"use_take_profit": False, "trailing_stop_pct": 0.10}, promoted_from="test")
    shaped = await _add_position(broker, rules_version=v, take_profit=None)
    assert await _monitor(broker, 300.0, atr=None) == []
    assert (await _pos(shaped)).stop_loss_native == 270.0
    sold = await _monitor(broker, 260.0)
    assert "Stop-Loss" in sold[0]["reason"] and (await _pos(shaped)).status == "CLOSED"
    plain = await _add_position(broker, take_profit=None)
    assert await _monitor(broker, 100.0) == []
    sold = await _monitor(broker, 90.0)
    assert "Stop-Loss" in sold[0]["reason"] and (await _pos(plain)).status == "CLOSED"


async def test_live_failed_time_exit_keeps_position_open_and_retries(broker):
    if broker.kind != "live":
        pytest.skip("broker order failures exist on live only")
    v = await write_main_rules({"max_hold_days": 20}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, days_old=21)
    broker._dispatch_real_close.return_value = None
    assert await _monitor(broker, 100.0) == []
    assert (await _pos(pid)).status == "OPEN"
    broker._dispatch_real_close.return_value = "t212_100"
    sold = await _monitor(broker, 100.0)
    assert sold[0]["reason"].startswith("Time exit") and (await _pos(pid)).status == "CLOSED"


async def test_live_broker_stop_update_failure_behaves_like_todays_ratchet(broker):
    """Today's ratchet logs a failed broker stop move and keeps the raised stop in the DB, where the
    bot's own monitor still enforces it. A promoted-rule stop follows the same rule, and nothing is sold."""
    if broker.kind != "live":
        pytest.skip("broker stop orders exist on live only")
    broker._t212_move_stop.side_effect = RuntimeError("T212 down")
    v = await write_main_rules({"trailing_stop_pct": 0.10}, promoted_from="test")
    shaped = await _add_position(broker, rules_version=v)
    control = await _add_position(broker, ticker=TICKER + "C")
    try:
        assert await _monitor(broker, 120.0, atr=None) == []
        assert (await _pos(shaped)).stop_loss_native == 108.0
        assert (await _pos(control)).stop_loss_native == 101.0
        assert broker._t212_move_stop.await_count == 2
        assert (await _pos(shaped)).status == (await _pos(control)).status == "OPEN"
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Position).where(Position.ticker == TICKER + "C"))
            await s.commit()


# --- initial_stop_native and rules_version are booked on every buy path ------------------------

async def test_paper_buy_books_the_starting_stop():
    b = PaperBroker()
    with patch("src.trading.paper.check_market_hours", return_value=(True, "")):
        res = await b.execute_buy(ticker=TICKER, name="Main Parity", price_native=100.0, currency="EUR",
                                  stop_loss_native=93.0, take_profit_native=120.0, allocation_eur=500.0,
                                  consensus_score=85, rationale="t", is_manual=True, rules_version=4)
    assert res["success"]
    async with AsyncSessionLocal() as s:
        (pos,) = (await s.execute(select(Position).where(Position.ticker == TICKER))).scalars().all()
    assert (pos.initial_stop_native, pos.rules_version) == (93.0, 4)


async def test_live_resting_fill_books_rules_version_and_starting_stop(rest, asml):
    tid = await _seed(order_id="t212_1", rules_version=7)
    rest.history = [{"order": {"id": 1, "status": "FILLED"}, "fill": {"quantity": 0.5, "price": 598.0}}]
    await _sync()
    (pos,) = await _asml_positions()
    assert (pos.trigger_id, pos.rules_version, pos.initial_stop_native) == (tid, 7, 570.0)


async def test_live_trigger_monitor_fill_books_rules_version_and_starting_stop(rest, asml, monkeypatch):
    monkeypatch.setattr(settings, "T212_RESTING_ORDERS", False)
    monkeypatch.setattr(live_module, "check_market_hours", lambda t, now=None: (True, ""))
    monkeypatch.setattr(live_module, "check_market_regime", lambda t, c: (True, ""))
    monkeypatch.setattr(LiveBroker, "_plan_live_buy", AsyncMock(return_value={"success": True, "shares": 0.5}))
    monkeypatch.setattr(LiveBroker, "_dispatch_real_order", AsyncMock(return_value=("t212_7", 0.5, 599.0)))
    monkeypatch.setattr(LiveBroker, "adjust_triggers_dynamically", AsyncMock(return_value=[]))
    tid = await _seed(rules_version=7)
    with patch.object(live_module.market_data_provider, "get_stock_data", return_value={"price": 599.0}):
        executed = await LiveBroker().check_and_execute_triggers()
    assert len(executed) == 1
    (pos,) = await _asml_positions()
    assert (pos.trigger_id, pos.rules_version, pos.initial_stop_native) == (tid, 7, 570.0)
    async with AsyncSessionLocal() as s:
        await s.execute(delete(Trade).where(Trade.ticker == "ASML.AS"))
        await s.commit()


# --- Fix round 1: a raised stop at or above the price never goes to the broker ---------------------

def _live_only(b):
    if b.kind != "live":
        pytest.skip("broker stop orders exist on live only")


async def test_live_breakeven_stop_above_price_stays_off_the_broker_and_sells(broker):
    _live_only(broker)
    v = await write_main_rules({"breakeven_r": 0.5}, promoted_from="test")
    # Initial stop 98.5: trigger 0.5 x 1.5% = 0.75%, below the 1% breakeven step
    pid = await _add_position(broker, rules_version=v, stop=98.5, initial_stop=98.5)
    sold = await _monitor(broker, 100.8, atr=None)
    pos = await _pos(pid)
    assert pos.stop_loss_native == 101.0
    broker._t212_move_stop.assert_not_awaited()
    assert "Stop-Loss" in sold[0]["reason"] and pos.status == "CLOSED"


async def test_live_breakeven_stop_above_price_with_failed_sell_keeps_the_old_broker_stop(broker):
    _live_only(broker)
    broker._dispatch_real_close.return_value = None
    v = await write_main_rules({"breakeven_r": 0.5}, promoted_from="test")
    pid = await _add_position(broker, rules_version=v, stop=98.5, initial_stop=98.5)
    assert await _monitor(broker, 100.8, atr=None) == []
    pos = await _pos(pid)
    assert pos.status == "OPEN" and pos.stop_loss_native == 101.0
    broker._t212_move_stop.assert_not_awaited()


async def test_live_stop_below_price_still_moves_the_broker_stop_once(broker):
    _live_only(broker)
    pid = await _add_position(broker)
    assert await _monitor(broker, 106.0, atr=None) == []
    assert (await _pos(pid)).stop_loss_native == 101.0
    broker._t212_move_stop.assert_awaited_once_with(TICKER, "EUR", 1.0, 101.0)


async def test_live_default_position_with_tiny_breakeven_pct_keeps_stop_off_the_broker(broker):
    _live_only(broker)
    async with AsyncSessionLocal() as s:
        previous = await s.get(SystemConfig, "BREAKEVEN_TRIGGER_PCT")
        previous = previous.value if previous else None
        await s.merge(SystemConfig(key="BREAKEVEN_TRIGGER_PCT", value="0.005"))
        await s.commit()
    try:
        pid = await _add_position(broker)
        sold = await _monitor(broker, 100.6, atr=None)
        assert (await _pos(pid)).stop_loss_native == 101.0
        broker._t212_move_stop.assert_not_awaited()
        assert "Stop-Loss" in sold[0]["reason"]
    finally:
        async with AsyncSessionLocal() as s:
            if previous is None:
                await s.execute(delete(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT"))
            else:
                await s.merge(SystemConfig(key="BREAKEVEN_TRIGGER_PCT", value=previous))
            await s.commit()
