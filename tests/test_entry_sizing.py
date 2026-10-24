"""One sizing rule for new entries: Telegram alerts, the dashboard and the buy itself use the same slot,
risk cap and whole-share count, and cash left over after a buy goes to the next stock."""
import datetime
import time
from unittest.mock import patch

import pytest
from sqlalchemy import select, delete, func

from config.settings import settings
from src.db.models import OrderTrigger, PortfolioSnapshot, Position, Trade
from src.db.session import init_db, AsyncSessionLocal
from src.trading.paper import paper_broker
from src.trading.risk import (
    calculate_position_allocation,
    entry_allocation_from_summary,
    new_entry_allocation,
    plan_buy,
    risk_capped_allocation,
)


@pytest.fixture
def two_slots(monkeypatch):
    monkeypatch.setattr(settings, "MAX_POSITIONS", 2)
    monkeypatch.setattr(settings, "FULL_BUDGET_UTILIZATION", True)
    monkeypatch.setattr(settings, "WHOLE_SHARES_ONLY", True)
    monkeypatch.setattr(settings, "WHOLE_SHARE_ROUND_UP_PCT", 10.0)
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "slot")
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(settings, "DEFAULT_STOP_LOSS_PCT", 0.04)


def test_new_entry_gets_the_full_slot_while_another_trigger_is_pending(two_slots):
    # €500, 2 slots, one trigger already holds €250: the old arming formula halved the second slot
    assert calculate_position_allocation(available_cash=250.0, current_positions_count=0, full_budget_utilization=True) == 125.0
    assert new_entry_allocation(500.0, 250.0, 0) == 250.0
    # Both slots taken: nothing left for a third entry
    assert new_entry_allocation(500.0, 500.0, 0) == 0.0
    # An explicit amount is capped at the cash no trigger has reserved
    assert new_entry_allocation(500.0, 250.0, 0, requested_alloc=400.0) == 250.0


def test_entry_allocation_reads_a_portfolio_summary(two_slots):
    summary = {"usable_capital_eur": 500.0, "reserved_triggers_eur": 250.0, "available_cash_eur": 250.0, "open_positions_count": 0}
    assert entry_allocation_from_summary(summary) == 250.0
    # After one buy the remaining slot gets everything that is left
    summary = {"usable_capital_eur": 320.0, "reserved_triggers_eur": 0.0, "available_cash_eur": 320.0, "open_positions_count": 1}
    assert entry_allocation_from_summary(summary) == 320.0


def test_slot_risk_cap_keeps_the_slot_for_a_normal_stop(two_slots, monkeypatch):
    assert risk_capped_allocation(250.0, 100.0, 96.0, 500.0) == 250.0   # 4% stop: full slot
    assert risk_capped_allocation(250.0, 100.0, 92.0, 500.0) == 125.0   # 8% stop: half
    assert risk_capped_allocation(250.0, 100.0, 98.0, 500.0) == 250.0   # tighter stop never adds
    assert risk_capped_allocation(250.0, 100.0, None, 500.0) == 250.0
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "portfolio")
    assert risk_capped_allocation(250.0, 100.0, 96.0, 500.0) == 62.5    # 0.5% of €500 at a 4% stop
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "off")
    assert risk_capped_allocation(250.0, 100.0, 92.0, 500.0) == 250.0
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "slot")
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.0)
    assert risk_capped_allocation(250.0, 100.0, 92.0, 500.0) == 250.0


def test_slot_risk_cap_matches_half_a_percent_at_eight_slots(monkeypatch):
    # Default setup: €10,000 over 8 slots, 4% stop -> €1,250 either way
    monkeypatch.setattr(settings, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(settings, "DEFAULT_STOP_LOSS_PCT", 0.04)
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "slot")
    slot_capped = risk_capped_allocation(1250.0, 100.0, 92.0, 10000.0)
    monkeypatch.setattr(settings, "RISK_BUDGET_BASE", "portfolio")
    assert risk_capped_allocation(1250.0, 100.0, 92.0, 10000.0) == slot_capped == 625.0


def test_planned_buy_rounds_to_whole_shares_within_free_cash(two_slots):
    # €250 slot, €90 share, other slot reserved: a third share would eat into it
    plan = plan_buy(250.0, 90.0, "EUR", 86.4, 500.0, max_eur=250.0)
    assert (plan["shares"], plan["cost_eur"], plan["risk_capped"]) == (2.0, 180.0, False)
    # Manual buys skip the risk cap
    assert plan_buy(250.0, 90.0, "EUR", 45.0, 500.0, max_eur=250.0, automated=False)["shares"] == 2.0
    assert plan_buy(250.0, 90.0, "EUR", 45.0, 500.0, max_eur=250.0)["risk_capped"] is True


def _unique(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}"


