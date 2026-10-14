import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import OrderTrigger, Position, ShadowPosition, ShadowTrigger
from src.db.session import AsyncSessionLocal
from src.trading import shadow as shadow_module
from src.trading.shadow import PRESETS, get_strategies, plan_entry, shadow_portfolios


def _report(ticker, score=85, action="BUY", approved=True, entry=95.0, trig_type="LIMIT_PULLBACK", sl=90.0, tp=110.0):
    return {
        "ticker": ticker,
        "action": action,
        "consensus_score": score,
        "entry_trigger_price": entry,
        "trigger_type": trig_type,
        "risk": {"approved": approved, "stop_loss_price": sl, "take_profit_price": tp},
    }


def _data(ticker, price=100.0):
    return {"ticker": ticker, "name": f"{ticker} Corp", "price": price, "currency": "EUR"}


async def _set_config(key, value):
    from src.db.models import SystemConfig
    async with AsyncSessionLocal() as session:
        row = await session.get(SystemConfig, key)
        if row:
            row.value = value
        else:
            session.add(SystemConfig(key=key, value=value))
        await session.commit()


@pytest.fixture
def shadows(monkeypatch):
    """Enables the given strategies on a clean slate and fakes open markets and quotes."""
    prices = {}

    def enable(keys):
        monkeypatch.setattr(settings, "SHADOW_ENABLED", True)
        monkeypatch.setattr(settings, "SHADOW_STRATEGIES", ",".join(keys))
        monkeypatch.setattr(settings, "SHADOW_INITIAL_CAPITAL", 10000.0)
        monkeypatch.setattr(settings, "TRIGGER_MIN_SCORE", 80)
        return prices

    async def _reset_all(main_trigger_min_score=80):
        await _set_config("TRIGGER_MIN_SCORE", str(main_trigger_min_score))
        await _set_config("BREAKEVEN_TRIGGER_PCT", "0.025")
        for key in list(PRESETS) + ["tight"]:
            await shadow_portfolios.reset(key)

    monkeypatch.setattr(shadow_module, "check_market_hours", lambda t: (True, "open"))
    monkeypatch.setattr(shadow_module.market_data_provider, "get_stock_data",
                        lambda t: {"price": prices[t]} if t in prices else None)
    return enable, _reset_all


