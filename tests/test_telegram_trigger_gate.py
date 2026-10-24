"""Telegram /trigger and the /analyze auto-arm go through the same trigger gate as the dashboard."""
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import pytest
from sqlalchemy import select, delete

from config.settings import settings
from src.db.models import OrderTrigger, Position, SystemConfig
from src.db.session import init_db, AsyncSessionLocal
from src.telegram.bot import TelegramAssistant
from src.trading.trigger_gate import evaluate_manual_trigger

REGIME_BLOCK = (False, "Market regime filter: ^GSPC (4000) is below its 200-day average (4200); no new entries in a falling market.")
UNSET = object()


def _report(action="BUY", score=86, approved=True, summary="Strong conviction", entry=98.0, stop=95.0, target=110.0,
            risk_summary=None, trigger_type="LIMIT_PULLBACK"):
    return {
        "action": action,
        "consensus_score": score,
        "entry_trigger_price": entry,
        "trigger_type": trigger_type,
        "trigger_rationale": "Pullback to support",
        "summary": summary,
        "technical": {"bias": "BULLISH", "analysis_summary": "Above the 50 day average"},
        "fundamental": {"valuation": "FAIR"},
        "risk": {
            "approved": approved,
            "take_profit_price": target,
            "stop_loss_price": stop,
            "recommended_size_pct": 0.05,
            "risk_summary": risk_summary or ("Position size too large for the volatility" if not approved else "Managed risk"),
        },
    }


def _stock(ticker, price=100.0, currency="EUR", quote_type="EQUITY"):
    return {"ticker": ticker, "name": f"Name {ticker}", "price": price, "currency": currency, "quote_type": quote_type}


@pytest.fixture(autouse=True)
async def _clean_rows():
    """Every ticker these tests use starts with TGATE; remove their rows before and after each test."""
    async def _wipe():
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.like("TGATE%")))
            await session.execute(delete(Position).where(Position.ticker.like("TGATE%")))
            await session.commit()
    await init_db()
    await _wipe()
    yield
    await _wipe()


@asynccontextmanager
async def telegram_env(ticker, report=None, stock=UNSET, cash=5000.0, regime=(True, ""), threshold=80):
    """A bot with every outside call mocked: market data, wallet, committee, regime filter, broker and threshold."""
    bot = TelegramAssistant()
    bot.token = "mock_token"
    bot.base_url = "https://api.telegram.org/botmock_token"
    replies = []
    bot._reply_chat = AsyncMock(side_effect=lambda cid, txt: replies.append(txt))
    bot.notify_trigger_armed = AsyncMock()

    summary = {"total_value_eur": 10000.0, "available_cash_eur": cash}
    armed = {"success": True, "trigger_id": 42, "message": "Trigger armed"}
    with ExitStack() as stack:
        env = SimpleNamespace(
            bot=bot,
            replies=replies,
            market=stack.enter_context(patch(
                "src.data.market.market_data_provider.get_stock_data",
                return_value=_stock(ticker) if stock is UNSET else stock
            )),
            committee=stack.enter_context(patch(
                "src.agents.committee.investment_committee.evaluate_ticker", AsyncMock(return_value=report or _report())
            )),
            create=stack.enter_context(patch("src.trading.paper.paper_broker.create_price_trigger", AsyncMock(return_value=armed))),
            regime=stack.enter_context(patch("src.trading.trigger_gate.check_market_regime", return_value=regime)),
        )
        stack.enter_context(patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value=summary)))
        stack.enter_context(patch("src.telegram.bot.get_trigger_min_score", AsyncMock(return_value=threshold)))

        async def send(text):
            await bot._handle_update({"message": {"chat": {"id": 12345}, "text": text}})
            return replies[-1]

        env.send = send
        yield env


