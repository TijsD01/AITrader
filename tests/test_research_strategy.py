"""Strategy controls taken from the trading research review: risk budget, trailing stop,
market regime filter, sentiment veto, 52-week high and the learner's trade-count gate."""
import datetime
import time
from unittest.mock import patch, AsyncMock, MagicMock

import pandas as pd
import pytest
from sqlalchemy import select, delete, func

from config.settings import settings
from src.agents.committee import investment_committee
from src.agents.sentiment import anonymize_text
from src.data import market as market_module
from src.data.market import market_data_provider
from src.db.models import OrderTrigger, SystemConfig, Trade
from src.db.session import init_db, AsyncSessionLocal
from src.screener.scanner import MarketScanner
from src.trading.learner import trade_learner
from src.trading.paper import paper_broker
from src.trading.risk import apply_risk_budget, compute_trailing_stop, check_market_regime, regime_index_for_ticker


def _unique(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}"


# --- Risk budget per trade ------------------------------------------------------------

def test_risk_budget_sizes_by_stop_distance():
    # €10k portfolio, 0.5% risk: a 4% stop allows €1,250, an 8% stop half of that
    assert apply_risk_budget(2000.0, 100.0, 96.0, 10000.0, risk_pct=0.5) == 1250.0
    assert apply_risk_budget(2000.0, 100.0, 92.0, 10000.0, risk_pct=0.5) == 625.0
    # A tight stop never raises the slot allocation
    assert apply_risk_budget(1000.0, 100.0, 98.0, 10000.0, risk_pct=0.5) == 1000.0


def test_risk_budget_leaves_allocation_alone_without_a_usable_stop():
    assert apply_risk_budget(1000.0, 100.0, None, 10000.0, risk_pct=0.5) == 1000.0
    assert apply_risk_budget(1000.0, 100.0, 101.0, 10000.0, risk_pct=0.5) == 1000.0
    assert apply_risk_budget(1000.0, 100.0, 90.0, 10000.0, risk_pct=0.0) == 1000.0


# --- Trailing stop --------------------------------------------------------------------

def test_trailing_stop_waits_for_the_breakeven_step():
    assert compute_trailing_stop(100.0, 96.0, 102.0, 2.0, 0.025, atr_mult=2.5) == 96.0


def test_trailing_stop_moves_to_breakeven_then_trails_by_atr():
    # +3%: highest - 2.5 ATR (98) is below breakeven + 1%, so the stop goes to 101
    assert compute_trailing_stop(100.0, 96.0, 103.0, 2.0, 0.025, atr_mult=2.5) == 101.0
    # +12%: the ATR trail (112 - 5 = 107) is now above breakeven
    assert compute_trailing_stop(100.0, 101.0, 112.0, 2.0, 0.025, atr_mult=2.5) == 107.0


def test_trailing_stop_never_lowers_and_can_be_switched_off():
    assert compute_trailing_stop(100.0, 108.0, 112.0, 2.0, 0.025, atr_mult=2.5) == 108.0
    assert compute_trailing_stop(100.0, 96.0, 112.0, 2.0, 0.025, atr_mult=0) == 101.0
    assert compute_trailing_stop(100.0, 96.0, 112.0, None, 0.025, atr_mult=2.5) == 101.0


# --- Market regime filter ------------------------------------------------------------

def test_regime_index_follows_the_tickers_market():
    assert regime_index_for_ticker("ASML.AS") == settings.REGIME_INDEX_EU
    assert regime_index_for_ticker("AAPL", "USD") == settings.REGIME_INDEX_US


def test_regime_filter_blocks_entries_below_the_average(monkeypatch):
    monkeypatch.setattr(settings, "REGIME_FILTER_ENABLED", True)
    below = {"index": "^GSPC", "price": 4000.0, "sma": 4200.0, "sma_days": 200, "above_sma": False}
    above = {**below, "price": 4400.0, "above_sma": True}
    with patch.object(market_data_provider, "get_index_regime", return_value=below):
        allowed, reason = check_market_regime("AAPL", "USD")
        assert not allowed and "below its 200-day average" in reason
    with patch.object(market_data_provider, "get_index_regime", return_value=above):
        assert check_market_regime("AAPL", "USD") == (True, "")
    # Missing index data fails open
    with patch.object(market_data_provider, "get_index_regime", return_value=None):
        assert check_market_regime("AAPL", "USD")[0] is True
    monkeypatch.setattr(settings, "REGIME_FILTER_ENABLED", False)
    with patch.object(market_data_provider, "get_index_regime", return_value=below):
        assert check_market_regime("AAPL", "USD")[0] is True


