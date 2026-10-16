import pytest
from unittest.mock import patch
from sqlalchemy import delete, select
from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position, PortfolioSnapshot
from src.trading.live import live_broker
from src.trading.risk import calculate_position_allocation, risk_guardrail

def test_full_budget_utilization_allocation():
    """Verify full budget deployment across available slots."""
    # Scenario A: €1,000 available cash, 0 open positions, max 4 positions
    alloc = calculate_position_allocation(
        available_cash=1000.0,
        current_positions_count=0,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2500.0
    )
    # 1000 / 4 = 250 EUR
    assert alloc == 250.0

    # Scenario B: €600 available cash, 3 open positions, max 4 positions (ONLY 1 slot left)
    # MUST deploy 100% of the available remaining cash
    alloc_last_slot = calculate_position_allocation(
        available_cash=600.0,
        current_positions_count=3,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2500.0
    )
    assert alloc_last_slot == 600.0

    # Scenario C: Single order cap enforcement
    alloc_capped = calculate_position_allocation(
        available_cash=5000.0,
        current_positions_count=3,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2000.0
    )
    assert alloc_capped == 2000.0

    # Scenario D: When full_budget_utilization is False, uses traditional conservative 8%
    alloc_conservative = calculate_position_allocation(
        available_cash=1000.0,
        current_positions_count=0,
        full_budget_utilization=False,
        recommended_size_pct=0.08
    )
    assert alloc_conservative == 80.0

@pytest.mark.asyncio
async def test_live_broker_safety_interlocks():
    """Verify live broker blocks buys when safety interlocks or kill switches are active."""
    await init_db()
    orig_mode = settings.TRADING_MODE
    orig_live = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    settings.ENFORCE_MARKET_HOURS = False

    try:
        # 1. LIVE_TRADING_ENABLED is False -> MUST block real money orders
        settings.TRADING_MODE = "LIVE"
        settings.LIVE_TRADING_ENABLED = False
        settings.KILL_SWITCH_ACTIVE = False

        res = await live_broker.execute_buy(
            ticker="AAPL",
            name="Apple Inc",
            price_native=220.0,
            currency="USD",
            stop_loss_native=210.0,
            take_profit_native=240.0,
            allocation_eur=500.0,
            consensus_score=85,
            rationale="Test Interlock"
        )
        assert res["success"] is False
        assert "LIVE_TRADING_ENABLED is False" in res["message"]

        # 2. Emergency Kill Switch is ACTIVE -> MUST block real money orders
        settings.LIVE_TRADING_ENABLED = True
        settings.KILL_SWITCH_ACTIVE = True

        res_kill = await live_broker.execute_buy(
            ticker="AAPL",
            name="Apple Inc",
            price_native=220.0,
            currency="USD",
            stop_loss_native=210.0,
            take_profit_native=240.0,
            allocation_eur=500.0,
            consensus_score=85,
            rationale="Test Kill Switch"
        )
        assert res_kill["success"] is False
        assert "Emergency kill switch is ACTIVE" in res_kill["message"]
    finally:
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.ENFORCE_MARKET_HOURS = orig_enforce