async def _insert_position(ticker):
    async with AsyncSessionLocal() as session:
        session.add(Position(
            ticker=ticker, name=ticker, currency="EUR", shares=1.0, entry_price_native=100.0, current_price_native=100.0,
            highest_price_native=100.0, entry_price_eur=100.0, current_price_eur=100.0, cost_basis_eur=100.0,
            status="OPEN", is_live=False
        ))
        await session.commit()


async def _insert_trigger(ticker):
    async with AsyncSessionLocal() as session:
        session.add(OrderTrigger(
            ticker=ticker, name=ticker, currency="EUR", trigger_type="LIMIT_PULLBACK", target_price=95.0,
            current_price_at_creation=100.0, allocation_eur=100.0, status="PENDING", is_live=False
        ))
        await session.commit()


# --- /trigger: input handling -------------------------------------------------------------------

@pytest.mark.parametrize("text", ["/trigger ^GSPC", "/trigger EURUSD=X", "/trigger AA/PL", "/trigger $", "/trigger AAPL!"])
async def test_trigger_rejects_an_invalid_ticker_before_any_lookup(text):
    async with telegram_env("TGATEX") as env:
        reply = await env.send(text)
    assert "not a valid stock ticker" in reply
    env.market.assert_not_called()
    env.committee.assert_not_awaited()
    env.create.assert_not_awaited()
    assert len(env.replies) == 1  # no "Analyzing..." message either


async def test_trigger_invalid_ticker_is_html_escaped():
    async with telegram_env("TGATEX") as env:
        reply = await env.send("/trigger <b>x</b>")
    assert "<b>x</b>" not in reply and "&lt;b&gt;x&lt;/b&gt;" in reply
    env.committee.assert_not_awaited()


@pytest.mark.parametrize("text", ["/trigger", "/trigger force", "/trigger FORCE"])
async def test_trigger_without_a_ticker_shows_the_usage(text):
    async with telegram_env("TGATEX") as env:
        reply = await env.send(text)
    assert "Usage" in reply and "[force]" in reply
    env.market.assert_not_called()


async def test_trigger_rejects_a_bad_price_before_any_lookup():
    async with telegram_env("TGATEX") as env:
        for text in ("/trigger TGATEX abc", "/trigger TGATEX 0", "/trigger TGATEX -5 force", "/trigger TGATEX force nan"):
            assert "Invalid price" in await env.send(text)
    env.market.assert_not_called()
    env.committee.assert_not_awaited()


# --- /trigger: prechecks run before the committee ---------------------------------------------

async def test_trigger_precheck_failures_never_run_the_committee():
    async with telegram_env("TGATEX", stock=None) as env:
        assert "Could not fetch market data" in await env.send("/trigger TGATEX")
    env.committee.assert_not_awaited()

    async with telegram_env("TGATEX", stock=_stock("TGATEX", quote_type="INDEX")) as env:
        reply = await env.send("/trigger TGATEX force")
        assert "Cannot arm trigger" in reply and "INDEX" in reply
    env.committee.assert_not_awaited()

    async with telegram_env("TGATEX", cash=12.5) as env:
        reply = await env.send("/trigger TGATEX")
        assert "Cannot arm trigger" in reply and "€12.50" in reply
    env.committee.assert_not_awaited()
    env.create.assert_not_awaited()


async def test_trigger_refuses_an_open_position_and_a_pending_trigger_before_the_committee():
    await _insert_position("TGATEPOS")
    await _insert_trigger("TGATEPEND")
    async with telegram_env("TGATEPOS") as env:
        assert "already an open position in TGATEPOS" in await env.send("/trigger TGATEPOS")
    env.committee.assert_not_awaited()
    async with telegram_env("TGATEPEND") as env:
        assert "already pending for TGATEPEND" in await env.send("/trigger TGATEPEND force")
    env.committee.assert_not_awaited()
    env.create.assert_not_awaited()


# --- /trigger: the verdict gate -----------------------------------------------------------------

