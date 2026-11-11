"""The bot never tries to buy an instrument Trading 212's API cannot buy (CFD-only or not listed on Invest)."""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import AnalysisReport, OrderTrigger, Position
from src.db.session import AsyncSessionLocal, init_db
from src.trading import trading212
from src.trading.paper import paper_broker
from src.trading.trigger_gate import precheck_manual_trigger

INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "currencyCode": "USD", "type": "STOCK", "exchange": "NASDAQ"},
    {"ticker": "VWCEd_EQ", "shortName": "VWCE", "currencyCode": "EUR", "type": "ETF", "exchange": "Deutsche Börse Xetra"},
    {"ticker": "CFDO_US", "shortName": "CFDO", "currencyCode": "USD", "type": "CFD", "exchange": "NASDAQ"},
    {"ticker": "TWINa_EQ", "shortName": "TWIN", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Paris"},
    {"ticker": "TWINb_EQ", "shortName": "TWIN", "currencyCode": "EUR", "type": "STOCK", "exchange": "Euronext Paris"},
]
BLOCKED = "CFDX"  # not listed at all


class StubClient(trading212.Trading212Client):
    def __init__(self, instruments=INSTRUMENTS):
        super().__init__("k", "s")
        self.listed = instruments
        self.calls = 0

    async def instruments(self):
        self.calls += 1
        return self.listed


@pytest.fixture
def t212(monkeypatch):
    client = StubClient()
    monkeypatch.setattr(trading212, "get_client", lambda: client)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    return client


def _stock(ticker, currency="USD", price=100.0):
    return {"ticker": ticker, "name": ticker, "price": price, "currency": currency, "quote_type": "EQUITY",
            "technicals": {"rsi14": 55, "volume_surge_ratio": 1.2, "atr14": 2.0}, "fundamentals": {}}


def _report(ticker, action="HOLD", score=50):
    return {
        "ticker": ticker, "action": action, "consensus_score": score, "entry_trigger_price": 98.0,
        "trigger_type": "LIMIT_PULLBACK", "trigger_rationale": "r", "summary": "s", "debate_transcript": "d",
        "technical": {"analysis_summary": "t"}, "fundamental": {"analysis_summary": "f"},
        "sentiment": {"sentiment_summary": "s"},
        "risk": {"approved": True, "take_profit_price": 110.0, "stop_loss_price": 95.0, "recommended_size_pct": 0.05,
                 "risk_summary": "ok"},
    }


async def _rows(model, ticker):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(model).where(model.ticker == ticker))).scalars().all()


# --- The helper ---------------------------------------------------------------------------------

async def test_stock_and_etf_match_can_be_bought(t212):
    assert await trading212.broker_can_buy("AAPL", "USD") == (True, "")
    assert await trading212.broker_can_buy("VWCE.DE", "EUR") == (True, "")


async def test_cfd_only_and_unlisted_cannot_be_bought(t212, caplog):
    with caplog.at_level(logging.INFO):
        ok, why = await trading212.broker_can_buy("CFDO", "USD")
        assert ok is False and why == "CFDO is not offered on Trading 212 Invest (CFD-only or not listed), so the API cannot buy it."
        ok, why = await trading212.broker_can_buy(BLOCKED, "USD")
        assert ok is False and BLOCKED in why and "not offered" in why
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_two_matches_are_ambiguous(t212):
    ok, why = await trading212.broker_can_buy("TWIN.PA", "EUR")
    assert ok is False and "ambiguous" in why


async def test_instrument_list_outage_fails_open_once_per_retry_window(monkeypatch, caplog):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503, text="down")
    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(trading212, "get_client", lambda: client)
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    with caplog.at_level(logging.WARNING):
        assert await trading212.broker_can_buy(BLOCKED, "USD") == (True, "")
        assert len(requests) == 1
        for _ in range(10):
            assert await trading212.broker_can_buy(BLOCKED, "USD") == (True, "")
    assert len(requests) == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    # Other callers still get the error, not an empty list
    with pytest.raises(trading212.Trading212Error):
        await client.instruments()


async def test_empty_instrument_list_fails_open(monkeypatch):
    monkeypatch.setattr(trading212, "get_client", lambda: StubClient([]))
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    assert await trading212.broker_can_buy(BLOCKED, "USD") == (True, "")


