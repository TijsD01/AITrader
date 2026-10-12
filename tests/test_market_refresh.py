import asyncio
import datetime
import time
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, delete

from config.settings import settings
from src.data import market as market_module
from src.data.market import market_data_provider
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import OrderTrigger, Position, PortfolioSnapshot
from src.scheduler.jobs import trading_scheduler
from src.trading.paper import paper_broker
from src.web.app import app


def _fake_yf_ticker(calls):
    class FakeTicker:
        fast_info = {"lastPrice": 101.0, "currency": "USD"}
        info = {}

        def __init__(self, sym):
            calls.append(sym)

        def history(self, period):
            closes = np.linspace(90.0, 100.0, 40)
            return pd.DataFrame(
                {"Close": closes, "High": closes + 1.0, "Low": closes - 1.0, "Volume": 1000.0},
                index=pd.date_range("2026-01-01", periods=40),
            )

    return FakeTicker


def test_invalidate_forces_a_fresh_quote_fetch():
    calls = []
    with patch("src.data.market.yf.Ticker", _fake_yf_ticker(calls)):
        market_data_provider.get_stock_data("CACHETEST")
        market_data_provider.get_stock_data("CACHETEST")
        assert len(calls) == 1, "second read inside the TTL should come from the cache"

        market_data_provider.invalidate(["cachetest"])
        market_data_provider.get_stock_data("CACHETEST")
        assert len(calls) == 2


def test_invalidate_leaves_other_tickers_cached():
    calls = []
    with patch("src.data.market.yf.Ticker", _fake_yf_ticker(calls)):
        market_data_provider.get_stock_data("KEEPTEST")
        market_data_provider.invalidate(["OTHERTEST"])
        market_data_provider.get_stock_data("KEEPTEST")
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_trigger_job_syncs_account_balance_before_checking_triggers():
    order = []

    async def fake_sync():
        order.append("sync")
        return {}

    async def fake_check():
        order.append("check")
        return []

    full_sync = AsyncMock(return_value={})
    with patch.object(paper_broker, "sync_cash_balance", side_effect=fake_sync, create=True), \
         patch.object(paper_broker, "sync_account_state", full_sync), \
         patch.object(paper_broker, "check_and_execute_triggers", side_effect=fake_check):
        await trading_scheduler.monitor_price_triggers()

    assert order == ["sync", "check"]
    full_sync.assert_not_awaited(), "the scheduled job must not run the position-reconciling full sync"


@pytest.mark.asyncio
async def test_trigger_job_still_checks_triggers_when_balance_sync_fails():
    check = AsyncMock(return_value=[])
    with patch.object(paper_broker, "sync_cash_balance", side_effect=ConnectionError("IBKR down"), create=True), \
         patch.object(paper_broker, "check_and_execute_triggers", check):
        await trading_scheduler.monitor_price_triggers()

    check.assert_awaited_once()


async def _arm_refresh_trigger(ticker: str) -> int:
    """Arms one €100 paper trigger against a fresh €4,000 cash snapshot."""
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        session.add(PortfolioSnapshot(
            cash_eur=4000.0, equity_eur=0.0, total_value_eur=4000.0, open_positions_count=0, is_live=False,
            timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ))
        await session.commit()
    res = await paper_broker.create_price_trigger(
        ticker=ticker, name="Refresh Co", currency="EUR", trigger_type="LIMIT_PULLBACK",
        target_price=90.0, current_price_at_creation=100.0, stop_loss=85.0, take_profit=110.0,
        allocation_eur=100.0, consensus_score=99, ai_rationale="Testing manual refresh"
    )
    assert res["success"] is True
    return res["trigger_id"]


async def _delete_trigger(ticker: str):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        await session.commit()


@pytest.mark.asyncio
async def test_manual_refresh_drops_cached_quotes_and_resizes_triggers():
    ticker = "REFRESH_TEST"
    orig_max, orig_full = settings.MAX_POSITIONS, settings.FULL_BUDGET_UTILIZATION
    settings.FULL_BUDGET_UTILIZATION = True
    try:
        trigger_id = await _arm_refresh_trigger(ticker)
        async with AsyncSessionLocal() as session:
            open_count = len((await session.execute(
                select(Position).where(Position.status == "OPEN", Position.is_live == False)
            )).scalars().all())
        settings.MAX_POSITIONS = open_count + 4
        market_module._stock_cache[ticker] = (time.time(), {"price": 1.0})

        with patch("src.trading.paper.check_market_hours", return_value=(False, "Market is closed")), \
             patch.object(market_data_provider, "get_stock_data", return_value=None):
            result = await trading_scheduler.refresh_market_data_now()

        assert ticker not in market_module._stock_cache
        assert result["success"] is True
        assert result["tickers_refreshed"] >= 1
        assert result["triggers_resized"] >= 1
        assert result["triggers_executed"] == 0
        assert result["positions_closed"] == 0

        async with AsyncSessionLocal() as session:
            t = (await session.execute(select(OrderTrigger).where(OrderTrigger.id == trigger_id))).scalars().first()
            assert t.allocation_eur == 1000.0
    finally:
        settings.MAX_POSITIONS, settings.FULL_BUDGET_UTILIZATION = orig_max, orig_full
        await _delete_trigger(ticker)


