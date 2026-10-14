import pytest
from unittest.mock import patch
from config.settings import settings
from src.trading.fees import (
    calculate_broker_fee,
    calculate_round_trip_fee,
    get_min_viable_order_eur,
    evaluate_trade_economic_viability,
    is_european_ticker
)
from src.trading.risk import calculate_position_allocation, validate_buy_order
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Position, Trade, PortfolioSnapshot
from src.trading.live import live_broker

def test_is_european_ticker():
    assert is_european_ticker("BESI.AS") is True
    assert is_european_ticker("SAP.DE") is True
    assert is_european_ticker("AIR.PA") is True
    assert is_european_ticker("AAPL") is False
    assert is_european_ticker("NVDA") is False
    assert is_european_ticker("XYZ", currency="EUR") is True

def test_ibkr_european_fee_calculation():
    # 1. IBKR Tiered Euronext (e.g. BESI.AS, €33.33 trade)
    fee_tiered = calculate_broker_fee(
        ticker="BESI.AS",
        shares=0.1895,
        price_native=175.88,
        currency="EUR",
        broker="ibkr",
        pricing_tier="tiered"
    )
    # Trade value ~ €33.33. Base commission min is €1.25 + €0.65 exchange/clearing = €1.90
    assert fee_tiered["fee_eur"] >= 1.90
    assert fee_tiered["is_european"] is True
    assert fee_tiered["currency"] == "EUR"

    # 2. IBKR Fixed Euronext (min €3.00)
    fee_fixed = calculate_broker_fee(
        ticker="BESI.AS",
        shares=0.1895,
        price_native=175.88,
        currency="EUR",
        broker="ibkr",
        pricing_tier="fixed"
    )
    assert fee_fixed["fee_eur"] >= 3.00

    # 3. IBKR Tiered XETRA (min €1.25 + €0.95 = €2.20)
    fee_xetra = calculate_broker_fee(
        ticker="SAP.DE",
        shares=1.0,
        price_native=180.0,
        currency="EUR",
        broker="ibkr",
        pricing_tier="tiered"
    )
    assert fee_xetra["fee_eur"] >= 2.20

def test_ibkr_us_fee_calculation():
    # 1. US Tiered BUY (e.g. AAPL 1 share @ $200)
    fee_us_buy = calculate_broker_fee(
        ticker="AAPL",
        shares=1.0,
        price_native=200.0,
        currency="USD",
        broker="ibkr",
        pricing_tier="tiered",
        order_action="BUY"
    )
    # Tiered min is $0.35 + clearing ~$0.01 = ~$0.36 USD -> in EUR ~0.33
    assert 0.25 <= fee_us_buy["fee_eur"] <= 0.60
    assert fee_us_buy["is_european"] is False

    # 2. US Fixed BUY (min $1.00 USD -> in EUR ~0.93)
    fee_us_fixed = calculate_broker_fee(
        ticker="AAPL",
        shares=1.0,
        price_native=200.0,
        currency="USD",
        broker="ibkr",
        pricing_tier="fixed",
        order_action="BUY"
    )
    assert 0.80 <= fee_us_fixed["fee_eur"] <= 1.20

def test_round_trip_fee_and_fee_drag():
    # Round-trip for €33.33 trade on BESI.AS
    rt = calculate_round_trip_fee(
        ticker="BESI.AS",
        shares=0.1895,
        entry_price=175.88,
        exit_price=191.64,
        currency="EUR",
        broker="ibkr",
        pricing_tier="tiered"
    )
    # Entry ~ €1.90, exit ~ €1.90 -> total round-trip ~ €3.80
    assert rt["round_trip_fee_eur"] >= 3.80
    # Fee drag = 3.80 / 33.33 = ~11.4%
    assert rt["round_trip_drag_pct"] > 10.0

