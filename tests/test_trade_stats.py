import re
import time
from unittest.mock import patch

from sqlalchemy import delete, select, func

from src.db import session as db_session
from src.db.session import init_db
from src.db.models import OrderTrigger, Position, Trade, PortfolioSnapshot
from src.trading.trade_stats import summarize_pct, summarize_r, track_record_block


def test_pct_below_min_n():
    assert summarize_pct([1.0] * 29) == "All trades: n=29, below 30, too few to judge"


def test_pct_stats():
    line = summarize_pct([2.0] * 12 + [-1.0] * 18)
    assert line == "All trades: n=30, win 40%, avg win +2.00%, avg loss -1.00%, expectancy +0.20% per trade"


def test_pct_zero_counts_as_loss_and_empty_side_shows_zero():
    assert summarize_pct([0.0] * 30) == (
        "All trades: n=30, win 0%, avg win +0.00%, avg loss +0.00%, expectancy +0.00% per trade"
    )
    assert summarize_pct([1.5] * 30) == (
        "All trades: n=30, win 100%, avg win +1.50%, avg loss +0.00%, expectancy +1.50% per trade"
    )


def test_r_skips_bad_stops_and_groups():
    rows = [("LIMIT_PULLBACK", 100.0, 110.0, 90.0)] * 30 + [("LIMIT_PULLBACK", 100.0, 90.0, 100.0), ("BREAKOUT_BUY", 100.0, 95.0, None)]
    assert summarize_r(rows) == ["LIMIT_PULLBACK: n=30, win 100%, avg R +1.00", "BREAKOUT_BUY: n=0, below 30, too few to judge"]


def _all_trades_n(block: str) -> int:
    return int(re.search(r"All trades: n=(\d+)", block).group(1))


async def test_track_record_block_reads_closed_trades():
    await init_db()
    before = await track_record_block()
    ttype = f"TST{int(time.time() * 1000) % 10**12}"
    ticker = f"TRK_{int(time.time() * 1000)}"
    async with db_session.AsyncSessionLocal() as s:
        trig = OrderTrigger(
            ticker=ticker, currency="USD", trigger_type=ttype, target_price=100.0,
            current_price_at_creation=105.0, stop_loss=90.0, take_profit=120.0,
            allocation_eur=100.0, status="TRIGGERED",
        )
        s.add(trig)
        await s.flush()
        pos = Position(
            ticker=ticker, currency="USD", shares=1.0, entry_price_native=100.0, current_price_native=105.0,
            highest_price_native=105.0, stop_loss_native=90.0, entry_price_eur=100.0, current_price_eur=105.0,
            cost_basis_eur=100.0, status="CLOSED", trigger_id=trig.id,
        )
        trade = Trade(
            ticker=ticker, order_type="SELL", shares=1.0, currency="USD", price_native=105.0,
            price_eur=105.0, total_eur=105.0, realized_pnl_eur=5.0, realized_pnl_pct=5.0,
        )
        s.add_all([pos, trade])
        await s.commit()
        ids = (trig.id, pos.id, trade.id)
    try:
        block = await track_record_block()
        assert block.startswith("=== TRACK RECORD (closed bot trades, numbers only) ===")
        assert _all_trades_n(block) == _all_trades_n(before) + 1
        # One closed position with entry 100, exit 105, stop 90 -> R = +0.50, below MIN_N
        assert f"{ttype}: n=1, below 30, too few to judge" in block.splitlines()
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(Trade).where(Trade.id == ids[2]))
            await s.execute(delete(Position).where(Position.id == ids[1]))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.id == ids[0]))
            await s.commit()


async def test_paper_trigger_stores_report_id_and_position_links_trigger():
    from src.trading.paper import paper_broker

    await init_db()
    async with db_session.AsyncSessionLocal() as s:
        max_snap = (await s.execute(select(func.coalesce(func.max(PortfolioSnapshot.id), 0)))).scalar()
    ticker = f"LNK_{int(time.time() * 1000)}"
    try:
        res = await paper_broker.create_price_trigger(
            ticker=ticker, name="Link Corp", currency="USD", trigger_type="LIMIT_PULLBACK",
            target_price=80.0, current_price_at_creation=85.0, stop_loss=75.0, take_profit=95.0,
            allocation_eur=500.0, consensus_score=82, ai_rationale="Link test", report_id=7,
        )
        assert res["success"] is True
        trigger_id = res["trigger_id"]
        async with db_session.AsyncSessionLocal() as s:
            trig = (await s.execute(select(OrderTrigger).where(OrderTrigger.id == trigger_id))).scalars().one()
            assert trig.report_id == 7

        quote = {"ticker": ticker, "price": 79.5, "currency": "USD", "name": "Link Corp"}
        with patch("src.trading.paper.check_market_hours", return_value=(True, "Market is OPEN")), \
             patch("src.trading.paper.market_data_provider.get_stock_data",
                   side_effect=lambda sym: quote if sym == ticker else None), \
             patch("src.trading.risk.check_market_hours", return_value=(True, "Market is OPEN")):
            executed = await paper_broker.check_and_execute_triggers()
        assert any(t.get("trigger_id") == trigger_id and t.get("success") for t in executed)

        async with db_session.AsyncSessionLocal() as s:
            pos = (await s.execute(select(Position).where(Position.ticker == ticker))).scalars().one()
            assert pos.trigger_id == trigger_id
    finally:
        async with db_session.AsyncSessionLocal() as s:
            await s.execute(delete(Trade).where(Trade.ticker == ticker))
            await s.execute(delete(Position).where(Position.ticker == ticker))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
            await s.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id > max_snap))
            await s.commit()


async def test_committee_prompt_gets_track_record_not_cold_start():
    from unittest.mock import AsyncMock
    from src.agents.base import llm_client
    from src.agents.committee import investment_committee
    from src.memory.client import agent_memory_client

    stock = {
        "ticker": "NVDA", "name": "NVIDIA", "price": 120.0, "currency": "USD",
        "technicals": {"price": 120.0, "ema20": 118.0, "ema50": 114.0, "sma200": 105.0, "above_ema20": True,
                       "above_ema50": True, "above_sma200": True, "rsi14": 58.0, "macd": 1.5, "macd_signal": 1.2,
                       "macd_hist": 0.3, "atr14": 3.5, "support_20d": 115.0, "resistance_20d": 125.0,
                       "return_5d_pct": 2.4, "return_1m_pct": 6.8, "volume_surge_ratio": 1.1},
        "fundamentals": {"beta": 1.2, "market_cap": 2500000000000},
    }
    gen = AsyncMock(return_value={})
    with patch("src.trading.trade_stats.track_record_block", AsyncMock(return_value="TRACK RECORD SENTINEL")), \
         patch.object(agent_memory_client, "get_cold_start_loadout", AsyncMock()) as cold, \
         patch.object(llm_client, "has_active_key", return_value=True), \
         patch.object(llm_client, "generate_json", gen):
        await investment_committee.evaluate_ticker(stock, portfolio_equity_eur=10000.0)
    committee_prompts = [c.args[1] for c in gen.call_args_list if "Analyst Reports:" in c.args[1]]
    assert len(committee_prompts) == 1
    assert "TRACK RECORD SENTINEL" in committee_prompts[0]
    cold.assert_not_called()