@pytest.mark.asyncio
async def test_alert_dashboard_and_buy_agree_and_leftover_cash_moves_on(two_slots, monkeypatch):
    await init_db()
    first, second = _unique("SLOTA"), _unique("SLOTB")
    async with AsyncSessionLocal() as session:
        # Snapshots this test writes are removed again, so later tests see their own cash
        last_snapshot_id = (await session.execute(select(func.max(PortfolioSnapshot.id)))).scalar() or 0
        await session.execute(delete(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False))
        open_count = len((await session.execute(
            select(Position).where(Position.status == "OPEN", Position.is_live == False)
        )).scalars().all())
        session.add(PortfolioSnapshot(
            cash_eur=500.0, equity_eur=0.0, total_value_eur=500.0, open_positions_count=open_count, is_live=False,
            timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        ))
        await session.commit()
    monkeypatch.setattr(settings, "MAX_POSITIONS", open_count + 2)
    monkeypatch.setattr(settings, "MAX_CAPITAL_USAGE_PCT", 100.0)
    monkeypatch.setattr(settings, "MAX_DEPLOYABLE_CAPITAL_EUR", None)

    prices = {first: 92.0, second: 155.0}
    quote = lambda s: {"ticker": s, "price": prices[s], "currency": "EUR", "name": s} if s in prices else None

    async def arm(ticker, target, stop, take_profit):
        summary = await paper_broker.get_portfolio_summary()
        alloc = entry_allocation_from_summary(summary, ticker, "EUR")
        return await paper_broker.create_price_trigger(
            ticker=ticker, name=ticker, currency="EUR", trigger_type="LIMIT_PULLBACK",
            target_price=target, current_price_at_creation=prices[ticker], stop_loss=stop, take_profit=take_profit,
            allocation_eur=alloc, consensus_score=90, ai_rationale="Testing entry sizing"
        )

    try:
        with patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=quote):
            res_a = await arm(first, 90.0, 86.4, 100.0)
            res_b = await arm(second, 150.0, 144.0, 170.0)
            assert res_a["success"] and res_b["success"]
            # The alert for the second trigger shows the full slot, the same as the dashboard
            assert res_b["allocation_eur"] == 250.0
            shown = {t["ticker"]: t for t in await paper_broker.get_triggers(status="PENDING")}
            assert shown[second]["allocation_eur"] == res_b["allocation_eur"]
            assert shown[first]["planned_shares"] == 2.0      # 3 x €90 would eat into the other slot
            assert shown[second]["planned_shares"] == 1.0

            # The first stock fills at its limit: 2 shares for €180
            prices[first] = 90.0
            with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
                 patch("src.trading.paper.risk_guardrail.validate_buy_order", return_value=(True, "Approved")):
                executed = await paper_broker.check_and_execute_triggers()
            bought = [e for e in executed if e.get("ticker") == first]
            assert bought and bought[0]["shares"] == 2.0

            # The €70 the first stock did not use now goes to the second one: 2 shares at €150 instead of 1
            shown = {t["ticker"]: t for t in await paper_broker.get_triggers(status="PENDING")}
            assert shown[second]["allocation_eur"] > 300.0
            assert shown[second]["planned_shares"] == 2.0
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_([first, second])))
            await session.execute(delete(Position).where(Position.ticker.in_([first, second])))
            await session.execute(delete(Trade).where(Trade.ticker.in_([first, second])))
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id > last_snapshot_id))
            await session.commit()


@pytest.mark.asyncio
async def test_manual_trigger_gate_checks_the_share_after_the_risk_cap(two_slots):
    from src.trading.trigger_gate import evaluate_manual_trigger
    report = {"action": "BUY", "consensus_score": 90, "risk": {"approved": True}}
    stock = {"ticker": "GATECAP", "price": 205.0, "currency": "EUR", "name": "Gate Cap"}
    with patch("src.trading.trigger_gate.check_market_regime", return_value=(True, "")):
        # €250 fits one €200 share with a 4% stop. A 50% stop scales the budget to €20, but the risk cap only
        # trims: one share is still bought as long as it fits the free cash
        ok = await evaluate_manual_trigger("GATECAP", stock, report, 200.0, 250.0, 80, stop_loss=192.0, portfolio_value_eur=500.0, free_cash_eur=500.0)
        capped = await evaluate_manual_trigger("GATECAP", stock, report, 200.0, 250.0, 80, stop_loss=100.0, portfolio_value_eur=500.0, free_cash_eur=500.0)
        no_cash = await evaluate_manual_trigger("GATECAP", stock, report, 200.0, 250.0, 80, stop_loss=100.0, portfolio_value_eur=500.0, free_cash_eur=150.0)
    assert ok.allowed
    assert capped.allowed
    assert not no_cash.allowed and no_cash.code == "SHARE_TOO_EXPENSIVE" and "20.00" in no_cash.message
