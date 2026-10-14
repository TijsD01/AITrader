import pytest
from unittest.mock import patch, AsyncMock

from src.agents.base import LLMClient
from src.agents.committee import InvestmentCommittee
from src.agents.risk_manager import RiskManagerAgent
from src.agents.sentiment import SentimentAnalystAgent
from src.agents.technical import TechnicalAnalystAgent


def _stock(price=100.0, support=99.5, ema20=99.0, resistance=120.0):
    return {
        "ticker": "TST", "name": "Test Corp", "price": price, "currency": "USD",
        "sector": "Technology", "industry": "Software",
        "technicals": {
            "price": price, "ema20": ema20, "ema50": 95.0, "sma200": 90.0, "rsi14": 55.0,
            "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "atr14": 0.2,
            "support_20d": support, "resistance_20d": resistance, "volume_surge_ratio": 1.0,
            "return_1d_pct": 0.5, "return_5d_pct": 1.0, "return_1m_pct": 3.0,
            "above_ema20": True, "above_ema50": True, "above_sma200": True,
        },
        "fundamentals": {"beta": 1.0},
    }


def test_extract_json_returns_only_dicts():
    assert LLMClient._extract_json('{"a": 1}') == {"a": 1}
    assert LLMClient._extract_json('noise {"a": 1} tail') == {"a": 1}
    assert LLMClient._extract_json("[1, 2, 3]") is None
    assert LLMClient._extract_json("42") is None


@pytest.mark.asyncio
async def test_risk_rule_path_anchors_stop_and_target_on_pullback_entry():
    # Support sits just under the price, so the pullback entry lands on support and the
    # old price-anchored stop would have been ~0.1% below (or above) the entry.
    with patch("src.agents.risk_manager.llm_client.has_active_key", return_value=False):
        rep = await RiskManagerAgent.evaluate_risk(_stock(), {"bias": "NEUTRAL"}, {"health": "STRONG"}, 10000.0)
    entry, stop, tp = rep["entry_trigger_price"], rep["stop_loss_price"], rep["take_profit_price"]
    assert rep["trigger_type"] == "LIMIT_PULLBACK"
    assert entry <= 100.0
    assert stop < entry < tp
    assert 0.93 * entry - 0.01 <= stop <= 0.975 * entry + 0.01
    assert (tp - entry) / (entry - stop) == pytest.approx(rep["risk_reward_ratio"], rel=0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_res", [
    {"stop_loss_price": "abc"},
    {"stop_loss_price": 105.0},                                                    # stop above entry
    {"stop_loss_price": 95.0, "entry_trigger_price": 99.0, "take_profit_price": 98.0},   # tp below entry
    {"stop_loss_price": 95.0, "entry_trigger_price": 101.0, "trigger_type": "LIMIT_PULLBACK", "take_profit_price": 110.0},
    {"stop_loss_price": 95.0, "entry_trigger_price": 99.0, "trigger_type": "BREAKOUT_BUY", "take_profit_price": 110.0},
    {"stop_loss_price": 95.0, "entry_trigger_price": 99.0, "trigger_type": "MOON", "take_profit_price": 110.0},
    ["not", "a", "dict"],
])
async def test_risk_llm_invalid_levels_fall_back_to_rules(llm_res):
    with patch("src.agents.risk_manager.llm_client.has_active_key", return_value=True), \
         patch("src.agents.risk_manager.llm_client.generate_json", new=AsyncMock(return_value=llm_res)):
        rep = await RiskManagerAgent.evaluate_risk(_stock(), {"bias": "NEUTRAL"}, {"health": "STRONG"}, 10000.0)
    assert rep["stop_loss_price"] < rep["entry_trigger_price"] < rep["take_profit_price"]
    assert "risk_summary" in rep and "Trade structure" in rep["risk_summary"]