def test_economic_viability_rejection():
    # A €33.33 order on BESI.AS with 8% TP (+€2.67 gross profit)
    # Round-trip fee €3.80 exceeds gross profit -> guaranteed loss!
    viability = evaluate_trade_economic_viability(
        ticker="BESI.AS",
        allocation_eur=33.33,
        price_native=175.88,
        stop_loss_native=171.94,
        take_profit_native=191.64,
        currency="EUR",
        broker="ibkr",
        pricing_tier="tiered"
    )
    assert viability["viable"] is False
    assert viability["net_profit_eur"] <= 0 or viability["fee_drag_pct"] > 3.5

    # A €150.00 order on BESI.AS (gross profit at 9% is €13.50, fee is €3.80 -> net +€9.70)
    viability_viable = evaluate_trade_economic_viability(
        ticker="BESI.AS",
        allocation_eur=150.0,
        price_native=175.88,
        stop_loss_native=171.94,
        take_profit_native=191.64,
        currency="EUR",
        broker="ibkr",
        pricing_tier="tiered"
    )
    assert viability_viable["viable"] is True
    assert viability_viable["net_profit_eur"] > 0
    assert viability_viable["fee_drag_pct"] <= 3.5

def test_fee_aware_allocation_consolidation():
    # In a €200 account with MAX_POSITIONS=6, raw division produces 200/6 = €33.33
    # Fee-aware sizing consolidates slots so each allocation meets minimum viable size (~€75-€100)
    alloc = calculate_position_allocation(
        available_cash=200.0,
        current_positions_count=0,
        full_budget_utilization=True,
        max_positions=6,
        ticker="BESI.AS",
        currency="EUR"
    )
    # Must NOT be diluted down to €33.33; should be consolidated into 2 slots (€100.00) or 1 slot (€200.00)
    assert alloc >= 75.0
    assert alloc in (100.0, 200.0)

def test_validate_buy_order_fee_guardrail():
    # Mock positions empty
    positions = []
    
    # 1. Unviable €33.33 trade with price_native provided should be blocked
    valid, msg = validate_buy_order(
        ticker="BESI.AS",
        current_positions=positions,
        cash_eur=200.0,
        portfolio_total_eur=200.0,
        allocation_eur=33.33,
        consensus_score=85,
        auto_buy_enabled=True,
        available_cash_eur=200.0,
        price_native=175.88,
        currency="EUR",
        stop_loss_native=171.94,
        take_profit_native=191.64
    )
    assert valid is False
    assert "Fee guardrail" in msg

    # 2. Viable €120.00 trade should be approved
    valid_ok, msg_ok = validate_buy_order(
        ticker="BESI.AS",
        current_positions=positions,
        cash_eur=200.0,
        portfolio_total_eur=200.0,
        allocation_eur=120.0,
        consensus_score=85,
        auto_buy_enabled=True,
        available_cash_eur=200.0,
        price_native=175.88,
        currency="EUR",
        stop_loss_native=168.0,
        take_profit_native=195.0
    )
    assert valid_ok is True
    assert msg_ok == "Approved"