async def test_alpaca_or_no_client_is_never_checked(t212, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_BROKER", "alpaca")
    assert await trading212.broker_can_buy(BLOCKED, "USD") == (True, "")
    assert t212.calls == 0
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(trading212, "get_client", lambda: None)
    assert await trading212.broker_can_buy(BLOCKED, "USD") == (True, "")


# --- Automated paths ----------------------------------------------------------------------------

async def test_session_skips_a_ticker_the_broker_cannot_buy(t212):
    from src.scheduler.jobs import trading_scheduler
    await init_db()
    cands = [{"ticker": BLOCKED, "pre_score": 85}, {"ticker": "AAPL", "pre_score": 80}]
    committee = AsyncMock(side_effect=lambda data, total: _report(data["ticker"]))
    follow = AsyncMock()
    try:
        with patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
             patch("src.scheduler.jobs.market_scanner.scan_universe", return_value=cands), \
             patch("src.scheduler.jobs.market_data_provider.get_stock_data", side_effect=_stock), \
             patch("src.scheduler.jobs.investment_committee.evaluate_ticker", committee), \
             patch("src.scheduler.jobs.shadow_portfolios.follow_report", follow), \
             patch("src.scheduler.jobs.review_tracker.compare_past_week_reviews", AsyncMock(return_value={})), \
             patch("src.scheduler.jobs.review_tracker.save_session_review", AsyncMock()), \
             patch("src.scheduler.jobs.telegram_bot.notify_market_open", AsyncMock(return_value="")), \
             patch("src.scheduler.jobs.telegram_bot.notify_trigger_armed", AsyncMock()):
            await trading_scheduler.run_market_session(market="US", session="OPEN")
        assert [c.args[0]["ticker"] for c in committee.await_args_list] == ["AAPL"]
        assert [c.args[1]["ticker"] for c in follow.await_args_list] == ["AAPL"]
        assert await _rows(OrderTrigger, BLOCKED) == [] and await _rows(Position, BLOCKED) == []
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(AnalysisReport).where(AnalysisReport.ticker.in_([BLOCKED, "AAPL"])))
            await s.commit()


async def test_opportunity_scan_skips_a_ticker_the_broker_cannot_buy(t212):
    from src.scheduler.jobs import trading_scheduler
    await init_db()
    cands = [{"ticker": BLOCKED}, {"ticker": "AAPL"}]
    committee = AsyncMock(side_effect=lambda data, total: _report(data["ticker"]))
    follow = AsyncMock()
    status = {"eu_market": {"is_open": False}, "us_market": {"is_open": True}}
    portfolio = {"available_cash_eur": 400.0, "total_value_eur": 500.0, "open_positions_count": 0}
    with patch("src.data.market_hours.get_market_statuses", return_value=status), \
         patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
         patch("src.scheduler.jobs.market_scanner.scan_universe", MagicMock(return_value=cands)), \
         patch("src.scheduler.jobs.market_data_provider.get_stock_data", side_effect=_stock), \
         patch("src.scheduler.jobs.investment_committee.cached_report", return_value=None), \
         patch("src.scheduler.jobs.investment_committee.evaluate_ticker", committee), \
         patch("src.scheduler.jobs.shadow_portfolios.follow_report", follow), \
         patch.object(paper_broker, "get_portfolio_summary", AsyncMock(return_value=portfolio)):
        await trading_scheduler.scan_market_opportunities()
    assert [c.args[0]["ticker"] for c in committee.await_args_list] == ["AAPL"]
    assert [c.args[1]["ticker"] for c in follow.await_args_list] == ["AAPL"]
    assert await _rows(OrderTrigger, BLOCKED) == [] and await _rows(Position, BLOCKED) == []


