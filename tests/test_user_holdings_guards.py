"""Guards: the bot never buys, arms a trigger on, or scans a ticker the user marked as Mine."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import delete, select

from config.settings import settings
from src.db.models import OrderTrigger, UserHolding
from src.db.session import AsyncSessionLocal, init_db
from src.trading.live import LiveBroker
from src.trading.trigger_gate import precheck_manual_trigger
from tests.test_trading212_broker import FakeT212, _live, clean_snapshots, fake  # noqa: F401

MINE = "MINEGRD"
REFUSAL = "MINEGRD is marked as yours. Flip it to Bot on the Home tab first."
STOCK = {"ticker": MINE, "name": "Ko Hold", "price": 50.0, "currency": "EUR", "quote_type": "EQUITY"}


@pytest.fixture
async def mine_row():
    await init_db()
    async with AsyncSessionLocal() as s:
        s.add(UserHolding(ticker=MINE, t212_ticker="MINEGRD_EQ", name=MINE, currency="EUR", shares=2.0,
                          avg_price_native=40.0, current_price_native=50.0, current_price_eur=50.0, cost_basis_eur=80.0))
        await s.commit()
    try:
        yield
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(UserHolding).where(UserHolding.ticker == MINE))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_([MINE, "KOGUARDB"])))
            await s.commit()


async def test_execute_buy_refuses_mine_ticker_manual_and_auto(fake, monkeypatch, mine_row):
    _live(monkeypatch)
    for manual in (True, False):
        res = await LiveBroker().execute_buy("minegrd", "Ko Hold", 50.0, "EUR", 45.0, 60.0, 200.0, 90, "test", is_manual=manual)
        assert res == {"success": False, "message": REFUSAL}
    assert fake.posts == []


async def test_create_trigger_refuses_mine_ticker(fake, monkeypatch, mine_row):
    _live(monkeypatch)
    res = await LiveBroker().create_price_trigger(MINE, "Ko Hold", "EUR", "LIMIT_PULLBACK", 48.0, 50.0, 45.0, 60.0, 100.0, 90, "r")
    assert res == {"success": False, "message": REFUSAL}
    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(OrderTrigger).where(OrderTrigger.ticker == MINE))).scalars().first() is None


async def test_manual_trigger_precheck_refuses_mine(fake, monkeypatch, mine_row):
    _live(monkeypatch)
    res = await precheck_manual_trigger(MINE, STOCK)
    assert res.code == "MINE" and res.message == REFUSAL


async def test_pending_trigger_on_mine_ticker_is_skipped(fake, monkeypatch, mine_row):
    _live(monkeypatch)
    async with AsyncSessionLocal() as s:
        s.add(OrderTrigger(ticker=MINE, name=MINE, currency="EUR", trigger_type="LIMIT_PULLBACK", target_price=48.0,
                           current_price_at_creation=50.0, stop_loss=45.0, take_profit=60.0, allocation_eur=100.0,
                           consensus_score=90, status="PENDING", is_live=True))
        await s.commit()
    broker = LiveBroker()
    buy = AsyncMock(return_value={"success": True})
    with patch.object(broker, "adjust_triggers_dynamically", AsyncMock()), \
         patch.object(broker, "execute_buy", buy), \
         patch("src.trading.live.market_data_provider.get_stock_data", MagicMock(return_value={"price": 47.0})):
        assert await broker.check_and_execute_triggers() == []
    buy.assert_not_awaited()
    assert fake.posts == []
    async with AsyncSessionLocal() as s:
        t = (await s.execute(select(OrderTrigger).where(OrderTrigger.ticker == MINE))).scalars().one()
        assert t.status == "PENDING"


async def test_scanner_drops_mine_candidates(fake, monkeypatch, mine_row):
    from src.scheduler.jobs import trading_scheduler
    _live(monkeypatch)
    cands = [{"ticker": MINE, "price": 50.0, "currency": "EUR"}, {"ticker": "KOGUARDB", "price": 50.0, "currency": "EUR"}]
    fetch = MagicMock(side_effect=lambda tk: dict(STOCK, ticker=tk))
    committee = AsyncMock(side_effect=RuntimeError("stop after the committee call"))
    broker = AsyncMock(**{"get_portfolio_summary.return_value": {"total_value_eur": 1000.0}})
    with patch("src.scheduler.jobs.market_scanner.load_watchlist_tickers", AsyncMock(return_value=[])), \
         patch("src.scheduler.jobs.market_scanner.scan_universe", MagicMock(return_value=cands)), \
         patch("src.scheduler.jobs.market_data_provider.get_stock_data", fetch), \
         patch("src.scheduler.jobs.investment_committee.evaluate_ticker", committee), \
         patch("src.scheduler.jobs.shadow_portfolios.follow_report", AsyncMock()), \
         patch("src.scheduler.jobs.paper_broker", broker), \
         patch("src.scheduler.jobs.telegram_bot", AsyncMock()), \
         patch("src.scheduler.jobs.review_tracker.compare_past_week_reviews", AsyncMock(return_value={})), \
         patch("src.scheduler.jobs.review_tracker.save_session_review", AsyncMock()):
        await trading_scheduler.run_market_session(market="EU", session="OPEN")
    assert [c.args[0] for c in fetch.call_args_list] == ["KOGUARDB"]
    assert [c.args[0]["ticker"] for c in committee.call_args_list] == ["KOGUARDB"]


async def test_guards_off_in_paper_mode(mine_row):
    assert settings.TRADING_MODE == "PAPER"
    res = await precheck_manual_trigger(MINE, STOCK)
    assert res is None or res.code != "MINE"