async def test_trigger_arms_an_approved_buy_without_a_forced_note():
    async with telegram_env("TGATEBUY", report=_report(score=86)) as env:
        reply = await env.send("/trigger $tgatebuy")
    assert "AI PRICE TRIGGER ARMED #42" in reply and "Forced" not in reply
    kwargs = env.create.call_args.kwargs
    assert kwargs["ticker"] == "TGATEBUY" and kwargs["target_price"] == 98.0 and kwargs["trigger_type"] == "LIMIT_PULLBACK"
    env.committee.assert_awaited_once()
    # Force on a clean pass changes nothing either
    async with telegram_env("TGATEBUY", report=_report(score=86)) as env:
        reply = await env.send("/trigger TGATEBUY force")
    assert "AI PRICE TRIGGER ARMED #42" in reply and "Forced" not in reply


@pytest.mark.parametrize("report, verdict, score", [
    (_report(action="HOLD", score=72), "HOLD", "72/100"),
    (_report(action="BUY", score=70), "BUY", "70/100"),
])
async def test_trigger_blocks_a_soft_block_and_hints_how_to_force(report, verdict, score):
    async with telegram_env("TGATESOFT", report=report) as env:
        reply = await env.send("/trigger TGATESOFT")
    assert "TRIGGER NOT ARMED" in reply and "TGATESOFT" in reply
    assert verdict in reply and score in reply
    assert "<code>/trigger TGATESOFT force</code>" in reply
    env.create.assert_not_awaited()


async def test_trigger_force_hint_keeps_the_price_the_user_asked_for():
    async with telegram_env("TGATESOFT", report=_report(action="HOLD", score=72)) as env:
        reply = await env.send("/trigger TGATESOFT 95")
    assert "<code>/trigger TGATESOFT 95.0 force</code>" in reply
    env.create.assert_not_awaited()


@pytest.mark.parametrize("text, price", [
    ("/trigger TGATEFORCE force", 98.0),
    ("/trigger TGATEFORCE FORCE", 98.0),
    ("/trigger TGATEFORCE 95 force", 95.0),
    ("/trigger TGATEFORCE 95,5 Force", 95.5),
    ("/trigger TGATEFORCE force 95", 95.0),
    ("/trigger $TGATEFORCE $95 force", 95.0),
])
async def test_trigger_force_overrides_a_hold_verdict(text, price):
    async with telegram_env("TGATEFORCE", report=_report(action="HOLD", score=72)) as env:
        reply = await env.send(text)
    assert "AI PRICE TRIGGER ARMED #42" in reply
    assert "Forced: armed against the committee verdict (HOLD, score 72/100, trigger threshold 80)" in reply
    assert env.create.call_args.kwargs["target_price"] == price


async def test_trigger_force_overrides_a_low_score():
    async with telegram_env("TGATEFORCE", report=_report(action="BUY", score=70)) as env:
        reply = await env.send("/trigger TGATEFORCE force")
    assert "AI PRICE TRIGGER ARMED #42" in reply
    assert "armed against the committee verdict (BUY, score 70/100, trigger threshold 80)" in reply
    env.create.assert_awaited_once()


async def test_trigger_threshold_comes_from_the_gate_setting():
    async with telegram_env("TGATETHR", report=_report(action="BUY", score=70), threshold=65) as env:
        reply = await env.send("/trigger TGATETHR")
    assert "AI PRICE TRIGGER ARMED #42" in reply
    env.create.assert_awaited_once()
    async with telegram_env("TGATETHR", report=_report(action="BUY", score=70), threshold=90) as env:
        reply = await env.send("/trigger TGATETHR")
    assert "below the trigger threshold of 90" in reply
    env.create.assert_not_awaited()


async def test_trigger_force_with_a_score_below_the_broker_minimum_is_refused_without_a_force_hint():
    async with telegram_env("TGATEMIN", report=_report(action="BUY", score=50)) as env:
        reply = await env.send("/trigger TGATEMIN force")
    assert "TRIGGER NOT ARMED" in reply and "minimum consensus score" in reply
    assert "force" not in reply.lower().replace("force does not override", "")
    env.create.assert_not_awaited()


