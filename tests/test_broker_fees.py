import pytest
from unittest.mock import patch
from config.settings import settings
from src.trading.fees import (
    calculate_broker_fee,
    is_european_ticker
)
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
             patch.object(live_broker, "_dispatch_real_order", side_effect=lambda **kw: ("t212_2001", kw["shares"], kw["price"])), \
             patch.object(live_broker, "_dispatch_real_close", return_value="t212_exit_2002"):
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
         patch("src.agents.committee.llm_client.has_active_key", return_value=False):
        
        # When equity is €30, fee drag will trigger risk veto
        report = await investment_committee.evaluate_ticker(mock_stock, portfolio_equity_eur=30.0)
        assert report["action"] == "HOLD"
        assert report["risk"]["approved"] is False
        assert "VETO" in report["debate_transcript"]