@pytest.mark.asyncio
async def test_live_execution_fee_accounting():
    """Verify live order execution deducts broker fees and tracks fees on positions and trades."""
    await init_db()
    orig_mode = settings.TRADING_MODE
    orig_live = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    orig_enforce = settings.ENFORCE_MARKET_HOURS

    settings.TRADING_MODE = "LIVE"
    settings.LIVE_TRADING_ENABLED = True
    settings.KILL_SWITCH_ACTIVE = False
    settings.ENFORCE_MARKET_HOURS = False

    ticker = "NVDA_FEE_TEST"
    try:
        async with AsyncSessionLocal() as session:
            # Seed test snapshot with €500 cash
            session.add(PortfolioSnapshot(
                cash_eur=500.0,
                equity_eur=0.0,
                total_value_eur=500.0,
                is_live=True
            ))
            await session.commit()

        mock_quote = {"price": 100.0, "currency": "USD", "name": "NVIDIA Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote), \
             patch.object(live_broker, "_dispatch_real_order", return_value="ibkr_2001"), \
             patch.object(live_broker, "_dispatch_real_close", return_value="ibkr_exit_2002"):
            # Execute BUY of €100
            buy_res = await live_broker.execute_buy(
                ticker=ticker,
                name="NVIDIA Corp",
                price_native=100.0,
                currency="USD",
                stop_loss_native=95.0,
                take_profit_native=115.0,
                allocation_eur=100.0,
                consensus_score=85,
                rationale="Fee accounting test",
                is_manual=True
            )
            assert buy_res["success"] is True
            assert "fee_eur" in buy_res
            assert buy_res["fee_eur"] > 0

            # Verify Position has fees_eur
            positions = await live_broker.get_positions()
            pos = next((p for p in positions if p["ticker"] == ticker), None)
            assert pos is not None
            assert pos.get("fees_eur", 0) > 0

            # Execute SELL
            sell_res = await live_broker.execute_sell(ticker, reason="Take profit")
            assert sell_res["success"] is True
            assert "realized_pnl_eur" in sell_res

    finally:
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.ENFORCE_MARKET_HOURS = orig_enforce

def test_uk_stamp_duty_sdr():
    """Verify UK London Stock Exchange (.L) equities incur 0.5% SDRT on BUY only."""
    # BUY £1000 of SHEL.L (100 shares @ £10.00)
    fee_buy = calculate_broker_fee(
        ticker="SHEL.L",
        shares=100.0,
        price_native=10.0,
        currency="GBP",
        broker="ibkr",
        pricing_tier="tiered",
        order_action="BUY"
    )
    # 0.5% of £1000 = £5.00 SDRT + IBKR base comm (~£1.00)
    assert fee_buy["fee_native"] >= 6.0
    assert "SDRT" in fee_buy["breakdown"]

    # SELL £1000 of SHEL.L - no stamp duty on sells
    fee_sell = calculate_broker_fee(
        ticker="SHEL.L",
        shares=100.0,
        price_native=10.0,
        currency="GBP",
        broker="ibkr",
        pricing_tier="tiered",
        order_action="SELL"
    )
    assert "SDRT" not in fee_sell["breakdown"]
    assert fee_sell["fee_native"] < fee_buy["fee_native"]

@pytest.mark.asyncio
async def test_risk_manager_veto_on_unviable_fees():
    """Verify RiskManagerAgent marks setup as disapproved when broker fee drag is unviable."""
    from src.agents.risk_manager import RiskManagerAgent

    mock_stock = {
        "ticker": "BESI.AS",
        "name": "BE Semiconductor",
        "price": 175.88,
        "currency": "EUR",
        "technicals": {"atr14": 3.0, "support_20d": 170.0, "resistance_20d": 185.0},
        "fundamentals": {"beta": 1.1}
    }
    mock_tech = {"bias": "BULLISH", "confidence": 85, "analysis_summary": "Strong momentum"}
    mock_fund = {"valuation": "UNDERVALUED", "health": "STRONG", "analysis_summary": "Good fundamentals"}

    with patch("src.agents.risk_manager.llm_client.has_active_key", return_value=False):
        # Portfolio equity of only €30 -> cannot support minimum viable order size
        risk_rep = await RiskManagerAgent.evaluate_risk(mock_stock, mock_tech, mock_fund, portfolio_equity_eur=30.0)
        assert risk_rep["approved"] is False
        assert "REJECTED" in risk_rep["risk_summary"]

@pytest.mark.asyncio
async def test_investment_committee_downgrades_to_hold_on_risk_veto():
    """Verify InvestmentCommittee downgrades bullish signal to HOLD if Risk Officer disapproves."""
    from src.agents.committee import investment_committee

    mock_stock = {
        "ticker": "BESI.AS",
        "name": "BE Semiconductor",
        "price": 175.88,
        "currency": "EUR",
        "technicals": {
            "price": 175.88,
            "rsi14": 60.0,
            "ema20": 172.0,
            "support_20d": 170.0,
            "resistance_20d": 180.0,
            "atr14": 3.5,
            "volume_surge_ratio": 1.2,
            "return_5d_pct": 3.0
        },
        "fundamentals": {"beta": 1.1, "market_cap": 12000000000}
    }

    with patch("src.agents.base.llm_client.has_active_key", return_value=False), \
         patch("src.agents.risk_manager.llm_client.has_active_key", return_value=False), \
         patch("src.agents.committee.llm_client.has_active_key", return_value=False):
        
        # When equity is €30, fee drag will trigger risk veto
        report = await investment_committee.evaluate_ticker(mock_stock, portfolio_equity_eur=30.0)
        assert report["action"] == "HOLD"
        assert report["risk"]["approved"] is False
        assert "VETO" in report["debate_transcript"]