@pytest.mark.parametrize("report, code_text", [
    (_report(action="SELL", score=90), "verdict is SELL"),
    (_report(action="BUY", score=95, approved=False), "Risk Officer vetoed"),
    (_report(action="HOLD", score=40, approved=False), "Risk Officer vetoed"),
])
async def test_trigger_hard_verdict_blocks_cannot_be_forced(report, code_text):
    for text in ("/trigger TGATEHARD", "/trigger TGATEHARD force", "/trigger TGATEHARD 95 force"):
        async with telegram_env("TGATEHARD", report=report) as env:
            reply = await env.send(text)
        assert "TRIGGER NOT ARMED" in reply and code_text in reply
        assert "resend" not in reply and "<code>/trigger" not in reply
        env.create.assert_not_awaited()


async def test_trigger_falling_market_is_a_hard_block_even_with_force_and_a_soft_verdict():
    async with telegram_env("TGATEMKT", report=_report(action="HOLD", score=72), regime=REGIME_BLOCK) as env:
        for text in ("/trigger TGATEMKT", "/trigger TGATEMKT force"):
            reply = await env.send(text)
            assert "TRIGGER NOT ARMED" in reply and "200-day average" in reply
            assert "resend" not in reply
    env.create.assert_not_awaited()


async def test_trigger_share_that_does_not_fit_the_allocation_is_a_hard_block(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    expensive = _stock("TGATEPRICEY", price=5000.0)
    async with telegram_env("TGATEPRICEY", stock=expensive, report=_report(entry=4900.0, stop=4700.0, target=5500.0)) as env:
        for text in ("/trigger TGATEPRICEY", "/trigger TGATEPRICEY force"):
            reply = await env.send(text)
            assert "TRIGGER NOT ARMED" in reply and "One share of TGATEPRICEY costs about €4900.00" in reply
            assert "resend" not in reply
    env.create.assert_not_awaited()


async def test_trigger_reasons_are_html_escaped():
    report = _report(approved=False, risk_summary="Too <b>risky</b> & volatile")
    async with telegram_env("TGATEESC", report=report) as env:
        reply = await env.send("/trigger TGATEESC")
    assert "Too &lt;b&gt;risky&lt;/b&gt; &amp; volatile" in reply and "<b>risky</b>" not in reply


async def test_trigger_gate_judges_the_levels_and_allocation_that_get_armed():
    from src.telegram import bot as bot_module
    real = evaluate_manual_trigger
    gate = AsyncMock(side_effect=real)
    with patch.object(bot_module, "evaluate_manual_trigger", gate):
        async with telegram_env("TGATELVL", report=_report(action="HOLD", score=72)) as env:
            await env.send("/trigger TGATELVL 95 force")
    args, kwargs = gate.call_args
    kwargs_armed = env.create.call_args.kwargs
    assert args[0] == "TGATELVL"
    assert args[3] == kwargs_armed["target_price"] == 95.0
    assert args[4] == kwargs_armed["allocation_eur"] > 0
    assert args[5] == 80 and kwargs["force"] is True


# --- /trigger: explicit price keeps deciding the trigger type -------------------------------

async def test_trigger_explicit_price_below_and_above_the_market():
    async with telegram_env("TGATEPX") as env:
        await env.send("/trigger TGATEPX 90")
    kwargs = env.create.call_args.kwargs
    assert (kwargs["trigger_type"], kwargs["target_price"]) == ("LIMIT_PULLBACK", 90.0)

    async with telegram_env("TGATEPX", report=_report(stop=95.0, target=110.0)) as env:
        await env.send("/trigger TGATEPX 105")
    kwargs = env.create.call_args.kwargs
    assert (kwargs["trigger_type"], kwargs["target_price"]) == ("BREAKOUT_BUY", 105.0)
    assert kwargs["stop_loss"] < 105.0 < kwargs["take_profit"]


# --- Levels: _plan_trigger / _arm_trigger_from_report keep the old results ------------------

def _old_levels(data, report, explicit_price=None):
    """The level calculation _arm_trigger_from_report had before it was split from the arming."""
    cur_pr = data["price"]
    trigger_price = explicit_price or report.get("entry_trigger_price", cur_pr)
    trigger_type = ("LIMIT_PULLBACK" if explicit_price < cur_pr else "BREAKOUT_BUY") if explicit_price else report.get("trigger_type", "LIMIT_PULLBACK")
    if trigger_type == "LIMIT_PULLBACK" and trigger_price >= cur_pr:
        trigger_price = round(cur_pr * 0.985, 2)
    elif trigger_type == "BREAKOUT_BUY" and trigger_price <= cur_pr:
        trigger_price = round(cur_pr * 1.01, 2)
    sl = report.get("risk", {}).get("stop_loss_price")
    tp = report.get("risk", {}).get("take_profit_price")
    if sl and sl >= trigger_price:
        sl = round(trigger_price * 0.96, 2)
    if tp and tp <= trigger_price:
        tp = round(trigger_price * 1.06, 2)
    return {"target_price": trigger_price, "trigger_type": trigger_type, "stop_loss": sl, "take_profit": tp}


@pytest.mark.parametrize("explicit", [None, 90.0, 100.0, 105.0])
@pytest.mark.parametrize("report", [
    _report(entry=98.0, stop=95.0, target=110.0),
    _report(entry=102.0, stop=99.0, target=120.0),                              # pullback entry above the market
    _report(entry=95.0, stop=97.0, target=94.0),                                # stop above and target below the entry
    _report(entry=99.0, trigger_type="BREAKOUT_BUY", stop=96.0, target=115.0),  # breakout entry below the market
    _report(entry=108.0, trigger_type="BREAKOUT_BUY", stop=100.0, target=125.0),
])
def test_plan_trigger_matches_the_previous_calculation(report, explicit):
    bot = TelegramAssistant()
    data = _stock("TGATEPLAN")
    plan = bot._plan_trigger(data, report, 5000.0, explicit)
    expected = _old_levels(data, report, explicit)
    assert {k: plan[k] for k in expected} == expected
    assert plan["alloc_eur"] > 0


async def test_arm_trigger_from_report_accepts_a_plan_or_computes_its_own():
    bot = TelegramAssistant()
    data, report = _stock("TGATEARM"), _report()
    with patch("src.trading.paper.paper_broker.create_price_trigger", AsyncMock(return_value={"success": True, "trigger_id": 1})) as create:
        own = await bot._arm_trigger_from_report("TGATEARM", data, report, 5000.0, 90.0)
        plan = bot._plan_trigger(data, report, 5000.0, 90.0)
        given = await bot._arm_trigger_from_report("TGATEARM", data, report, 5000.0, 90.0, plan=plan)
    assert own == given == {"success": True, "trigger_id": 1, **plan}
    for call in create.call_args_list:
        assert call.kwargs["target_price"] == 90.0 and call.kwargs["allocation_eur"] == plan["alloc_eur"]


# --- /analyze auto-arm ------------------------------------------------------------------------

async def test_analyze_auto_arms_an_approved_buy_at_the_threshold_and_names_the_real_threshold():
    async with telegram_env("TGATEAUTO", report=_report(action="STRONG_BUY", score=86), threshold=80) as env:
        reply = await env.send("/analyze $TGATEAUTO")
    assert "AUTOMATIC TRIGGER ARMED #42" in reply
    assert "threshold of 80" in reply and "exceeds" not in reply
    kwargs = env.create.call_args.kwargs
    assert kwargs["ticker"] == "TGATEAUTO" and kwargs["target_price"] == 98.0
    env.bot.notify_trigger_armed.assert_awaited_once()


async def test_analyze_uses_the_saved_threshold_not_a_hard_coded_80():
    async with telegram_env("TGATEAUTO", report=_report(action="BUY", score=72), threshold=70) as env:
        reply = await env.send("/analyze TGATEAUTO")
    assert "AUTOMATIC TRIGGER ARMED" in reply and "threshold of 70" in reply
    env.create.assert_awaited_once()
    async with telegram_env("TGATEAUTO", report=_report(action="BUY", score=85), threshold=90) as env:
        reply = await env.send("/analyze TGATEAUTO")
    assert "AUTOMATIC TRIGGER ARMED" not in reply and "Not auto-armed" not in reply
    env.create.assert_not_awaited()


async def test_analyze_reads_the_threshold_from_the_database_setting(monkeypatch):
    monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", 80)

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
        await set_saved("70")
        # Not patching get_trigger_min_score here: the real lookup must see the saved 70
        bot = TelegramAssistant()
        bot._reply_chat = AsyncMock()
        bot.notify_trigger_armed = AsyncMock()
        with patch("src.data.market.market_data_provider.get_stock_data", return_value=_stock("TGATEDB")), \
             patch("src.agents.committee.investment_committee.evaluate_ticker", AsyncMock(return_value=_report(score=75))), \
             patch("src.trading.paper.paper_broker.get_portfolio_summary", AsyncMock(return_value={"total_value_eur": 10000.0, "available_cash_eur": 5000.0})), \
             patch("src.trading.paper.paper_broker.create_price_trigger", AsyncMock(return_value={"success": True, "trigger_id": 7})) as create:
            await bot._handle_update({"message": {"chat": {"id": 12345}, "text": "/analyze TGATEDB"}})
        create.assert_awaited_once()
        assert "threshold of 70" in bot._reply_chat.call_args.args[1]
    finally:
        await set_saved(original_value)


@pytest.mark.parametrize("report, reason", [
    (_report(action="HOLD", score=85), "verdict is HOLD"),
    (_report(action="SELL", score=90), "verdict is SELL"),
    (_report(action="HOLD", score=88, approved=False), "Risk Officer vetoed this trade: Position size too large"),
])
async def test_analyze_does_not_arm_when_the_verdict_blocks_and_says_why(report, reason):
    async with telegram_env("TGATEBLK", report=report) as env:
        reply = await env.send("/analyze TGATEBLK")
    assert "COMMITTEE VERDICT" in reply
    assert "AUTOMATIC TRIGGER ARMED" not in reply
    assert "<i>Not auto-armed:" in reply and reason in reply
    env.create.assert_not_awaited()
    env.bot.notify_trigger_armed.assert_not_awaited()


@pytest.mark.parametrize("report", [
    _report(action="BUY", score=75),
    _report(action="STRONG_BUY", score=79),
    _report(action="HOLD", score=50),
    _report(action="SELL", score=20),
    _report(action="HOLD", score=60, approved=False),
])
async def test_analyze_stays_quiet_when_the_score_is_below_the_threshold(report):
    async with telegram_env("TGATELOW", report=report) as env:
        reply = await env.send("/analyze TGATELOW")
    assert "COMMITTEE VERDICT" in reply
    assert "auto-armed" not in reply.lower() and "AUTOMATIC" not in reply
    env.create.assert_not_awaited()
    env.regime.assert_not_called()


async def test_analyze_reports_open_positions_pending_triggers_and_missing_cash():
    await _insert_position("TGATEPOS")
    await _insert_trigger("TGATEPEND")
    async with telegram_env("TGATEPOS") as env:
        assert "Position already open in portfolio" in await env.send("/analyze TGATEPOS")
    async with telegram_env("TGATEPEND") as env:
        assert "Pending trigger already active for TGATEPEND" in await env.send("/analyze TGATEPEND")
    async with telegram_env("TGATECASH", cash=12.5) as env:
        reply = await env.send("/analyze TGATECASH")
        assert "86/100" in reply and "threshold of 80" in reply and "cash is insufficient (€12.50)" in reply
        assert "exceeds" not in reply
    env.create.assert_not_awaited()


async def test_analyze_does_not_arm_in_a_falling_market():
    async with telegram_env("TGATEMKT", regime=REGIME_BLOCK) as env:
        reply = await env.send("/analyze TGATEMKT")
    assert "<i>Not auto-armed:" in reply and "200-day average" in reply and "AUTOMATIC TRIGGER ARMED" not in reply
    env.create.assert_not_awaited()


async def test_analyze_does_not_arm_when_one_share_does_not_fit(monkeypatch):
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    expensive = _stock("TGATEPRICEY", price=5000.0)
    async with telegram_env("TGATEPRICEY", stock=expensive, report=_report(entry=4900.0, stop=4700.0, target=5500.0)) as env:
        reply = await env.send("/analyze TGATEPRICEY")
    assert "<i>Not auto-armed:" in reply and "One share of TGATEPRICEY costs about €4900.00" in reply
    env.create.assert_not_awaited()


async def test_analyze_does_not_arm_an_untradable_instrument():
    async with telegram_env("TGATEIDX", stock=_stock("TGATEIDX", quote_type="INDEX")) as env:
        reply = await env.send("/analyze TGATEIDX")
    assert "<i>Not auto-armed:" in reply and "INDEX" in reply
    async with telegram_env("^GSPC", stock=_stock("^GSPC", quote_type=None)) as env:
        reply = await env.send("/analyze ^GSPC")
    assert "not a valid stock ticker" in reply
    env.create.assert_not_awaited()


async def test_analyze_reasons_are_html_escaped():
    report = _report(action="HOLD", score=88, approved=False, risk_summary="Too <i>risky</i>")
    async with telegram_env("TGATEESC", report=report) as env:
        reply = await env.send("/analyze TGATEESC")
    assert "Too &lt;i&gt;risky&lt;/i&gt;" in reply and "Too <i>risky</i>" not in reply


async def test_analyze_reports_a_broker_refusal_escaped():
    async with telegram_env("TGATEREF") as env:
        env.create.return_value = {"success": False, "message": "Refused <now>"}
        reply = await env.send("/analyze TGATEREF")
    assert "Could not auto-arm trigger: Refused &lt;now&gt;" in reply


# --- /help ---------------------------------------------------------------------------------------

async def test_help_describes_the_force_option():
    async with telegram_env("TGATEX") as env:
        reply = await env.send("/help")
    assert "/trigger TICKER [PRICE] [force]" in reply
    assert "force</code> overrides a HOLD verdict or low score" in reply


# --- /buy: same verdict gate as /trigger ---------------------------------------------------------

BOUGHT = {"success": True, "shares": 3, "price_native": 100.0, "price_eur": 100.0, "total_eur": 300.0, "stop_loss": 95.0, "take_profit": 110.0}


@asynccontextmanager
async def buy_env(ticker, report):
    async with telegram_env(ticker, report=report) as env:
        with patch("src.trading.risk.check_market_hours", return_value=(True, "")), \
             patch("src.trading.paper.paper_broker.execute_buy", AsyncMock(return_value=dict(BOUGHT))) as buy:
            env.buy = buy
            yield env


@pytest.mark.parametrize("report, verdict", [(_report(action="HOLD", score=72), "HOLD"), (_report(action="BUY", score=70), "BUY")])
async def test_buy_soft_block_needs_force_and_hints_it(report, verdict):
    async with buy_env("TGATEB1", report) as env:
        reply = await env.send("/buy TGATEB1 500")
    assert "BUY NOT EXECUTED" in reply and verdict in reply and "/buy TGATEB1 500 force" in reply
    env.buy.assert_not_awaited()


@pytest.mark.parametrize("text", ["/buy TGATEB2 force", "/buy TGATEB2 force 250", "/buy TGATEB2 250 force"])
async def test_buy_force_overrides_a_soft_block(text):
    async with buy_env("TGATEB2", _report(action="HOLD", score=72)) as env:
        reply = await env.send(text)
    assert "MANUAL BUY ORDER EXECUTED" in reply and "Forced" in reply
    env.buy.assert_awaited_once()


@pytest.mark.parametrize("report, text", [
    (_report(action="SELL", score=20), "SELL"),
    (_report(action="BUY", score=90, approved=False), "vetoed"),
])
async def test_buy_never_allowed_on_sell_or_veto_even_with_force(report, text):
    async with buy_env("TGATEB3", report) as env:
        reply = await env.send("/buy TGATEB3 force")
    assert "BUY NOT EXECUTED" in reply and text in reply and "resend" not in reply
    env.buy.assert_not_awaited()


async def test_buy_approved_verdict_executes_without_force_and_rejects_bad_ticker():
    async with buy_env("TGATEB4", _report(score=86)) as env:
        reply = await env.send("/buy TGATEB4")
        assert "MANUAL BUY ORDER EXECUTED" in reply and "Forced" not in reply
        reply = await env.send("/buy ^GSPC")
        assert "not a valid stock ticker" in reply
    assert env.buy.await_count == 1


async def test_buy_forced_below_broker_minimum_is_refused_by_the_gate():
    # The gate now stops it before the broker, so the broker-failure hint in the bot is no longer reachable
    async with buy_env("TGATEB5", _report(action="HOLD", score=50)) as env:
        reply = await env.send("/buy TGATEB5 force")
    assert "BUY NOT EXECUTED" in reply and "minimum consensus score" in reply and "/buy TGATEB5" not in reply
    env.buy.assert_not_awaited()


# --- /sync and /shadow ---------------------------------------------------------------------------

SYNC_SUMMARY = {"total_value_eur": 12345.5, "cash_eur": 1000.0, "available_cash_eur": 900.0, "open_positions_count": 4}


@pytest.mark.parametrize("live", [False, True])
async def test_sync_reports_key_figures_in_paper_and_live_mode(live):
    from src.trading import broker as broker_mod
    sync = AsyncMock(return_value=SYNC_SUMMARY)
    fake = broker_mod.live_broker if live else SimpleNamespace(sync_account_state=sync)
    async with telegram_env("TGATES") as env:
        with patch("src.telegram.bot.get_active_broker", return_value=fake), \
             patch.object(broker_mod.live_broker, "sync_account_state", sync):
            reply = await env.send("/sync")
    sync.assert_awaited_once()
    assert ("LIVE" if live else "PAPER") in reply
    assert "12,345.50" in reply and "900.00" in reply and "<b>Open Positions:</b> 4" in reply


async def test_sync_error_is_reported_and_escaped():
    broken = SimpleNamespace(sync_account_state=AsyncMock(side_effect=RuntimeError("<boom>")))
    async with telegram_env("TGATES") as env:
        with patch("src.telegram.bot.get_active_broker", return_value=broken):
            reply = await env.send("/sync")
    assert "Sync failed" in reply and "&lt;boom&gt;" in reply


async def test_shadow_renders_and_handles_none_enabled():
    comp = {"main": {"label": "Paper", "total_value_eur": 10000.0}, "strategies": [{
        "key": "x", "label": "Aggr<essive>", "return_pct": 3.456, "total_value_eur": 10345.6, "started_at": "2026-01-01 10:00",
        "main_same_period": {"return_pct": None}, "open_positions": [{}], "pending_triggers": [{}, {}]}]}
    async with telegram_env("TGATES") as env:
        with patch("src.trading.shadow.shadow_portfolios.get_comparison", AsyncMock(return_value=comp)):
            reply = await env.send("/shadow")
        assert "SHADOW PORTFOLIOS" in reply and "Aggr&lt;essive&gt;" in reply and "+3.46%" in reply and "main n/a" in reply
        assert "open 1, pending 2" in reply
        with patch("src.trading.shadow.shadow_portfolios.get_comparison", AsyncMock(return_value={"strategies": []})):
            reply = await env.send("/shadow")
        assert "No shadow strategies" in reply