async def test_redebate_cancels_a_pending_trigger_the_broker_cannot_buy(t212):
    from src.scheduler.jobs import trading_scheduler
    await init_db()
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=BLOCKED, name=BLOCKED, currency="USD", trigger_type="LIMIT_PULLBACK",
                           target_price=98.0, current_price_at_creation=100.0, stop_loss=95.0, take_profit=110.0,
                           allocation_eur=500.0, consensus_score=86, status="PENDING", is_live=False))
        await s.commit()
    committee = AsyncMock(side_effect=lambda data, total: _report(data["ticker"], "BUY", 90))
    try:
        with patch("src.scheduler.jobs.market_data_provider.get_stock_data", side_effect=_stock), \
             patch("src.scheduler.jobs.investment_committee.evaluate_ticker", committee), \
             patch("src.scheduler.jobs.telegram_bot.send_message", AsyncMock()):
            result = await trading_scheduler.refresh_pending_triggers()
        (trig,) = await _rows(OrderTrigger, BLOCKED)
        assert trig.status == "CANCELLED"
        assert "not offered on Trading 212" in (trig.cancel_reason or "")
        assert any(BLOCKED in c for c in result["cancelled"])
        assert committee.await_count == 0
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == BLOCKED))
            await s.commit()


# --- Manual paths -------------------------------------------------------------------------------

async def test_manual_trigger_precheck_blocks_hard(t212):
    await init_db()
    res = await precheck_manual_trigger(BLOCKED, _stock(BLOCKED), 1000.0)
    assert res.code == "NOT_ON_BROKER" and res.allowed is False and res.overridable is False
    assert "not offered on Trading 212" in res.message
    assert await precheck_manual_trigger("AAPL", _stock("AAPL"), 1000.0) is None


async def test_manual_trigger_precheck_reports_in_use_before_broker(t212):
    """A pending trigger is the more useful answer, and needs no broker lookup."""
    await init_db()
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=BLOCKED, name=BLOCKED, currency="USD", trigger_type="LIMIT_PULLBACK",
                           target_price=98.0, current_price_at_creation=100.0, stop_loss=95.0, take_profit=110.0,
                           allocation_eur=500.0, consensus_score=86, status="PENDING", is_live=False))
        await s.commit()
    try:
        res = await precheck_manual_trigger(BLOCKED, _stock(BLOCKED), 1000.0)
        assert res.code == "TRIGGER_PENDING"
        assert t212.calls == 0
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == BLOCKED))
            await s.commit()


async def test_manual_dashboard_buy_is_refused_before_committee_and_broker(t212):
    from src.web.app import app
    committee, buy = AsyncMock(), AsyncMock()
    with patch("src.web.routes.check_market_hours", return_value=(True, "")), \
         patch("src.web.routes.market_data_provider.get_stock_data", side_effect=_stock), \
         patch("src.web.routes.investment_committee.evaluate_ticker", committee), \
         patch.object(paper_broker, "execute_buy", buy):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/trade/buy", json={"ticker": BLOCKED})
    assert resp.status_code == 400 and "not offered on Trading 212" in resp.json()["detail"]
    committee.assert_not_awaited()
    buy.assert_not_awaited()


async def test_manual_telegram_buy_is_refused_before_committee_and_broker(t212, monkeypatch):
    from src.telegram.bot import TelegramAssistant
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setattr(settings, "ENFORCE_MARKET_HOURS", False)
    bot = TelegramAssistant()
    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append(txt))
    committee, buy = AsyncMock(), AsyncMock()
    with patch("src.telegram.bot.market_data_provider.get_stock_data", side_effect=_stock), \
         patch("src.telegram.bot.investment_committee.evaluate_ticker", committee), \
         patch.object(paper_broker, "execute_buy", buy):
        await bot._handle_update({"message": {"chat": {"id": 12345}, "text": f"/buy {BLOCKED} 500 force"}})
    assert "not offered on Trading 212" in replies[-1]
    committee.assert_not_awaited()
    buy.assert_not_awaited()


async def test_paper_mode_with_trading212_keys_blocks_too(monkeypatch):
    """No patched get_client: the real one, built from the configured keys, while trading on paper."""
    monkeypatch.setattr(settings, "TRADING_MODE", "PAPER")
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")
    monkeypatch.setattr(settings, "TRADING212_API_KEY", "key")
    monkeypatch.setattr(settings, "TRADING212_API_SECRET", "secret")
    monkeypatch.setattr(trading212, "_client", None)
    monkeypatch.setattr(trading212.Trading212Client, "instruments", AsyncMock(return_value=INSTRUMENTS))
    await init_db()
    res = await precheck_manual_trigger(BLOCKED, _stock(BLOCKED), 1000.0)
    assert res.code == "NOT_ON_BROKER"
