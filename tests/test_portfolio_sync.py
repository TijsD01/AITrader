import pytest
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from src.web.app import app
from src.db.session import init_db
from src.trading.paper import paper_broker
from config.settings import settings

@pytest.mark.asyncio
async def test_portfolio_summary_refreshes_unrealized_pnl():
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    transport = ASGITransport(app=app)
    try:
        await init_db()
        settings.ENFORCE_MARKET_HOURS = False  # after init_db, which reloads the stored value

        # Buy position at 100 EUR
        await paper_broker.execute_buy(
            ticker="SYNCTEST.DE",
            name="Sync Test",
            price_native=100.0,
            currency="EUR",
            stop_loss_native=90.0,
            take_profit_native=120.0,
            allocation_eur=500.0,
            consensus_score=85,
            rationale="Test Sync"
        )

        # Price moves to 110 EUR (+10 EUR per share, ~5 shares = +50 EUR)
        def mock_stock_data(ticker):
            if ticker == "SYNCTEST.DE":
                return {"price": 110.0, "currency": "EUR"}
            return None

        with patch("src.trading.paper.market_data_provider.get_stock_data", side_effect=mock_stock_data):
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                # Direct call to /api/portfolio MUST have refreshed unrealized PnL
                port_resp = await ac.get("/api/portfolio")
                assert port_resp.status_code == 200
                port_data = port_resp.json()

                # Get positions
                pos_resp = await ac.get("/api/positions")
                assert pos_resp.status_code == 200
                pos_data = pos_resp.json()

                # Find the position
                pos = next((p for p in pos_data if p["ticker"] == "SYNCTEST.DE"), None)
                assert pos is not None
                assert pos["unrealized_pnl_eur"] > 0, "Position unrealized PnL should be positive"

                # Summary unrealized PnL MUST equal sum of all open positions
                total_pos_pnl = round(sum(p["unrealized_pnl_eur"] for p in pos_data), 2)
                assert port_data["unrealized_pnl_eur"] == total_pos_pnl, (
                    f"Summary unrealized PnL ({port_data['unrealized_pnl_eur']}) did not match "
                    f"sum of positions ({total_pos_pnl})"
                )

                # Summary equity MUST equal sum of position values
                total_pos_equity = round(sum(p["current_value_eur"] for p in pos_data), 2)
                assert port_data["equity_eur"] == total_pos_equity, (
                    f"Summary equity ({port_data['equity_eur']}) did not match "
                    f"sum of position current values ({total_pos_equity})"
                )
    finally:
        with patch("src.trading.paper.market_data_provider.get_stock_data", return_value={"price": 100.0, "currency": "EUR", "name": "Sync Test"}):
            await paper_broker.execute_sell("SYNCTEST.DE", reason="cleanup")
        settings.ENFORCE_MARKET_HOURS = orig_enforce


@pytest.mark.asyncio
async def test_live_sync_fills_levels_from_latest_trigger():
    from sqlalchemy import select, delete
    from src.db.models import Position, OrderTrigger
    from src.db.session import AsyncSessionLocal
    from src.trading.live import LiveBroker

    await init_db()
    async with AsyncSessionLocal() as s:
        s.add_all([
            OrderTrigger(ticker=t, target_price=221.0, current_price_at_creation=225.0, stop_loss=215.02,
                         take_profit=230.18, allocation_eur=200.0, status="TRIGGERED", is_live=True)
            for t in ("SYNCNEW", "SYNCOLD")
        ])
        s.add(Position(ticker="SYNCOLD", name="SYNCOLD", currency="USD", shares=1.0, entry_price_native=221.37,
                       current_price_native=221.37, highest_price_native=221.37, entry_price_eur=200.0,
                       current_price_eur=200.0, cost_basis_eur=200.0, status="OPEN", is_live=True))
        await s.commit()
    broker = LiveBroker()
    try:
        # The two helpers a broker sync runs per holding: tracked (SYNCOLD) and adopted (SYNCNEW)
        async with AsyncSessionLocal() as s:
            old = (await s.execute(select(Position).where(Position.ticker == "SYNCOLD"))).scalars().first()
            await broker._update_tracked_holding(s, old, "SYNCOLD", 1.0, 221.37, "USD")
            await broker._adopt_holding(s, "SYNCNEW", "SYNCNEW", "USD", 1.0, 221.37, 0.0, [], [])
            await s.commit()
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(Position).where(Position.ticker.in_(["SYNCNEW", "SYNCOLD"]), Position.is_live == True))).scalars().all()
            assert {p.ticker: (p.stop_loss_native, p.take_profit_native) for p in rows} == {
                "SYNCNEW": (215.02, 230.18), "SYNCOLD": (215.02, 230.18)}
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Position).where(Position.ticker.in_(["SYNCNEW", "SYNCOLD"])))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_(["SYNCNEW", "SYNCOLD"])))
            await s.commit()


@pytest.mark.asyncio
async def test_live_sync_never_backfills_take_profit_under_promoted_rules():
    """A position opened under promoted rules without a target keeps none; an older trigger's target is stale."""
    from sqlalchemy import select, delete
    from src.db.models import Position, OrderTrigger
    from src.db.session import AsyncSessionLocal
    from src.trading.live import LiveBroker

    await init_db()
    async with AsyncSessionLocal() as s:
        s.add_all([
            OrderTrigger(ticker=t, target_price=221.0, current_price_at_creation=225.0, stop_loss=215.02,
                         take_profit=230.18, allocation_eur=200.0, status="TRIGGERED", is_live=True)
            for t in ("SYNCRV", "SYNCNORV")
        ])
        s.add_all([
            Position(ticker=t, name=t, currency="USD", shares=1.0, entry_price_native=221.37,
                     current_price_native=221.37, highest_price_native=221.37, entry_price_eur=200.0,
                     current_price_eur=200.0, cost_basis_eur=200.0, status="OPEN", is_live=True, rules_version=rv)
            for t, rv in (("SYNCRV", 3), ("SYNCNORV", None))
        ])
        await s.commit()
    broker = LiveBroker()
    try:
        async with AsyncSessionLocal() as s:
            for t in ("SYNCRV", "SYNCNORV"):
                pos = (await s.execute(select(Position).where(Position.ticker == t))).scalars().first()
                await broker._update_tracked_holding(s, pos, t, 1.0, 221.37, "USD")
            await s.commit()
        async with AsyncSessionLocal() as s:
            rows = (await s.execute(select(Position).where(Position.ticker.in_(["SYNCRV", "SYNCNORV"])))).scalars().all()
            assert {p.ticker: (p.stop_loss_native, p.take_profit_native) for p in rows} == {
                "SYNCRV": (215.02, None), "SYNCNORV": (215.02, 230.18)}
    finally:
        async with AsyncSessionLocal() as s:
            await s.execute(delete(Position).where(Position.ticker.in_(["SYNCRV", "SYNCNORV"])))
            await s.execute(delete(OrderTrigger).where(OrderTrigger.ticker.in_(["SYNCRV", "SYNCNORV"])))
            await s.commit()
