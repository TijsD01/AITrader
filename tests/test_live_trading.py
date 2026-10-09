import pytest
from unittest.mock import patch
from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position
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
        # Mock quote for NVDA_LIVE_TEST so it doesn't make slow network calls
        mock_quote = {"price": 120.0, "currency": "USD", "name": "NVIDIA Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote):
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
            assert buy_res["success"] is True
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
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.ENFORCE_MARKET_HOURS = orig_enforce
        settings.LIVE_BROKER = orig_broker