def test_index_regime_compares_last_close_with_its_average():
    market_module._regime_cache.clear()
    closes = pd.Series([100.0] * 199 + [80.0])  # last close below the 200-day mean
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame({"Close": closes})
    with patch("src.data.market.yf.Ticker", return_value=ticker):
        regime = market_data_provider.get_index_regime("^TEST", 200)
    assert regime["above_sma"] is False
    assert regime["price"] == 80.0 and regime["sma"] == 99.9
    market_module._regime_cache.clear()


@pytest.mark.asyncio
async def test_paper_trigger_waits_while_the_regime_filter_blocks():
    await init_db()
    ticker = _unique("REGIME")
    async with AsyncSessionLocal() as session:
        trigger = OrderTrigger(
            ticker=ticker, name="Regime Corp", currency="USD", trigger_type="LIMIT_PULLBACK",
            target_price=100.0, current_price_at_creation=101.0, stop_loss=95.0, take_profit=110.0,
            allocation_eur=300.0, consensus_score=90, ai_rationale="Test", status="PENDING", is_live=False,
        )
        session.add(trigger)
        await session.commit()
        trigger_id = trigger.id

    quote = {"ticker": ticker, "price": 99.0, "currency": "USD", "name": "Regime Corp"}
    with patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
         patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=lambda s: quote if s == ticker else None), \
         patch("src.trading.paper.check_market_regime", return_value=(False, "index below average")):
        executed = await paper_broker.check_and_execute_triggers()

    assert not any(e.get("ticker") == ticker for e in executed)
    async with AsyncSessionLocal() as session:
        status = (await session.execute(select(OrderTrigger.status).where(OrderTrigger.id == trigger_id))).scalar()
        await session.execute(delete(OrderTrigger).where(OrderTrigger.id == trigger_id))
        await session.commit()
    assert status == "PENDING"


@pytest.mark.asyncio
async def test_paper_trigger_buy_is_capped_by_the_risk_budget(monkeypatch):
    await init_db()
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "portfolio")
    ticker = _unique("RISKCAP")
    async with AsyncSessionLocal() as session:
        trigger = OrderTrigger(
            ticker=ticker, name="Risk Corp", currency="EUR", trigger_type="LIMIT_PULLBACK",
            target_price=100.0, current_price_at_creation=101.0, stop_loss=50.0, take_profit=200.0,
            allocation_eur=100000.0, consensus_score=90, ai_rationale="Test", status="PENDING", is_live=False,
        )
        session.add(trigger)
        await session.commit()
        trigger_id = trigger.id

    # Capture what reaches the guardrails instead of buying, so shared portfolio state does not matter
    seen = {}
    def _capture(**kwargs):
        seen.update(kwargs)
        return False, "captured"

    quote = {"ticker": ticker, "price": 100.0, "currency": "EUR", "name": "Risk Corp"}
    with patch.object(paper_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
         patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
         patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=lambda s: quote if s == ticker else None), \
         patch("src.trading.paper.risk_guardrail.validate_buy_order", side_effect=_capture):
        await paper_broker.check_and_execute_triggers()

    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.id == trigger_id))
        await session.commit()
    # A 50% stop at 0.5% risk allows 1% of the portfolio value
    assert seen["allocation_eur"] == pytest.approx(seen["portfolio_total_eur"] * 0.01, abs=0.01)


# --- Sentiment ---------------------------------------------------------------------------

def test_headlines_are_anonymized():
    text = "- ASML's orders beat; ASML Holding N.V. shares rise (Reuters)"
    out = anonymize_text(text, "ASML.AS", "ASML Holding N.V.")
    assert "ASML" not in out and out.count("the company") == 2
    assert anonymize_text("- Apple Inc. sued; AAPL falls, apples fine", "AAPL", "Apple Inc.") == \
        "- the company sued; the company falls, apples fine"