@pytest.mark.asyncio
async def test_live_broker_full_execution_lifecycle():
    """Verify live order execution logs live trades with broker order IDs."""
    await init_db()
    orig_mode = settings.TRADING_MODE
    orig_live = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    orig_broker = settings.LIVE_BROKER

    settings.TRADING_MODE = "LIVE"
    settings.LIVE_TRADING_ENABLED = True
    settings.KILL_SWITCH_ACTIVE = False
    settings.ENFORCE_MARKET_HOURS = False
    settings.LIVE_BROKER = "ibkr"

    ticker = "NVDA_LIVE_TEST"
    try:
        # Clean up any residual test position from previous runs and ensure live test snapshot exists
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
            )
            if not snap_res.scalars().first():
                session.add(PortfolioSnapshot(
                    cash_eur=1000.0,
                    equity_eur=0.0,
                    total_value_eur=1000.0,
                    is_live=True,
                    open_positions_count=0
                ))
            await session.commit()

        # Mock quote for NVDA_LIVE_TEST so it doesn't make slow network calls
        mock_quote = {"price": 120.0, "currency": "USD", "name": "NVIDIA Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote), \
             patch.object(live_broker, "_dispatch_real_order", return_value="ibkr_1001"), \
             patch.object(live_broker, "_dispatch_real_close", return_value="ibkr_exit_1002"):
            # 1. Execute live buy
            buy_res = await live_broker.execute_buy(
                ticker=ticker,
                name="NVIDIA Corp",
                price_native=120.0,
                currency="USD",
                stop_loss_native=114.0,
                take_profit_native=132.0,
                allocation_eur=600.0,
                consensus_score=88,
                rationale="High conviction live trade test",
                is_manual=True
            )
            assert buy_res["success"] is True, buy_res
            assert buy_res["is_live"] is True
            assert buy_res["broker"] == "IBKR"
            assert buy_res["broker_order_id"].startswith("ibkr_")

            # 2. Check position record in database
            positions = await live_broker.get_positions()
            pos = next((p for p in positions if p["ticker"] == ticker), None)
            assert pos is not None
            assert pos["is_live"] is True
            assert pos["broker_order_id"] == buy_res["broker_order_id"]

            # 3. Check portfolio summary reflects live status
            summary = await live_broker.get_portfolio_summary()
            assert summary["trading_mode"] == "LIVE"
            assert summary["live_trading_enabled"] is True
            assert summary["broker"] == "IBKR"

            # 4. Execute live sell
            sell_res = await live_broker.execute_sell(ticker, reason="Profit Target Hit")
            assert sell_res["success"] is True
            assert sell_res["is_live"] is True
            assert sell_res["broker_order_id"].startswith("ibkr_exit_")

            # 5. Verify position is now closed
            positions_after = await live_broker.get_positions()
            assert not any(p["ticker"] == ticker for p in positions_after)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.commit()
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_live_broker_get_triggers_has_current_price_and_distance():
    """Verify live broker get_triggers returns current_price and distance_pct without undefined."""
    from src.db.models import OrderTrigger
    await init_db()

    ticker = "TRIG_LIVE_TEST"
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        t = OrderTrigger(
            ticker=ticker,
            name="Live Test Inc",
            currency="EUR",
            trigger_type="LIMIT_PULLBACK",
            target_price=90.0,
            current_price_at_creation=100.0,
            stop_loss=85.0,
            take_profit=110.0,
            allocation_eur=500.0,
            consensus_score=85,
            ai_rationale="Test pullback",
            status="PENDING",
            is_live=True
        )
        session.add(t)
        await session.commit()

    try:
        with patch.object(live_broker, "_fetch_ibkr_prices", return_value={ticker: 95.0}):
            triggers = await live_broker.get_triggers()
            found = next((tr for tr in triggers if tr["ticker"] == ticker), None)
            assert found is not None
            assert "current_price" in found
            assert found["current_price"] == 95.0
            assert "distance_pct" in found
            # ((90 / 95) - 1) * 100 = -5.26
            assert found["distance_pct"] == -5.26
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
            await session.commit()



@pytest.mark.asyncio
async def test_live_buy_not_recorded_when_broker_rejects():
    """A buy that never reached the broker must not create a position."""
    await init_db()
    orig = (settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE, settings.LIVE_BROKER)
    settings.TRADING_MODE = "LIVE"
    settings.LIVE_TRADING_ENABLED = True
    settings.KILL_SWITCH_ACTIVE = False
    settings.LIVE_BROKER = "ibkr"
    ticker = "REJ_LIVE_TEST"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            snap_res = await session.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            if not snap_res.scalars().first():
                session.add(PortfolioSnapshot(cash_eur=1000.0, equity_eur=0.0, total_value_eur=1000.0, is_live=True, open_positions_count=0))
            await session.commit()

        mock_quote = {"price": 120.0, "currency": "USD", "name": "Rejected Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote), \
             patch.object(live_broker, "_dispatch_real_order", return_value=None):
            res = await live_broker.execute_buy(
                ticker=ticker, name="Rejected Corp", price_native=120.0, currency="USD",
                stop_loss_native=114.0, take_profit_native=132.0, allocation_eur=600.0,
                consensus_score=88, rationale="broker down", is_manual=True
            )
        assert res["success"] is False
        positions = await live_broker.get_positions()
        assert not any(p["ticker"] == ticker for p in positions)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.commit()
        settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE, settings.LIVE_BROKER = orig


@pytest.mark.asyncio
async def test_portfolio_summary_uses_ibkr_values():
    """Dashboard total/equity come from IBKR account summary, not local quotes."""
    from types import SimpleNamespace as NS
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Position).where(Position.is_live == True))
        await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
        session.add(PortfolioSnapshot(cash_eur=100.0, equity_eur=300.0, total_value_eur=400.0, daily_pnl_eur=0.0,
                                      daily_pnl_pct=0.0, open_positions_count=0, is_live=True))
        await session.commit()

    class FakeIB:
        def isConnected(self):
            return True
        async def accountSummaryAsync(self):
            return [NS(tag="TotalCashValue", value="100.0", currency="EUR"),
                    NS(tag="NetLiquidation", value="431.69", currency="EUR"),
                    NS(tag="GrossPositionValue", value="331.69", currency="EUR")]

    try:
        with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(live_broker, "_get_ib", return_value=FakeIB()):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "ibkr"
            assert s["total_value_eur"] == 431.69 and s["equity_eur"] == 331.69
        # Gateway down: falls back to the stored snapshot
        with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(live_broker, "_get_ib", side_effect=ConnectionError):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "snapshot" and s["total_value_eur"] == 400.0
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.commit()
