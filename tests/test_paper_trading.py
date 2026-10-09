import pytest
from src.db.session import init_db
from src.trading.paper import paper_broker
from src.trading.risk import risk_guardrail
from config.settings import settings

@pytest.mark.asyncio
async def test_paper_broker_full_lifecycle():
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    settings.ENFORCE_MARKET_HOURS = False
    try:
        # 1. Initialize DB
        await init_db()

        # 2. Check initial portfolio balance
        summary = await paper_broker.get_portfolio_summary()
        assert summary["base_currency"] == "EUR"
        assert summary["cash_eur"] >= 1000.0

        # 3. Test Buy Order Execution (Buying 500 EUR of SAP.DE)
        buy_res = await paper_broker.execute_buy(
            ticker="SAP.DE",
            name="SAP SE",
            price_native=180.0,
            currency="EUR",
            stop_loss_native=172.0,
            take_profit_native=198.0,
            allocation_eur=800.0,
            consensus_score=85,
            rationale="Strong Technical Breakout"
        )
        assert buy_res["success"] is True
        assert buy_res["ticker"] == "SAP.DE"
        assert buy_res["shares"] > 0

        # 4. Verify position is now open
        positions = await paper_broker.get_positions()
        tickers = [p["ticker"] for p in positions]
        assert "SAP.DE" in tickers

        # 5. Test Risk Guardrail blocks duplicate position
        dup_res = await paper_broker.execute_buy(
            ticker="SAP.DE",
            name="SAP SE",
            price_native=180.0,
            currency="EUR",
            stop_loss_native=172.0,
            take_profit_native=198.0,
            allocation_eur=800.0,
            consensus_score=85,
            rationale="Second Buy"
        )
        assert dup_res["success"] is False
        assert "already exists" in dup_res["message"]

        # 6. Test Risk Guardrail blocks low consensus score
        low_score_res = await paper_broker.execute_buy(
            ticker="INGA.AS",
            name="ING Groep",
            price_native=15.0,
            currency="EUR",
            stop_loss_native=14.0,
            take_profit_native=17.0,
            allocation_eur=800.0,
            consensus_score=40,  # Below threshold
            rationale="Weak conviction"
        )
        assert low_score_res["success"] is False
        assert "below minimum threshold" in low_score_res["message"]

        # 7. Test Sell Order Execution
        sell_res = await paper_broker.execute_sell("SAP.DE", reason="Target Reached")
        assert sell_res["success"] is True
        assert sell_res["ticker"] == "SAP.DE"

        # 8. Verify position is closed
        positions_after = await paper_broker.get_positions()
        tickers_after = [p["ticker"] for p in positions_after]
        assert "SAP.DE" not in tickers_after

        # 9. Test: CANNOT invest money you do not have (no budget)
        curr_summary = await paper_broker.get_portfolio_summary()
        current_cash = curr_summary["cash_eur"]
        oversized_buy = await paper_broker.execute_buy(
            ticker="OVERSIZED",
            name="Oversized Corp",
            price_native=50.0,
            currency="EUR",
            stop_loss_native=45.0,
            take_profit_native=60.0,
            allocation_eur=current_cash + 5000.0,  # Exceeds available cash!
            consensus_score=85,
            rationale="Oversized trade test",
            is_manual=True
        )
        assert oversized_buy["success"] is False
        assert "Insufficient cash" in oversized_buy["message"] or "No available cash" in oversized_buy["message"]

        # Ensure cash did not change or go negative
        after_summary = await paper_broker.get_portfolio_summary()
        assert after_summary["cash_eur"] == current_cash
        assert after_summary["cash_eur"] >= 0.0
    finally:
        settings.ENFORCE_MARKET_HOURS = orig_enforce