def _committee_patches(sent, llm_active, llm_verdict=None):
    tech = {"bias": "BULLISH", "confidence": 90, "analysis_summary": "Uptrend", "key_drivers": []}
    fund = {"valuation": "UNDERVALUED", "health": "STRONG", "confidence": 85, "analysis_summary": "Solid"}
    risk = {
        "approved": True, "entry_trigger_price": 98.0, "trigger_type": "LIMIT_PULLBACK",
        "stop_loss_price": 95.0, "take_profit_price": 110.0, "recommended_size_pct": 0.08,
        "risk_reward_ratio": 2.1, "risk_summary": "OK",
    }
    return [
        patch("src.agents.committee.TechnicalAnalystAgent.analyze", AsyncMock(return_value=tech)),
        patch("src.agents.committee.FundamentalAnalystAgent.analyze", AsyncMock(return_value=fund)),
        patch("src.agents.committee.SentimentAnalystAgent.analyze", AsyncMock(return_value=sent)),
        patch("src.agents.committee.RiskManagerAgent.evaluate_risk", AsyncMock(return_value=risk)),
        patch("src.agents.committee.llm_client.has_active_key", return_value=llm_active),
        patch("src.agents.committee.llm_client.generate_json", AsyncMock(return_value=llm_verdict)),
    ]


async def _evaluate(patches):
    stock = {"ticker": "NEWS", "name": "News Corp", "price": 100.0, "currency": "USD"}
    for p in patches:
        p.start()
    try:
        return await investment_committee.evaluate_ticker(stock, 10000.0)
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_negative_news_vetoes_an_llm_buy():
    sent = {"mood": "NEGATIVE", "score": -0.7, "sentiment_summary": "Probe announced"}
    verdict = {"action": "BUY", "consensus_score": 86, "verdict_summary": "Buy", "debate_transcript": "Debate"}
    report = await _evaluate(_committee_patches(sent, True, verdict))
    assert report["action"] == "HOLD"
    assert report["consensus_score"] <= 55


@pytest.mark.asyncio
async def test_positive_news_does_not_raise_the_rule_score():
    neutral = await _evaluate(_committee_patches({"mood": "NEUTRAL", "score": 0.0, "sentiment_summary": "-"}, False))
    positive = await _evaluate(_committee_patches({"mood": "POSITIVE", "score": 0.9, "sentiment_summary": "+"}, False))
    assert positive["consensus_score"] == neutral["consensus_score"]


# --- Scanner -----------------------------------------------------------------------------

def test_scanner_rewards_stocks_near_their_52_week_high():
    technicals = {
        "price": 100.0, "above_ema20": True, "above_ema50": True, "above_sma200": True, "rsi14": 55.0,
        "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "volume_surge_ratio": 1.0,
    }
    near = {"technicals": {**technicals, "pct_below_52w_high": 2.0}, "fundamentals": {}}
    far = {"technicals": {**technicals, "pct_below_52w_high": 20.0}, "fundamentals": {}}
    assert MarketScanner._evaluate_technical_setup(near)[0] - MarketScanner._evaluate_technical_setup(far)[0] == 10


# --- Learner -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_learner_recalibrates_only_after_enough_closed_trades(monkeypatch):
    await init_db()
    async with AsyncSessionLocal() as session:
        latest = (await session.execute(select(func.max(Trade.executed_at)).where(Trade.order_type == "SELL"))).scalar()
    start = max(latest or datetime.datetime.utcnow(), datetime.datetime.utcnow())
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "LAST_CALIBRATION_TIMESTAMP"))
        cfg = res.scalars().first()
        previous = cfg.value if cfg else None
        if cfg:
            cfg.value = start.isoformat()
        else:
            session.add(SystemConfig(key="LAST_CALIBRATION_TIMESTAMP", value=start.isoformat()))
        for i in range(2):
            session.add(Trade(
                ticker=_unique(f"GATE{i}"), name="Gate", order_type="SELL", shares=1.0, currency="EUR",
                price_native=95.0, price_eur=95.0, total_eur=95.0, realized_pnl_eur=-5.0, realized_pnl_pct=-5.0,
                rationale="Stop-Loss triggered", executed_at=start + datetime.timedelta(seconds=i + 1),
            ))
        await session.commit()

    monkeypatch.setattr(settings, "LEARNER_MIN_TRADES", 3)
    with patch("src.trading.learner.llm_client.has_active_key", return_value=False):
        skipped = await trade_learner.calibrate_internal_parameters(automatic=True)
        assert skipped["success"] is False and skipped["skipped"] is True
        assert skipped["adjustments"] == []
        # Manual calibration is not gated
        manual = await trade_learner.calibrate_internal_parameters()
        assert manual["success"] is True

    if previous is not None:
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(SystemConfig).where(SystemConfig.key == "LAST_CALIBRATION_TIMESTAMP"))
            res.scalars().first().value = previous
            await session.commit()