def test_plan_entry_baseline_matches_main_levels():
    plan = plan_entry(PRESETS["baseline"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0)
    assert plan == {"trigger_type": "LIMIT_PULLBACK", "target_price": 95.0, "stop_loss": 90.0, "take_profit": 110.0}


def test_plan_entry_aggressive_and_momentum():
    # Half the pullback (97.5), stop distance 5 x 1.25 = 6.25, target 3R
    aggressive = plan_entry(PRESETS["aggressive"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0)
    assert aggressive == {"trigger_type": "LIMIT_PULLBACK", "target_price": 97.5, "stop_loss": 91.25, "take_profit": 116.25}

    momentum = plan_entry(PRESETS["momentum"], "LIMIT_PULLBACK", 95.0, 90.0, 110.0, 100.0)
    assert momentum["trigger_type"] == "MARKET"
    assert momentum["target_price"] == 100.0
    assert momentum["stop_loss"] == 95.0
    assert momentum["take_profit"] is None


def test_strategy_selection_and_custom_json(monkeypatch):
    monkeypatch.setattr(settings, "SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "SHADOW_STRATEGIES", "aggressive, nope, tight, aggressive")
    monkeypatch.setattr(settings, "SHADOW_CUSTOM_STRATEGIES",
                        '{"tight": {"base": "aggressive", "stop_scale": 0.8, "label": "Tight", "bogus": 1}}')
    strategies = get_strategies()
    assert [s.key for s in strategies] == ["aggressive", "tight"]
    tight = strategies[1]
    assert tight.stop_scale == 0.8 and tight.score_offset == -10 and tight.label == "Tight"

    monkeypatch.setattr(settings, "SHADOW_ENABLED", False)
    assert get_strategies() == []


@pytest.mark.asyncio
async def test_follow_report_respects_each_strategy_threshold(shadows):
    enable, reset_all = shadows
    enable(["aggressive", "baseline"])
    await reset_all()

    # Score 72 clears aggressive (80 - 10) but not baseline (80)
    armed = await shadow_portfolios.follow_report(_report("SHA.AS", score=72), _data("SHA.AS"))
    assert [a["strategy"] for a in armed] == ["aggressive"]

    # Same ticker again: no duplicate trigger
    assert await shadow_portfolios.follow_report(_report("SHA.AS", score=90), _data("SHA.AS")) == [
        {"strategy": "baseline", "ticker": "SHA.AS", "trigger_type": "LIMIT_PULLBACK",
         "target_price": 95.0, "stop_loss": 90.0, "take_profit": 110.0}
    ]

    # HOLD verdicts and Risk Officer vetoes are skipped
    assert await shadow_portfolios.follow_report(_report("SHB.AS", action="HOLD"), _data("SHB.AS")) == []
    assert await shadow_portfolios.follow_report(_report("SHC.AS", approved=False), _data("SHC.AS")) == []

    # Nothing lands in the main portfolio's tables
    async with AsyncSessionLocal() as session:
        main_trigs = await session.execute(select(OrderTrigger).where(OrderTrigger.ticker.in_(["SHA.AS", "SHB.AS", "SHC.AS"])))
        assert main_trigs.scalars().all() == []


@pytest.mark.asyncio
async def test_trigger_fill_trailing_stop_and_exit(shadows):
    enable, reset_all = shadows
    prices = enable(["aggressive"])
    await reset_all()

    prices["SHD.AS"] = 100.0
    await shadow_portfolios.follow_report(_report("SHD.AS"), _data("SHD.AS"))

    # Not yet at the 97.5 entry
    assert await shadow_portfolios.check_triggers() == []
    prices["SHD.AS"] = 97.0
    filled = await shadow_portfolios.check_triggers()
    assert len(filled) == 1 and filled[0]["price_native"] == 97.0
    # A fifth of the 10k (max 5 positions), capped so the 91.25 stop loses 1% (~€1,687), in whole shares
    assert filled[0]["shares"] == 17.0

    # Price runs up: the stop trails 6% under the high
    prices["SHD.AS"] = 110.0
    assert await shadow_portfolios.check_exits() == []
    async with AsyncSessionLocal() as session:
        pos = (await session.execute(select(ShadowPosition).where(ShadowPosition.ticker == "SHD.AS"))).scalars().one()
        assert pos.stop_loss_native == round(110.0 * 0.94, 2)

    # Falls back through the trailing stop: closed with a profit
    prices["SHD.AS"] = 103.0
    closed = await shadow_portfolios.check_exits()
    assert len(closed) == 1 and closed[0]["realized_pnl_eur"] > 0

    comparison = await shadow_portfolios.get_comparison({"total_value_eur": 10000.0, "trading_mode": "PAPER"}, False)
    agg = comparison["strategies"][0]
    assert agg["key"] == "aggressive"
    assert agg["closed_trades"] == 1 and agg["wins"] == 1
    assert agg["total_value_eur"] == agg["cash_eur"] > 10000.0
    assert agg["main_same_period"]["return_pct"] == 0.0

    # The main portfolio never saw this trade
    async with AsyncSessionLocal() as session:
        main_pos = await session.execute(select(Position).where(Position.ticker == "SHD.AS"))
        assert main_pos.scalars().all() == []


@pytest.mark.asyncio
async def test_market_entry_time_exit_and_reset(shadows):
    enable, reset_all = shadows
    prices = enable(["momentum"])
    await reset_all()

    prices["SHE.AS"] = 100.0
    await shadow_portfolios.follow_report(_report("SHE.AS"), _data("SHE.AS"))
    async with AsyncSessionLocal() as session:
        pos = (await session.execute(select(ShadowPosition).where(ShadowPosition.ticker == "SHE.AS"))).scalars().one()
        assert pos.status == "OPEN" and pos.take_profit_native is None
        pos.opened_at = pos.opened_at - datetime.timedelta(days=21)
        await session.commit()

    closed = await shadow_portfolios.check_exits()
    assert closed and "Time exit" in closed[0]["reason"]

    assert await shadow_portfolios.reset("momentum") is True
    async with AsyncSessionLocal() as session:
        left = await session.execute(select(ShadowTrigger).where(ShadowTrigger.strategy == "momentum"))
        assert left.scalars().all() == []


@pytest.mark.asyncio
async def test_shadow_errors_never_reach_the_main_bot(shadows):
    enable, _ = shadows
    enable(["aggressive"])
    with patch.object(shadow_module, "derive_trigger_levels", side_effect=RuntimeError("boom")):
        assert await shadow_portfolios.follow_report(_report("SHF.AS"), _data("SHF.AS")) == []


@pytest.mark.asyncio
async def test_market_session_feeds_the_shadow(shadows):
    """The shadow sees the same committee verdicts as the main bot and records which ones the main bot acted on."""
    from sqlalchemy import delete
    from src.scheduler.jobs import trading_scheduler

    enable, reset_all = shadows
    enable(["aggressive"])
    await reset_all()

    candidates = [{"ticker": "SHG85", "pre_score": 85}, {"ticker": "SHH75", "pre_score": 75}]

    def stock(ticker):
        return {"ticker": ticker, "name": ticker, "price": 100.0, "currency": "EUR", "technicals": {}, "fundamentals": {}}

    async def evaluate(data, total):
        score = 85 if data["ticker"] == "SHG85" else 75
        return {
            **_report(data["ticker"], score=score, entry=98.0, sl=95.0),
            "summary": "s", "debate_transcript": "d", "trigger_rationale": "r",
            "technical": {"analysis_summary": ""}, "fundamental": {"analysis_summary": ""},
            "sentiment": {"sentiment_summary": ""},
            "risk": {"approved": True, "stop_loss_price": 95.0, "take_profit_price": 110.0, "risk_summary": ""},
        }

    try:
        with patch("src.screener.scanner.market_scanner.scan_universe", return_value=candidates), \
             patch("src.data.market.market_data_provider.get_stock_data", side_effect=stock), \
             patch("src.agents.committee.investment_committee.evaluate_ticker", side_effect=evaluate), \
             patch("src.telegram.bot.telegram_bot.send_message"), \
             patch("src.telegram.bot.telegram_bot.notify_trigger_armed"):
            await trading_scheduler.run_market_session(market="EU", session="OPEN")

        async with AsyncSessionLocal() as session:
            res = await session.execute(select(ShadowTrigger).where(ShadowTrigger.strategy == "aggressive"))
            trigs = {t.ticker: t for t in res.scalars().all()}
        assert set(trigs) == {"SHG85", "SHH75"}
        assert trigs["SHG85"].followed_main is True
        assert trigs["SHH75"].followed_main is False   # only the shadow's lower threshold took this one
        assert trigs["SHG85"].target_price == 99.0      # half of the main bot's 2% pullback
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_(["SHG85", "SHH75"])))
            await session.commit()


@pytest.mark.asyncio
async def test_snapshots_set_the_main_baseline_and_throttle(shadows):
    from src.db.models import ShadowAccount, ShadowSnapshot
    enable, reset_all = shadows
    enable(["baseline"])
    await reset_all()

    calls = []

    async def main_summary():
        calls.append(1)
        return {"total_value_eur": 12000.0}

    await shadow_portfolios.record_snapshots(main_summary)
    await shadow_portfolios.record_snapshots(main_summary)  # within the hour: nothing to do, no broker call
    assert len(calls) == 1
    async with AsyncSessionLocal() as session:
        acc = await session.get(ShadowAccount, "baseline")
        assert acc.main_value_at_start_eur == 12000.0
        snaps = (await session.execute(select(ShadowSnapshot).where(ShadowSnapshot.strategy == "baseline"))).scalars().all()
        assert len(snaps) == 1


@pytest.mark.asyncio
async def test_learner_changes_to_the_main_bot_do_not_move_a_running_shadow(shadows):
    """The shadow keeps the main threshold and breakeven % it started with; a reset picks up the new ones."""
    enable, reset_all = shadows
    enable(["aggressive"])
    await reset_all(main_trigger_min_score=80)

    # Shadow starts while the main threshold is 80 (aggressive: 70)
    assert await shadow_portfolios.follow_report(_report("SHI.AS", score=1), _data("SHI.AS")) == []

    # The learner tightens the main bot to 90 and moves breakeven to 5%
    await _set_config("TRIGGER_MIN_SCORE", "90")
    await _set_config("BREAKEVEN_TRIGGER_PCT", "0.05")
    armed = await shadow_portfolios.follow_report(_report("SHI.AS", score=72), _data("SHI.AS"))
    assert [a["strategy"] for a in armed] == ["aggressive"]

    comparison = await shadow_portfolios.get_comparison({"total_value_eur": 10000.0}, False)
    params = comparison["strategies"][0]["params"]
    assert params["effective_trigger_min_score"] == 70
    assert params["effective_breakeven_pct"] == 0.025
    assert params["current_main_params"]["trigger_min_score"] == 90

    # After a reset the shadow restarts on the main bot's new values (aggressive: 80)
    await shadow_portfolios.reset("aggressive")
    assert await shadow_portfolios.follow_report(_report("SHJ.AS", score=75), _data("SHJ.AS")) == []
    await _set_config("TRIGGER_MIN_SCORE", "80")


@pytest.mark.asyncio
async def test_whole_shares_atr_trail_and_regime_filter(shadows, monkeypatch):
    """Shadows follow the main bot's newer rules unless a strategy overrides them."""
    from src.db.models import ShadowAccount
    enable, reset_all = shadows
    prices = enable(["baseline", "tight"])
    monkeypatch.setattr(settings, "SHADOW_CUSTOM_STRATEGIES",
                        '{"tight": {"base": "baseline", "regime_filter": true, "trailing_atr_mult": 1.0}}')
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(settings, "TRAILING_STOP_ATR_MULT", 2.5)
    monkeypatch.setattr(settings, "REGIME_FILTER_ENABLED", False)
    await reset_all()

    # The index is in a downtrend: only "tight" (own regime filter) holds back
    monkeypatch.setattr(shadow_module.market_data_provider, "get_index_regime",
                        lambda index, days: {"above_sma": False}, raising=False)
    prices["SHK.AS"] = 100.0
    await shadow_portfolios.follow_report(_report("SHK.AS"), _data("SHK.AS"))
    prices["SHK.AS"] = 94.0
    filled = await shadow_portfolios.check_triggers()
    assert [f["strategy"] for f in filled] == ["baseline"]
    # €1,250 slot, capped at 0.5% risk: 10000 x 0.005 / (4 / 94) = €1,175 -> 12 whole shares
    assert filled[0]["shares"] == 12.0

    # Past breakeven the stop trails at high - 2.5 x ATR (main bot's value)
    monkeypatch.setattr(shadow_module.market_data_provider, "get_stock_data",
                        lambda t: {"price": prices[t], "technicals": {"atr14": 2.0}})
    prices["SHK.AS"] = 104.0
    await shadow_portfolios.check_exits()
    async with AsyncSessionLocal() as session:
        pos = (await session.execute(select(ShadowPosition).where(
            ShadowPosition.strategy == "baseline", ShadowPosition.ticker == "SHK.AS"))).scalars().one()
        assert pos.stop_loss_native == 99.0

        # Started on the main values of that moment
        acc = await session.get(ShadowAccount, "baseline")
        assert acc.frozen_params["risk_per_trade_pct"] == 0.5
        assert acc.frozen_params["regime_filter"] is False

    # One share costing more than the slot: the entry is dropped, as in the main bot
    prices["SHL.AS"] = 5000.0
    await shadow_portfolios.follow_report(_report("SHL.AS", entry=4900.0, sl=4700.0, tp=5500.0), _data("SHL.AS", 5000.0))
    prices["SHL.AS"] = 4890.0
    await shadow_portfolios.check_triggers()
    async with AsyncSessionLocal() as session:
        trig = (await session.execute(select(ShadowTrigger).where(
            ShadowTrigger.strategy == "baseline", ShadowTrigger.ticker == "SHL.AS"))).scalars().one()
        assert trig.status == "CANCELLED"


@pytest.mark.asyncio
async def test_older_shadow_freezes_new_main_parameters_on_first_use(shadows, monkeypatch):
    from src.db.models import ShadowAccount
    enable, reset_all = shadows
    enable(["baseline"])
    await reset_all()
    await shadow_portfolios.record_snapshots()
    async with AsyncSessionLocal() as session:
        acc = await session.get(ShadowAccount, "baseline")
        acc.frozen_params = {"trigger_min_score": 80, "breakeven_pct": 0.025, "max_positions": 8}
        await session.commit()

    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.7)
    await shadow_portfolios.check_exits()
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.3)
    async with AsyncSessionLocal() as session:
        acc = await session.get(ShadowAccount, "baseline")
        assert acc.frozen_params["risk_per_trade_pct"] == 0.7
        assert acc.frozen_params["trigger_min_score"] == 80


@pytest.mark.asyncio
async def test_deposit_into_the_live_account_does_not_lift_the_main_return(shadows):
    from sqlalchemy import delete
    from src.db.models import CashFlow, ShadowAccount
    enable, reset_all = shadows
    enable(["baseline"])
    await reset_all()
    # The first look sets the main baseline at €10,000
    await shadow_portfolios.get_comparison({"total_value_eur": 10000.0, "trading_mode": "LIVE"}, True)
    async with AsyncSessionLocal() as session:
        acc = await session.get(ShadowAccount, "baseline")
        assert acc.main_value_at_start_eur == 10000.0
        session.add(CashFlow(amount_eur=2000.0, source="manual", is_live=True,
                             timestamp=acc.started_at + datetime.timedelta(minutes=5)))
        await session.commit()
    try:
        # €2,000 deposited and €100 earned: the main bot is up 1%, not 21%
        comparison = await shadow_portfolios.get_comparison({"total_value_eur": 12100.0, "trading_mode": "LIVE"}, True)
        assert comparison["strategies"][0]["main_same_period"]["return_pct"] == 1.0
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(CashFlow))
            await session.commit()
