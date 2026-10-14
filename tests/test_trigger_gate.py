"""Validation of manually armed triggers: the gate rules, the ticker prechecks and both trigger endpoints."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, delete

from config.settings import settings
from src.db.models import OrderTrigger, Position, SystemConfig
from src.db.session import init_db, AsyncSessionLocal
from src.scheduler.jobs import TradingScheduler
from src.trading.paper import paper_broker
from src.trading.trigger_gate import (
    TriggerGateResult, normalize_ticker, is_tradable_quote_type, get_trigger_min_score,
    evaluate_trigger_verdict, evaluate_manual_trigger, precheck_manual_trigger
)
from src.web.app import app

ENDPOINTS = ["/api/triggers/from-ai", "/api/triggers"]
REGIME_BLOCK = (False, "Market regime filter: ^GSPC (4000) is below its 200-day average (4200); no new entries in a falling market.")


def _report(action="BUY", score=86, approved=True, summary="Strong conviction", entry=98.0, stop=95.0, target=110.0):
    return {
        "action": action,
        "consensus_score": score,
        "entry_trigger_price": entry,
        "trigger_type": "LIMIT_PULLBACK",
        "trigger_rationale": "Pullback to support",
        "summary": summary,
        "risk": {
            "approved": approved,
            "take_profit_price": target,
            "stop_loss_price": stop,
            "recommended_size_pct": 0.05,
            "risk_summary": "Position size too large for the volatility" if not approved else "Managed risk",
        },
    }


def _stock(ticker, price=100.0, currency="EUR", quote_type="EQUITY"):
    return {
        "ticker": ticker,
        "name": f"Name {ticker}",
        "price": price,
        "currency": currency,
        "quote_type": quote_type,
        "technicals": {"rsi14": 55, "volume_surge_ratio": 1.2, "atr14": 2.0},
        "fundamentals": {},
    }


@pytest.fixture(autouse=True)
async def _clean_gate_rows():
    """Every ticker these tests use starts with GATE; remove their rows before and after each test."""
    async def _wipe():
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.like("GATE%")))
            await session.execute(delete(Position).where(Position.ticker.like("GATE%")))
            await session.commit()
    await init_db()
    await _wipe()
    yield
    await _wipe()


async def _insert_position(ticker, is_live=False):
    async with AsyncSessionLocal() as session:
        session.add(Position(
            ticker=ticker, name=ticker, currency="EUR", shares=1.0, entry_price_native=100.0, current_price_native=100.0,
            highest_price_native=100.0, entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=100.0,
            status="OPEN", is_live=is_live
        ))
        await session.commit()


async def _insert_trigger(ticker, is_live=False):
    async with AsyncSessionLocal() as session:
        session.add(OrderTrigger(
            ticker=ticker, name=ticker, currency="EUR", trigger_type="LIMIT_PULLBACK", target_price=95.0,
            current_price_at_creation=100.0, allocation_eur=100.0, status="PENDING", is_live=is_live
        ))
        await session.commit()


async def _pending(ticker):
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(OrderTrigger).where(OrderTrigger.ticker == ticker, OrderTrigger.status == "PENDING"))
        return res.scalars().all()


# --- Unit tests: ticker and quote type -----------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("aapl", "AAPL"),
    ("  nvda ", "NVDA"),
    ("$tsla", "TSLA"),
    ("BRK-B", "BRK-B"),
    ("BRK.B", "BRK.B"),
    ("asml.as", "ASML.AS"),
    ("0700.HK", "0700.HK"),
    ("A", "A"),
    ("A" * 15, "A" * 15),
])
def test_normalize_ticker_accepts_stock_symbols(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", [
    "^GSPC", "^STOXX50E", "EURUSD=X", "", "   ", "$", "AAPL MSFT", "-AAPL", ".AAPL", "AA/PL", "AAPL!", "A" * 16,
    "what is this", None, 123,
])
def test_normalize_ticker_rejects_everything_else(raw):
    assert normalize_ticker(raw) is None


@pytest.mark.parametrize("quote_type", [None, "", "EQUITY", "ETF", "equity", "etf", "SOMETHING_NEW"])
def test_tradable_quote_types(quote_type):
    assert is_tradable_quote_type(quote_type)


@pytest.mark.parametrize("quote_type", ["INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND", "OPTION", "index", " Currency "])
def test_non_tradable_quote_types(quote_type):
    assert not is_tradable_quote_type(quote_type)


# --- Market data: quote type ---------------------------------------------------------------

class _FakeYahooTicker:
    def __init__(self, info):
        import pandas as pd
        self.info = info
        self.fast_info = {"lastPrice": 100.0, "currency": "EUR"}
        closes = [90.0 + i * 0.2 for i in range(60)]
        self._history = pd.DataFrame({
            "Close": closes, "High": [c + 1 for c in closes], "Low": [c - 1 for c in closes], "Volume": [1000.0] * 60
        })

    def history(self, period="1y"):
        return self._history


@pytest.mark.parametrize("info, expected", [({"quoteType": "INDEX"}, "INDEX"), ({"quoteType": "EQUITY"}, "EQUITY"), ({}, None)])
def test_market_data_carries_the_quote_type(info, expected):
    from src.data import market
    symbol = f"GATEQT{len(market._stock_cache)}"
    try:
        with patch("src.data.market.yf.Ticker", return_value=_FakeYahooTicker(info)):
            data = market.market_data_provider.get_stock_data(symbol)
        assert data is not None and data["quote_type"] == expected
        assert is_tradable_quote_type(data["quote_type"]) is (expected != "INDEX")
    finally:
        market._stock_cache.pop(symbol, None)


# --- Unit tests: verdict rules ---------------------------------------------------------

def test_approved_buy_at_or_above_threshold_is_allowed():
    for action in ("BUY", "STRONG_BUY"):
        res = evaluate_trigger_verdict(_report(action=action, score=80), 80)
        assert res.allowed and res.code == "OK" and not res.overridable
        assert (res.action, res.consensus_score, res.trigger_min_score) == (action, 80, 80)


def test_risk_veto_is_a_hard_block_and_comes_first():
    # Even a high-scoring BUY, and a SELL, report the veto first; force changes nothing
    for action in ("BUY", "SELL"):
        for force in (False, True):
            res = evaluate_trigger_verdict(_report(action=action, score=95, approved=False), 80, force=force)
            assert not res.allowed and res.code == "RISK_VETO" and not res.overridable
    assert "Position size too large" in evaluate_trigger_verdict(_report(approved=False), 80).message


@pytest.mark.parametrize("action", ["SELL", "STRONG_SELL", "sell"])
def test_sell_verdict_is_a_hard_block(action):
    for force in (False, True):
        res = evaluate_trigger_verdict(_report(action=action, score=90), 80, force=force)
        assert not res.allowed and res.code == "SELL_VERDICT" and not res.overridable
    assert "SELL" in evaluate_trigger_verdict(_report(action=action), 80).message.upper()


@pytest.mark.parametrize("action", ["HOLD", "WATCH", "", None])
def test_hold_is_a_soft_block(action):
    report = _report(score=62)
    report["action"] = action
    res = evaluate_trigger_verdict(report, 80)
    assert not res.allowed and res.code == "NOT_BUY_VERDICT" and res.overridable
    assert "62/100" in res.message
    if action:
        assert action in res.message


def test_score_below_threshold_is_a_soft_block():
    res = evaluate_trigger_verdict(_report(action="BUY", score=79), 80)
    assert not res.allowed and res.code == "LOW_SCORE" and res.overridable
    assert "79/100" in res.message and "80" in res.message and "BUY" in res.message
    assert (res.action, res.consensus_score, res.trigger_min_score) == ("BUY", 79, 80)


def test_hold_with_a_low_score_reports_the_verdict_first():
    res = evaluate_trigger_verdict(_report(action="HOLD", score=30), 80)
    assert res.code == "NOT_BUY_VERDICT"


def test_force_turns_soft_blocks_into_an_allowed_result():
    for report in (_report(action="HOLD", score=62), _report(action="BUY", score=70), _report(action="HOLD", score=30)):
        res = evaluate_trigger_verdict(report, 80, force=True)
        assert res.allowed and res.code == "OK_FORCED" and not res.overridable
        assert "against" in res.message.lower()
        assert res.action == report["action"] and res.consensus_score == report["consensus_score"]
    assert "HOLD" in evaluate_trigger_verdict(_report(action="HOLD", score=62), 80, force=True).message


def test_force_does_not_change_a_clean_pass():
    res = evaluate_trigger_verdict(_report(), 80, force=True)
    assert res.allowed and res.code == "OK"


def test_gate_result_detail_is_json_safe_and_complete():
    res = evaluate_trigger_verdict(_report(action="HOLD", score=62), 80)
    detail = res.to_detail()
    assert json.loads(json.dumps(detail)) == detail
    assert set(detail) == {"allowed", "code", "message", "overridable", "action", "consensus_score", "trigger_min_score"}
    assert detail["allowed"] is False and detail["overridable"] is True
    bare = TriggerGateResult(False, "NO_MARKET_DATA", "x").to_detail()
    assert bare["action"] is None and bare["consensus_score"] is None and bare["overridable"] is False


@pytest.mark.parametrize("action", ["BUY", "STRONG_BUY", "HOLD", "SELL"])
@pytest.mark.parametrize("score", [30, 79, 80, 95])
@pytest.mark.parametrize("approved", [True, False])
def test_scheduler_arming_decision_matches_the_gate(action, score, approved):
    report = _report(action=action, score=score, approved=approved)
    expected = (
        score >= 80 and action in ("BUY", "STRONG_BUY") and approved
    )
    assert TradingScheduler._should_arm_trigger(report, 80) is expected
    assert evaluate_trigger_verdict(report, 80).allowed is expected


# --- Unit tests: whole evaluation (rules 1-6) -----------------------------------------

@pytest.mark.asyncio
async def test_manual_trigger_passes_with_a_buy_a_healthy_market_and_an_affordable_share():
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")) as regime:
        res = await evaluate_manual_trigger("GATEOK", _stock("GATEOK"), _report(), 98.0, 500.0, 80)
    assert res.allowed and res.code == "OK"
    regime.assert_called_once_with("GATEOK", "EUR")


@pytest.mark.asyncio
async def test_verdict_hard_blocks_skip_the_market_lookups():
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")) as regime:
        for report, code in ((_report(action="SELL"), "SELL_VERDICT"), (_report(approved=False), "RISK_VETO")):
            res = await evaluate_manual_trigger("GATEX", _stock("GATEX"), report, 98.0, 500.0, 80, force=True)
            assert not res.allowed and res.code == code and not res.overridable
    regime.assert_not_called()


@pytest.mark.asyncio
async def test_soft_block_is_overridable_only_with_force():
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")):
        blocked = await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(action="HOLD", score=62), 98.0, 500.0, 80)
        forced = await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(action="HOLD", score=62), 98.0, 500.0, 80, force=True)
    assert not blocked.allowed and blocked.code == "NOT_BUY_VERDICT" and blocked.overridable
    assert forced.allowed and forced.code == "OK_FORCED"


@pytest.mark.asyncio
async def test_falling_market_is_a_hard_block_even_with_force():
    with patch("src.trading.trigger_gate.check_market_regime", return_value=REGIME_BLOCK):
        for force in (False, True):
            res = await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(), 98.0, 500.0, 80, force=force)
            assert not res.allowed and res.code == "MARKET_REGIME" and not res.overridable
            assert "^GSPC" in res.message
            assert res.action == "BUY" and res.consensus_score == 86


@pytest.mark.asyncio
async def test_hard_block_wins_over_a_soft_block_so_overridable_is_truthful():
    with patch("src.trading.trigger_gate.check_market_regime", return_value=REGIME_BLOCK):
        res = await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(action="HOLD", score=62), 98.0, 500.0, 80)
    assert res.code == "MARKET_REGIME" and not res.overridable


@pytest.mark.asyncio
async def test_one_share_must_fit_the_allocation(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")):
        for force in (False, True):
            res = await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(), 5000.0, 1000.0, 80, force=force)
            assert not res.allowed and res.code == "SHARE_TOO_EXPENSIVE" and not res.overridable
            assert "5000.00" in res.message and "1000.00" in res.message
        # A share that fits, and the rule switched off
        assert (await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(), 260.0, 1000.0, 80)).allowed
        monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", False)
        assert (await evaluate_manual_trigger("GATEX", _stock("GATEX"), _report(), 5000.0, 1000.0, 80)).allowed


@pytest.mark.asyncio
async def test_share_price_is_converted_to_euro(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")), \
         patch("src.trading.trigger_gate.currency_service.to_eur", return_value=2000.0) as to_eur:
        res = await evaluate_manual_trigger("GATEX", _stock("GATEX", currency="USD"), _report(), 2160.0, 1000.0, 80)
    to_eur.assert_called_once_with(2160.0, "USD")
    assert not res.allowed and res.code == "SHARE_TOO_EXPENSIVE"
    assert "2000.00" in res.message


# --- Unit tests: trigger threshold and prechecks ---------------------------------------

@pytest.mark.asyncio
async def test_trigger_min_score_prefers_the_saved_setting(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", 77)

    async def set_saved(value):
        async with AsyncSessionLocal() as session:
            await session.execute(delete(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
            if value is not None:
                session.add(SystemConfig(key="TRIGGER_MIN_SCORE", value=value))
            await session.commit()

    async with AsyncSessionLocal() as session:
        res = await session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
        original = res.scalars().first()
        original_value = original.value if original else None
    try:
        await set_saved(None)
        assert await get_trigger_min_score() == 77
        await set_saved("85")
        assert await get_trigger_min_score() == 85
        await set_saved("not a number")
        assert await get_trigger_min_score() == 77
        await set_saved("")
        assert await get_trigger_min_score() == 77
    finally:
        await set_saved(original_value)


@pytest.mark.asyncio
async def test_precheck_rejects_missing_data_and_untradable_instruments():
    res = await precheck_manual_trigger("GATENODATA", None, 5000.0)
    assert res.code == "NO_MARKET_DATA" and not res.allowed and not res.overridable

    for quote_type in ("INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND", "OPTION"):
        res = await precheck_manual_trigger("GATEX", _stock("GATEX", quote_type=quote_type), 5000.0)
        assert res.code == "NOT_TRADABLE" and not res.overridable
        assert quote_type in res.message

    for quote_type in ("EQUITY", "ETF", None):
        assert await precheck_manual_trigger("GATEX", _stock("GATEX", quote_type=quote_type), 5000.0) is None


@pytest.mark.asyncio
async def test_precheck_rejects_an_open_position_and_a_pending_trigger():
    await _insert_position("GATEPOS")
    res = await precheck_manual_trigger("GATEPOS", _stock("GATEPOS"), 5000.0)
    assert res.code == "POSITION_OPEN" and not res.overridable and "GATEPOS" in res.message

    await _insert_trigger("GATEPEND")
    res = await precheck_manual_trigger("GATEPEND", _stock("GATEPEND"), 5000.0)
    assert res.code == "TRIGGER_PENDING" and not res.overridable and "GATEPEND" in res.message

    assert await precheck_manual_trigger("GATEFREE", _stock("GATEFREE"), 5000.0) is None


@pytest.mark.asyncio
async def test_precheck_ignores_closed_positions_and_finished_triggers():
    await _insert_position("GATEOLD")
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Position).where(Position.ticker == "GATEOLD"))
        res.scalars().first().status = "CLOSED"
        session.add(OrderTrigger(
            ticker="GATEOLD", currency="EUR", trigger_type="LIMIT_PULLBACK", target_price=95.0,
            current_price_at_creation=100.0, allocation_eur=100.0, status="CANCELLED", is_live=False
        ))
        await session.commit()
    assert await precheck_manual_trigger("GATEOLD", _stock("GATEOLD"), 5000.0) is None


@pytest.mark.asyncio
async def test_precheck_keeps_paper_and_live_triggers_apart(monkeypatch):
    await _insert_trigger("GATEPAPER", is_live=False)
    await _insert_trigger("GATELIVE", is_live=True)

    monkeypatch.setattr(settings, "TRADING_MODE", "PAPER")
    assert (await precheck_manual_trigger("GATEPAPER", _stock("GATEPAPER"))).code == "TRIGGER_PENDING"
    assert await precheck_manual_trigger("GATELIVE", _stock("GATELIVE")) is None

    monkeypatch.setattr(settings, "TRADING_MODE", "LIVE")
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    assert (await precheck_manual_trigger("GATELIVE", _stock("GATELIVE"))).code == "TRIGGER_PENDING"
    assert await precheck_manual_trigger("GATEPAPER", _stock("GATEPAPER")) is None


@pytest.mark.asyncio
async def test_precheck_requires_twenty_euro_of_available_cash():
    res = await precheck_manual_trigger("GATEX", _stock("GATEX"), 19.99)
    assert res.code == "INSUFFICIENT_CASH" and not res.overridable
    assert "19.99" in res.message
    assert await precheck_manual_trigger("GATEX", _stock("GATEX"), 20.0) is None
    # Without a cash figure the check is skipped
    assert await precheck_manual_trigger("GATEX", _stock("GATEX")) is None


@pytest.mark.asyncio
async def test_precheck_order_is_data_then_type_then_position_then_cash():
    await _insert_position("GATEORD")
    index = _stock("GATEORD", quote_type="INDEX")
    assert (await precheck_manual_trigger("GATEORD", None, 0.0)).code == "NO_MARKET_DATA"
    assert (await precheck_manual_trigger("GATEORD", index, 0.0)).code == "NOT_TRADABLE"
    assert (await precheck_manual_trigger("GATEORD", _stock("GATEORD"), 0.0)).code == "POSITION_OPEN"
    assert (await precheck_manual_trigger("GATEFREE", _stock("GATEFREE"), 0.0)).code == "INSUFFICIENT_CASH"


# --- API tests --------------------------------------------------------------------------

@contextmanager
def _api_env(data, report, cash=5000.0, min_score=80, regime=(True, "")):
    """Mocks market data, the committee, the portfolio and the market regime; the paper broker and DB are real."""
    portfolio = {"available_cash_eur": cash, "total_value_eur": 10000.0, "open_positions_count": 0}
    env = SimpleNamespace(committee=AsyncMock(return_value=report), stock=MagicMock(return_value=data))
    with patch("src.web.routes.market_data_provider.get_stock_data", env.stock), \
         patch("src.web.routes.investment_committee.evaluate_ticker", env.committee), \
         patch.object(paper_broker, "get_portfolio_summary", AsyncMock(return_value=portfolio)), \
         patch("src.web.routes.get_trigger_min_score", AsyncMock(return_value=min_score)), \
         patch("src.trading.trigger_gate.check_market_regime", return_value=regime), \
         patch("src.telegram.bot.telegram_bot.notify_trigger_armed", AsyncMock()):
        yield env


async def _post(path, **body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_buy_verdict_arms_the_trigger_and_reports_the_gate(path):
    with _api_env(_stock("GATEBUY"), _report()):
        resp = await _post(path, ticker="GATEBUY", allocation_eur=500.0)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["ticker"] == "GATEBUY" and body["target_price"] == 98.0
    assert body["gate"]["code"] == "OK" and body["gate"]["allowed"] is True
    triggers = await _pending("GATEBUY")
    assert len(triggers) == 1 and triggers[0].consensus_score == 86


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_ticker_is_normalized_before_use(path):
    with _api_env(_stock("GATENORM"), _report()) as env:
        resp = await _post(path, ticker="  $gatenorm ", allocation_eur=500.0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ticker"] == "GATENORM"
    env.stock.assert_called_once_with("GATENORM")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_hold_verdict_is_blocked_but_can_be_forced(path):
    report = _report(action="HOLD", score=62, summary="Wait for a better entry")
    with _api_env(_stock("GATEHOLD"), report):
        resp = await _post(path, ticker="GATEHOLD", allocation_eur=500.0)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "NOT_BUY_VERDICT" and detail["overridable"] is True and detail["allowed"] is False
    assert detail["action"] == "HOLD" and detail["consensus_score"] == 62 and detail["trigger_min_score"] == 80
    assert "HOLD" in detail["message"]
    assert detail["summary"] == "Wait for a better entry"
    assert await _pending("GATEHOLD") == []

    with _api_env(_stock("GATEHOLD"), report):
        forced = await _post(path, ticker="GATEHOLD", allocation_eur=500.0, force=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["gate"]["code"] == "OK_FORCED"
    assert len(await _pending("GATEHOLD")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_low_score_is_blocked_but_can_be_forced(path):
    report = _report(action="BUY", score=70)
    with _api_env(_stock("GATELOW"), report):
        resp = await _post(path, ticker="GATELOW", allocation_eur=500.0)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "LOW_SCORE" and detail["overridable"] is True
    assert await _pending("GATELOW") == []

    with _api_env(_stock("GATELOW"), report):
        forced = await _post(path, ticker="GATELOW", allocation_eur=500.0, force=True)
    assert forced.status_code == 200, forced.text
    assert forced.json()["gate"]["code"] == "OK_FORCED"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_threshold_comes_from_the_saved_setting(path):
    with _api_env(_stock("GATETHR"), _report(action="BUY", score=70), min_score=65):
        resp = await _post(path, ticker="GATETHR", allocation_eur=500.0)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("report, code", [
    (_report(action="SELL", score=20), "SELL_VERDICT"),
    (_report(action="STRONG_SELL", score=10), "SELL_VERDICT"),
    (_report(action="HOLD", score=60, approved=False), "RISK_VETO"),
    (_report(action="BUY", score=90, approved=False), "RISK_VETO"),
])
async def test_hard_verdict_blocks_are_not_overridable(path, report, code):
    for force in (False, True):
        with _api_env(_stock("GATEHARD"), report):
            resp = await _post(path, ticker="GATEHARD", allocation_eur=500.0, force=force)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == code and detail["overridable"] is False
        assert "summary" in detail
    assert await _pending("GATEHARD") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_falling_market_blocks_even_a_forced_trigger(path):
    for report in (_report(), _report(action="HOLD", score=62)):
        for force in (False, True):
            with _api_env(_stock("GATEREG"), report, regime=REGIME_BLOCK):
                resp = await _post(path, ticker="GATEREG", allocation_eur=500.0, force=force)
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert detail["code"] == "MARKET_REGIME" and detail["overridable"] is False
            assert "falling market" in detail["message"]
    assert await _pending("GATEREG") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_share_costing_more_than_the_allocation_blocks_even_a_forced_trigger(path, monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    expensive = _report(entry=4900.0, stop=4700.0, target=5300.0)
    for force in (False, True):
        with _api_env(_stock("GATESHARE", price=5000.0), expensive):
            resp = await _post(path, ticker="GATESHARE", allocation_eur=1000.0, force=force)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "SHARE_TOO_EXPENSIVE" and detail["overridable"] is False
    assert await _pending("GATESHARE") == []


@pytest.mark.asyncio
async def test_manual_trigger_checks_the_share_at_the_price_the_user_chose(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    with _api_env(_stock("GATEMAN"), _report()):
        resp = await _post(
            "/api/triggers", ticker="GATEMAN", allocation_eur=500.0,
            trigger_type="BREAKOUT_BUY", target_price=5000.0, stop_loss=4800.0, take_profit=5600.0
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "SHARE_TOO_EXPENSIVE"


@pytest.mark.asyncio
async def test_manual_trigger_keeps_the_levels_the_user_gave():
    with _api_env(_stock("GATELEVEL"), _report()):
        resp = await _post(
            "/api/triggers", ticker="GATELEVEL", allocation_eur=500.0,
            target_price=97.0, stop_loss=93.0, take_profit=120.0
        )
    assert resp.status_code == 200, resp.text
    trigger = (await _pending("GATELEVEL"))[0]
    assert (trigger.target_price, trigger.stop_loss, trigger.take_profit) == (97.0, 93.0, 120.0)


@pytest.mark.asyncio
async def test_summary_in_a_blocked_response_is_truncated():
    with _api_env(_stock("GATESUM"), _report(action="HOLD", score=50, summary="x" * 1000)):
        resp = await _post("/api/triggers/from-ai", ticker="GATESUM", allocation_eur=500.0)
    assert resp.status_code == 422
    assert resp.json()["detail"]["summary"] == "x" * 400


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("raw", ["^GSPC", "EURUSD=X", "not a ticker", "", "   ", "AAPL; DROP"])
async def test_invalid_ticker_is_rejected_before_any_lookup(path, raw):
    with _api_env(_stock("GATEBAD"), _report()) as env:
        resp = await _post(path, ticker=raw, allocation_eur=500.0)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "INVALID_TICKER" and detail["overridable"] is False and detail["message"]
    env.stock.assert_not_called()
    env.committee.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_missing_market_data_is_a_404_without_the_committee(path):
    with _api_env(None, _report()) as env:
        resp = await _post(path, ticker="GATENODATA", allocation_eur=500.0)
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "NO_MARKET_DATA"
    env.committee.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("quote_type", ["INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND", "OPTION"])
async def test_untradable_instrument_is_rejected_without_the_committee(path, quote_type):
    with _api_env(_stock("GATEIDX", quote_type=quote_type), _report()) as env:
        resp = await _post(path, ticker="GATEIDX", allocation_eur=500.0, force=True)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "NOT_TRADABLE" and detail["overridable"] is False
    env.committee.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_open_position_is_rejected_before_the_committee(path):
    await _insert_position("GATEHELD")
    with _api_env(_stock("GATEHELD"), _report()) as env:
        resp = await _post(path, ticker="GATEHELD", allocation_eur=500.0, force=True)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "POSITION_OPEN"
    env.committee.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_pending_trigger_is_rejected_before_the_committee(path):
    await _insert_trigger("GATEWAIT")
    with _api_env(_stock("GATEWAIT"), _report()) as env:
        resp = await _post(path, ticker="GATEWAIT", allocation_eur=500.0, force=True)
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "TRIGGER_PENDING"
    env.committee.assert_not_awaited()
    assert len(await _pending("GATEWAIT")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ENDPOINTS)
async def test_insufficient_cash_is_rejected_before_the_committee(path):
    with _api_env(_stock("GATECASH"), _report(), cash=15.0) as env:
        resp = await _post(path, ticker="GATECASH", allocation_eur=500.0)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "INSUFFICIENT_CASH"
    assert "15.00" in detail["message"]
    env.committee.assert_not_awaited()