@pytest.mark.asyncio
async def test_risk_llm_valid_response_is_normalised():
    llm_res = {"approved": "false", "stop_loss_price": "95", "entry_trigger_price": 99.0,
               "trigger_type": "LIMIT_PULLBACK", "take_profit_price": 110, "recommended_size_pct": 50}
    with patch("src.agents.risk_manager.llm_client.has_active_key", return_value=True), \
         patch("src.agents.risk_manager.llm_client.generate_json", new=AsyncMock(return_value=llm_res)):
        rep = await RiskManagerAgent.evaluate_risk(_stock(), {"bias": "NEUTRAL"}, {"health": "STRONG"}, 10000.0)
    assert rep["approved"] is False
    assert rep["stop_loss_price"] == 95.0 and rep["take_profit_price"] == 110.0
    assert rep["recommended_size_pct"] == 0.12  # 50 -> 0.5 -> clamped to 0.12
    for key in ("risk_summary", "risk_flags", "trigger_rationale", "risk_reward_ratio"):
        assert key in rep


@pytest.mark.asyncio
async def test_technical_llm_partial_response_keeps_all_keys():
    with patch("src.agents.technical.llm_client.has_active_key", return_value=True), \
         patch("src.agents.technical.llm_client.generate_json", new=AsyncMock(return_value={"bias": "bullish"})):
        rep = await TechnicalAnalystAgent.analyze(_stock())
    assert rep["bias"] == "BULLISH"
    for key in ("confidence", "key_drivers", "support_level", "resistance_level", "analysis_summary"):
        assert key in rep


@pytest.mark.asyncio
@pytest.mark.parametrize("title,expected", [
    ("Executive shuffle at the firm", 0.0),       # "cut" in "executive" must not match
    ("Company to cut guidance", -0.35),
    ("Shares surge on record profit", 0.75),
    (None, 0.0),
])
async def test_sentiment_matches_whole_words(title, expected):
    news = [{"title": title, "publisher": "x", "time": "now"}]
    with patch("src.agents.sentiment.llm_client.has_active_key", return_value=False), \
         patch("src.agents.sentiment.news_service.get_ticker_news", return_value=news):
        rep = await SentimentAnalystAgent.analyze(_stock())
    assert rep["score"] == pytest.approx(expected)


@pytest.mark.parametrize("res,expected", [
    ({"action": "BUY", "consensus_score": "85/100"}, ("BUY", 85)),
    ({"action": "strong buy", "consensus_score": 150}, ("STRONG_BUY", 100)),
    ({"action": "HOLD", "consensus_score": -4}, ("HOLD", 0)),
    ({"action": "BUY", "consensus_score": "high"}, None),
    ({"action": "MOON", "consensus_score": 80}, None),
    ("garbage", None),
    (None, None),
])
def test_committee_parse_llm_verdict(res, expected):
    assert InvestmentCommittee._parse_llm_verdict(res) == expected


@pytest.mark.asyncio
async def test_committee_llm_malformed_and_nonbuy_cap():
    # llm_client is a shared singleton: answer only the committee's prompt, let sub-agents use their rules
    def fake(verdict):
        async def _gen(system_prompt, user_prompt):
            return verdict if "Synthesize the debate" in user_prompt else None
        return _gen

    with patch("src.agents.sentiment.news_service.get_ticker_news", return_value=[]), \
         patch("src.agents.committee.llm_client.has_active_key", return_value=True):
        with patch("src.agents.committee.llm_client.generate_json", new=fake(["x"])):
            out = await InvestmentCommittee.evaluate_ticker(_stock())  # must not raise
        assert out["action"] in ("STRONG_BUY", "BUY", "HOLD", "SELL")
        verdict = {"action": "SELL", "consensus_score": 90, "verdict_summary": "no"}
        with patch("src.agents.committee.llm_client.generate_json", new=fake(verdict)):
            out = await InvestmentCommittee.evaluate_ticker(_stock())
        assert out["action"] == "SELL" and out["consensus_score"] == 55
