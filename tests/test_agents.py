import pytest
from unittest.mock import patch
from src.agents.technical import TechnicalAnalystAgent
from src.agents.fundamental import FundamentalAnalystAgent
from src.agents.sentiment import SentimentAnalystAgent
from src.agents.risk_manager import RiskManagerAgent
from src.agents.committee import investment_committee

@pytest.mark.asyncio
async def test_specialist_agents_and_committee():
    with patch("src.agents.base.llm_client.has_active_key", return_value=False), \
         patch("src.agents.risk_manager.llm_client.has_active_key", return_value=False), \
         patch("src.agents.committee.llm_client.has_active_key", return_value=False):
        # Mock stock profile
        mock_stock = {
            "ticker": "NVDA",
            "name": "NVIDIA Corporation",
            "price": 125.50,
            "currency": "USD",
            "sector": "Technology",
            "industry": "Semiconductors",
            "technicals": {
                "price": 125.50,
                "ema20": 122.0,
                "ema50": 118.0,
                "sma200": 105.0,
                "rsi14": 62.5,
                "macd": 2.4,
                "macd_signal": 1.8,
                "macd_hist": 0.6,
                "atr14": 3.8,
                "support_20d": 116.0,
                "resistance_20d": 130.0,
                "volume_surge_ratio": 1.35,
                "return_1d_pct": 1.5,
                "return_5d_pct": 4.2,
                "return_1m_pct": 11.0,
                "above_ema20": True,
                "above_ema50": True,
                "above_sma200": True,
            },
            "fundamentals": {
                "market_cap": 3000000000000,
                "trailing_pe": 45.0,
                "forward_pe": 32.0,
                "peg_ratio": 1.1,
                "price_to_book": 25.0,
                "profit_margins": 0.55,
                "operating_margins": 0.60,
                "return_on_equity": 0.70,
                "revenue_growth": 0.85,
                "earnings_growth": 1.10,
                "debt_to_equity": 15.0,
                "dividend_yield": 0.001,
                "beta": 1.45,
                "target_mean_price": 145.0,
            }
        }

        # 1. Test Technical Agent
        tech = await TechnicalAnalystAgent.analyze(mock_stock)
        assert tech["bias"] in ("BULLISH", "NEUTRAL", "BEARISH")
        assert 0 <= tech["confidence"] <= 100

        # 2. Test Fundamental Agent
        fund = await FundamentalAnalystAgent.analyze(mock_stock)
        assert fund["valuation"] in ("UNDERVALUED", "FAIR", "OVERVALUED")
        assert 0 <= fund["confidence"] <= 100

        # 3. Test Sentiment Agent
        sent = await SentimentAnalystAgent.analyze(mock_stock)
        assert sent["mood"] in ("POSITIVE", "NEUTRAL", "NEGATIVE")
        assert -1.0 <= sent["score"] <= 1.0

        # 4. Test Risk Manager
        risk = await RiskManagerAgent.evaluate_risk(mock_stock, tech, fund, 10000.0)
        assert risk["approved"] is True
        assert risk["stop_loss_price"] < mock_stock["price"]
        assert risk["take_profit_price"] > mock_stock["price"]
        assert risk["risk_reward_ratio"] >= 1.5

        # 5. Test Full Committee Debate
        report = await investment_committee.evaluate_ticker(mock_stock, 10000.0)
        assert report["action"] in ("STRONG_BUY", "BUY", "HOLD", "SELL")
        assert 0 <= report["consensus_score"] <= 100
        assert len(report["debate_transcript"]) > 50
        assert "Portfolio Manager" in report["debate_transcript"]