@pytest.mark.asyncio
async def test_manual_refresh_never_overlaps_the_scheduled_trigger_check():
    running = 0
    max_running = 0

    async def slow_check():
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.05)
        running -= 1
        return []

    with patch.object(paper_broker, "sync_account_state", AsyncMock(return_value={})), \
         patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
         patch.object(paper_broker, "check_and_execute_triggers", side_effect=slow_check), \
         patch.object(paper_broker, "check_stops_and_targets", AsyncMock(return_value=[])):
        await asyncio.gather(
            trading_scheduler.monitor_price_triggers(),
            trading_scheduler.refresh_market_data_now(),
        )

    assert max_running == 1


@pytest.mark.asyncio
async def test_market_data_refresh_endpoint_reports_the_refresh_result():
    await init_db()
    transport = ASGITransport(app=app)
    with patch("src.trading.paper.check_market_hours", return_value=(False, "Market is closed")), \
         patch.object(market_data_provider, "get_stock_data", return_value=None):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/api/market-data/refresh")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    for key in ("tickers_refreshed", "triggers_resized", "triggers_executed", "positions_closed", "cash_eur"):
        assert key in body


def _fake_ib(cash: str):
    from types import SimpleNamespace
    ib = SimpleNamespace()
    ib.isConnected = lambda: True
    ib.accountSummaryAsync = AsyncMock(return_value=[
        SimpleNamespace(tag="TotalCashValue", currency="EUR", value=cash),
        SimpleNamespace(tag="NetLiquidation", currency="EUR", value="9000.0"),
        SimpleNamespace(tag="GrossPositionValue", currency="EUR", value="1500.0"),
    ])
    ib.positions = lambda: []  # IBKR lists no positions, e.g. briefly after a reconnect
    return ib


async def _latest_live_cash():
    async with AsyncSessionLocal() as session:
        snap = (await session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(PortfolioSnapshot.timestamp.desc(), PortfolioSnapshot.id.desc())
        )).scalars().first()
        return snap.cash_eur if snap else None


@pytest.mark.asyncio
async def test_live_cash_sync_records_broker_cash_and_keeps_unlisted_positions_open():
    from src.trading.live import live_broker
    await init_db()
    ticker = "CASHSYNC_TEST"
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"
    try:
        async with AsyncSessionLocal() as session:
            session.add(Position(
                ticker=ticker, name=ticker, currency="EUR", shares=5.0,
                entry_price_native=100.0, current_price_native=100.0, highest_price_native=100.0,
                entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=500.0,
                stop_loss_native=90.0, take_profit_native=120.0, status="OPEN", is_live=True
            ))
            await session.commit()

        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=_fake_ib("7777.0"))):
            await live_broker.sync_cash_balance()

        assert await _latest_live_cash() == 7777.0
        async with AsyncSessionLocal() as session:
            pos = (await session.execute(select(Position).where(Position.ticker == ticker))).scalars().first()
            assert pos.status == "OPEN"
            assert pos.stop_loss_native == 90.0
    finally:
        settings.LIVE_BROKER = orig_broker
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.cash_eur == 7777.0))
            await session.commit()


@pytest.mark.asyncio
async def test_live_cash_sync_keeps_last_balance_when_broker_is_disconnected():
    from src.trading.live import live_broker
    await init_db()
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"
    try:
        before = await _latest_live_cash()
        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=None)):
            await live_broker.sync_cash_balance()
        assert await _latest_live_cash() == before
    finally:
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_live_cash_sync_adds_no_snapshot_when_cash_is_unchanged():
    from sqlalchemy import func
    from src.trading.live import live_broker
    await init_db()
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"

    async def count_snapshots():
        async with AsyncSessionLocal() as session:
            return (await session.execute(
                select(func.count()).select_from(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.cash_eur == 6543.21)
            )).scalar()

    try:
        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=_fake_ib("6543.21"))):
            await live_broker.sync_cash_balance()
            await live_broker.sync_cash_balance()
        assert await count_snapshots() == 1
    finally:
        settings.LIVE_BROKER = orig_broker
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True, PortfolioSnapshot.cash_eur == 6543.21))
            await session.commit()
