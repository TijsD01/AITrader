import pytest
from unittest.mock import patch
from sqlalchemy import delete, select
from config.settings import settings
from src.db.session import init_db, AsyncSessionLocal
from src.db.models import Trade, Position, PortfolioSnapshot
from src.trading.live import live_broker
from src.trading.risk import calculate_position_allocation, risk_guardrail

def test_full_budget_utilization_allocation():
    """Verify full budget deployment across available slots."""
    # Scenario A: €1,000 available cash, 0 open positions, max 4 positions
    alloc = calculate_position_allocation(
        available_cash=1000.0,
        current_positions_count=0,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2500.0
    )
    # 1000 / 4 = 250 EUR
    assert alloc == 250.0

    # Scenario B: €600 available cash, 3 open positions, max 4 positions (ONLY 1 slot left)
    # MUST deploy 100% of the available remaining cash
    alloc_last_slot = calculate_position_allocation(
        available_cash=600.0,
        current_positions_count=3,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2500.0
    )
    assert alloc_last_slot == 600.0

    # Scenario C: Single order cap enforcement
    alloc_capped = calculate_position_allocation(
        available_cash=5000.0,
        current_positions_count=3,
        full_budget_utilization=True,
        max_positions=4,
        max_single_order_eur=2000.0
    )
    assert alloc_capped == 2000.0

    # Scenario D: When full_budget_utilization is False, uses traditional conservative 8%
    alloc_conservative = calculate_position_allocation(
        available_cash=1000.0,
        current_positions_count=0,
        full_budget_utilization=False,
        recommended_size_pct=0.08
    )
    assert alloc_conservative == 80.0

@pytest.mark.asyncio
async def test_live_broker_safety_interlocks():
    """Verify live broker blocks buys when safety interlocks or kill switches are active."""
    await init_db()
    orig_mode = settings.TRADING_MODE
    orig_live = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    settings.ENFORCE_MARKET_HOURS = False

    try:
        # 1. LIVE_TRADING_ENABLED is False -> MUST block real money orders
        settings.TRADING_MODE = "LIVE"
        settings.LIVE_TRADING_ENABLED = False
        settings.KILL_SWITCH_ACTIVE = False

        res = await live_broker.execute_buy(
            ticker="AAPL",
            name="Apple Inc",
            price_native=220.0,
            currency="USD",
            stop_loss_native=210.0,
            take_profit_native=240.0,
            allocation_eur=500.0,
            consensus_score=85,
            rationale="Test Interlock"
        )
        assert res["success"] is False
        assert "LIVE_TRADING_ENABLED is False" in res["message"]

        # 2. Emergency Kill Switch is ACTIVE -> MUST block real money orders
        settings.LIVE_TRADING_ENABLED = True
        settings.KILL_SWITCH_ACTIVE = True

        res_kill = await live_broker.execute_buy(
            ticker="AAPL",
            name="Apple Inc",
            price_native=220.0,
            currency="USD",
            stop_loss_native=210.0,
            take_profit_native=240.0,
            allocation_eur=500.0,
            consensus_score=85,
            rationale="Test Kill Switch"
        )
        assert res_kill["success"] is False
        assert "Emergency kill switch is ACTIVE" in res_kill["message"]
    finally:
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.ENFORCE_MARKET_HOURS = orig_enforce

@pytest.mark.asyncio
async def test_live_broker_full_execution_lifecycle():
    """Verify live order execution logs live trades with broker order IDs."""
    await init_db()
    orig_mode = settings.TRADING_MODE
    orig_live = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    orig_broker = settings.LIVE_BROKER

    settings.TRADING_MODE = "LIVE"
    settings.LIVE_TRADING_ENABLED = True
    settings.KILL_SWITCH_ACTIVE = False
    settings.ENFORCE_MARKET_HOURS = False
    settings.LIVE_BROKER = "ibkr"

    ticker = "NVDA_LIVE_TEST"
    try:
        # Clean up any residual test position from previous runs and ensure live test snapshot exists
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True)
            )
            if not snap_res.scalars().first():
                session.add(PortfolioSnapshot(
                    cash_eur=1000.0,
                    equity_eur=0.0,
                    total_value_eur=1000.0,
                    is_live=True,
                    open_positions_count=0
                ))
            await session.commit()

        # Mock quote for NVDA_LIVE_TEST so it doesn't make slow network calls
        mock_quote = {"price": 120.0, "currency": "USD", "name": "NVIDIA Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote), \
             patch.object(live_broker, "_dispatch_real_order", side_effect=lambda **kw: ("ibkr_1001", kw["shares"], kw["price"])), \
             patch.object(live_broker, "_dispatch_real_close", return_value="ibkr_exit_1002"):
            # 1. Execute live buy
            buy_res = await live_broker.execute_buy(
                ticker=ticker,
                name="NVIDIA Corp",
                price_native=120.0,
                currency="USD",
                stop_loss_native=114.0,
                take_profit_native=132.0,
                allocation_eur=600.0,
                consensus_score=88,
                rationale="High conviction live trade test",
                is_manual=True
            )
            assert buy_res["success"] is True, buy_res
            assert buy_res["is_live"] is True
            assert buy_res["broker"] == "IBKR"
            assert buy_res["broker_order_id"].startswith("ibkr_")

            # 2. Check position record in database
            positions = await live_broker.get_positions()
            pos = next((p for p in positions if p["ticker"] == ticker), None)
            assert pos is not None
            assert pos["is_live"] is True
            assert pos["broker_order_id"] == buy_res["broker_order_id"]

            # 3. Check portfolio summary reflects live status
            summary = await live_broker.get_portfolio_summary()
            assert summary["trading_mode"] == "LIVE"
            assert summary["live_trading_enabled"] is True
            assert summary["broker"] == "IBKR"

            # 4. Execute live sell
            sell_res = await live_broker.execute_sell(ticker, reason="Profit Target Hit")
            assert sell_res["success"] is True
            assert sell_res["is_live"] is True
            assert sell_res["broker_order_id"].startswith("ibkr_exit_")

            # 5. Verify position is now closed
            positions_after = await live_broker.get_positions()
            assert not any(p["ticker"] == ticker for p in positions_after)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.commit()
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live
        settings.KILL_SWITCH_ACTIVE = orig_kill
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_live_broker_get_triggers_has_current_price_and_distance():
    """Verify live broker get_triggers returns current_price and distance_pct without undefined."""
    from src.db.models import OrderTrigger
    await init_db()

    ticker = "TRIG_LIVE_TEST"
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        t = OrderTrigger(
            ticker=ticker,
            name="Live Test Inc",
            currency="EUR",
            trigger_type="LIMIT_PULLBACK",
            target_price=90.0,
            current_price_at_creation=100.0,
            stop_loss=85.0,
            take_profit=110.0,
            allocation_eur=500.0,
            consensus_score=85,
            ai_rationale="Test pullback",
            status="PENDING",
            is_live=True
        )
        session.add(t)
        await session.commit()

    try:
        with patch.object(live_broker, "_fetch_ibkr_prices", return_value={ticker: 95.0}):
            triggers = await live_broker.get_triggers()
            found = next((tr for tr in triggers if tr["ticker"] == ticker), None)
            assert found is not None
            assert "current_price" in found
            assert found["current_price"] == 95.0
            assert "distance_pct" in found
            # ((90 / 95) - 1) * 100 = -5.26
            assert found["distance_pct"] == -5.26
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
            await session.commit()



@pytest.mark.asyncio
async def test_live_buy_not_recorded_when_broker_rejects():
    """A buy that never reached the broker must not create a position."""
    await init_db()
    orig = (settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE, settings.LIVE_BROKER)
    settings.TRADING_MODE = "LIVE"
    settings.LIVE_TRADING_ENABLED = True
    settings.KILL_SWITCH_ACTIVE = False
    settings.LIVE_BROKER = "ibkr"
    ticker = "REJ_LIVE_TEST"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            snap_res = await session.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            if not snap_res.scalars().first():
                session.add(PortfolioSnapshot(cash_eur=1000.0, equity_eur=0.0, total_value_eur=1000.0, is_live=True, open_positions_count=0))
            await session.commit()

        mock_quote = {"price": 120.0, "currency": "USD", "name": "Rejected Corp"}
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value=mock_quote), \
             patch.object(live_broker, "_dispatch_real_order", return_value=None):
            res = await live_broker.execute_buy(
                ticker=ticker, name="Rejected Corp", price_native=120.0, currency="USD",
                stop_loss_native=114.0, take_profit_native=132.0, allocation_eur=600.0,
                consensus_score=88, rationale="broker down", is_manual=True
            )
        assert res["success"] is False
        positions = await live_broker.get_positions()
        assert not any(p["ticker"] == ticker for p in positions)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.commit()
        settings.TRADING_MODE, settings.LIVE_TRADING_ENABLED, settings.KILL_SWITCH_ACTIVE, settings.LIVE_BROKER = orig


@pytest.mark.asyncio
async def test_portfolio_summary_uses_ibkr_values():
    """Dashboard total/equity come from IBKR account summary, not local quotes."""
    from types import SimpleNamespace as NS
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Position).where(Position.is_live == True))
        await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
        session.add(PortfolioSnapshot(cash_eur=100.0, equity_eur=300.0, total_value_eur=400.0, daily_pnl_eur=0.0,
                                      daily_pnl_pct=0.0, open_positions_count=0, is_live=True))
        await session.commit()

    class FakeIB:
        def isConnected(self):
            return True
        async def accountSummaryAsync(self):
            return [NS(tag="TotalCashValue", value="100.0", currency="EUR"),
                    NS(tag="NetLiquidation", value="431.69", currency="EUR"),
                    NS(tag="GrossPositionValue", value="331.69", currency="EUR")]

    try:
        with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(live_broker, "_get_ib", return_value=FakeIB()):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "ibkr"
            assert s["total_value_eur"] == 431.69 and s["equity_eur"] == 331.69
        # Gateway down: falls back to the stored snapshot
        with patch.object(settings, "LIVE_BROKER", "ibkr"), patch.object(live_broker, "_get_ib", side_effect=ConnectionError):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "snapshot" and s["total_value_eur"] == 400.0
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.commit()


@pytest.mark.asyncio
async def test_ibkr_cash_capped_at_available_funds():
    """Snapshots keep the real TotalCashValue; buys may only spend up to AvailableFunds."""
    from types import SimpleNamespace as NS

    class FakeIB:
        def __init__(self, rows):
            self.rows = rows
        async def accountSummaryAsync(self):
            return self.rows

    base = [NS(tag="TotalCashValue", value="500.0", currency="EUR"),
            NS(tag="NetLiquidation", value="900.0", currency="EUR"),
            NS(tag="GrossPositionValue", value="400.0", currency="EUR")]
    live_broker._available_funds_eur = None
    try:
        assert live_broker._spendable_cash(500.0) == 500.0  # AvailableFunds not seen yet
        values = await live_broker._fetch_ibkr_account_values(FakeIB(base + [NS(tag="AvailableFunds", value="320.0", currency="EUR")]))
        assert values == (500.0, 900.0, 400.0)
        assert live_broker._spendable_cash(500.0) == 320.0
        assert live_broker._spendable_cash(200.0) == 200.0
    finally:
        live_broker._available_funds_eur = None


@pytest.mark.asyncio
async def test_sync_keeps_cost_basis_and_adopts_with_fees():
    """Sync must not re-convert an existing position's cost at today's FX, and adoption books fees from fills."""
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock, MagicMock
    await init_db()
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"
    tickers = ("SYNCKEEP", "SYNCADOPT")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker.in_(tickers)))
            session.add(Position(ticker="SYNCKEEP", name="k", currency="USD", shares=2.0, entry_price_native=100.0,
                                 current_price_native=100.0, highest_price_native=100.0, entry_price_eur=90.0,
                                 current_price_eur=90.0, cost_basis_eur=183.0, status="OPEN", is_live=True))
            await session.commit()

        def ibpos(sym, n, avg, con):
            return NS(contract=NS(symbol=sym, currency="USD", conId=con), position=n, avgCost=avg)
        fill = NS(contract=NS(conId=2), execution=NS(side="BOT"), commissionReport=NS(commission=1.0, currency="USD"))
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value = [ibpos("SYNCKEEP", 2.0, 100.0, 1), ibpos("SYNCADOPT", 1.0, 200.0, 2)]
        ib.fills.return_value = [fill]
        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=ib)), \
             patch.object(live_broker, "_fetch_ibkr_account_values", AsyncMock(return_value=(None, None, None))), \
             patch.object(live_broker, "refresh_positions_pnl", AsyncMock()), \
             patch.object(live_broker, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0})), \
             patch("src.trading.live.currency_service.to_eur", side_effect=lambda v, c: v * 0.5):
            await live_broker.sync_account_state()

        async with AsyncSessionLocal() as session:
            rows = {p.ticker: p for p in (await session.execute(select(Position).where(Position.ticker.in_(tickers)))).scalars()}
        assert rows["SYNCKEEP"].cost_basis_eur == 183.0
        assert rows["SYNCADOPT"].cost_basis_eur == 100.0 and rows["SYNCADOPT"].fees_eur == 0.5
        # No bot trigger for SYNCADOPT: a hand-bought holding is tracked without stop/target, so the bot never sells it
        assert rows["SYNCADOPT"].stop_loss_native is None and rows["SYNCADOPT"].take_profit_native is None
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker.in_(tickers)))
            await session.commit()
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_sync_books_sell_for_position_the_broker_closed():
    """A bracket leg that filled at IBKR closes the local position with a SELL trade at the fill price, no new snapshot."""
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock, MagicMock
    await init_db()
    orig_broker = settings.LIVE_BROKER
    settings.LIVE_BROKER = "ibkr"
    ticker = "SYNCGONE"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.execute(delete(Trade).where(Trade.ticker == ticker))
            session.add(Position(ticker=ticker, name="g", currency="USD", shares=2.0, entry_price_native=100.0,
                                 current_price_native=100.0, highest_price_native=100.0, entry_price_eur=100.0,
                                 current_price_eur=100.0, cost_basis_eur=200.0, stop_loss_native=95.0, status="OPEN", is_live=True))
            await session.commit()
            snaps_before = len((await session.execute(select(PortfolioSnapshot))).scalars().all())

        sold = NS(contract=NS(symbol=ticker, currency="USD"), execution=NS(side="SLD", avgPrice=96.0, time=1))
        ib = MagicMock()
        ib.isConnected.return_value = True
        ib.positions.return_value = []
        ib.reqPositionsAsync = AsyncMock(return_value=[])
        ib.fills.return_value = [sold]
        with patch.object(live_broker, "_get_ib", AsyncMock(return_value=ib)), \
             patch.object(live_broker, "_fetch_ibkr_account_values", AsyncMock(return_value=(None, None, None))), \
             patch.object(live_broker, "refresh_positions_pnl", AsyncMock()), \
             patch.object(live_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])), \
             patch.object(live_broker, "_dispatch_real_close", AsyncMock(side_effect=AssertionError("must not send an order"))), \
             patch.object(live_broker, "get_portfolio_summary", AsyncMock(return_value={"cash_eur": 0, "equity_eur": 0, "total_value_eur": 0})), \
             patch("src.telegram.bot.telegram_bot.notify_position_closed", AsyncMock()), \
             patch("src.trading.live.currency_service.to_eur", side_effect=lambda v, c: v):
            await live_broker.sync_account_state()

        async with AsyncSessionLocal() as session:
            pos = (await session.execute(select(Position).where(Position.ticker == ticker))).scalars().first()
            sell = (await session.execute(select(Trade).where(Trade.ticker == ticker, Trade.order_type == "SELL"))).scalars().first()
            snaps_after = len((await session.execute(select(PortfolioSnapshot))).scalars().all())
        assert pos.status == "CLOSED"
        assert sell is not None and sell.price_native == 96.0 and sell.realized_pnl_eur < 0
        assert snaps_after == snaps_before
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.execute(delete(Trade).where(Trade.ticker == ticker))
            await session.commit()
        settings.LIVE_BROKER = orig_broker


@pytest.mark.asyncio
async def test_daily_loss_limit_counts_only_todays_drop_and_turns_on_kill_switch():
    import datetime as dt
    from unittest.mock import AsyncMock
    from src.db.models import CashFlow
    from src.db.session import set_kill_switch
    await init_db()
    ticker = "LOSSLIMIT"
    yesterday = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=1)

    async def set_price(eur):
        async with AsyncSessionLocal() as session:
            pos = (await session.execute(select(Position).where(Position.ticker == ticker))).scalars().first()
            pos.current_price_eur = eur
            await session.commit()

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.execute(delete(CashFlow).where(CashFlow.is_live == True))
            session.add(PortfolioSnapshot(cash_eur=0.0, equity_eur=10000.0, total_value_eur=10000.0, is_live=True, timestamp=yesterday))
            # Bought at €12,000: €2,000 under water since earlier days, which is not today's loss
            session.add(Position(ticker=ticker, name="l", currency="EUR", shares=1.0, entry_price_native=100.0,
                                 current_price_native=100.0, highest_price_native=100.0, entry_price_eur=12000.0,
                                 current_price_eur=10000.0, cost_basis_eur=12000.0, unrealized_pnl_eur=-2000.0,
                                 status="OPEN", is_live=True))
            await session.commit()
        await set_kill_switch(False)
        with patch.object(settings, "MAX_DAILY_LOSS_EUR", 500.0), patch.object(settings, "MAX_DAILY_LOSS_PCT", 3.0):
            # 3% of €10,000 = €300 limit
            assert live_broker.daily_loss_limit_eur(10000.0) == 300.0
            assert await live_broker.get_daily_loss_eur() == 0.0
            await set_price(9750.0)  # €250 down today stays under it
            assert await live_broker.enforce_daily_loss_limit() is None and not settings.KILL_SWITCH_ACTIVE
            # A €1,000 deposit today is not a gain that hides the loss, and no loss either
            async with AsyncSessionLocal() as session:
                session.add(PortfolioSnapshot(cash_eur=1000.0, equity_eur=9750.0, total_value_eur=10750.0, is_live=True))
                session.add(CashFlow(amount_eur=1000.0, is_live=True))
                await session.commit()
            assert await live_broker.get_daily_loss_eur() == 250.0
            await set_price(9600.0)  # €400 down: over the €322.50 limit (3% of €10,750)
            with patch("src.telegram.bot.telegram_bot.send_message", AsyncMock()) as alert:
                assert "kill switch on" in await live_broker.enforce_daily_loss_limit()
            assert settings.KILL_SWITCH_ACTIVE and alert.await_count == 1
    finally:
        await set_kill_switch(False)
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.execute(delete(Position).where(Position.ticker == ticker))
            await session.execute(delete(CashFlow).where(CashFlow.is_live == True))
            await session.commit()


@pytest.mark.asyncio
async def test_delayed_ibkr_quotes_are_skipped_for_the_yfinance_fallback():
    """A real-time quote is used; a delayed one (no subscription, e.g. US) is left out so callers fall back to yfinance."""
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    quotes = {"LIVEQ": (1, 100.0), "DELAYQ": (3, 50.0)}

    class FakeIB:
        def isConnected(self):
            return True
        def reqMarketDataType(self, t):
            pass
        async def qualifyContractsAsync(self, *cs):
            return list(cs)
        def reqMktData(self, c, *a):
            md, price = quotes[c.symbol]
            return NS(marketDataType=md, marketPrice=lambda: price, last=price, close=price)
        def cancelMktData(self, c):
            pass

    live_broker._ibkr_price_cache.clear()
    with patch.object(settings, "LIVE_BROKER", "ibkr"), \
         patch.object(live_broker, "_get_ib", AsyncMock(return_value=FakeIB())), \
         patch("asyncio.sleep", AsyncMock()):
        prices = await live_broker._fetch_ibkr_prices([("LIVEQ", "EUR"), ("DELAYQ", "USD")])
    live_broker._ibkr_price_cache.clear()
    assert prices == {"LIVEQ": 100.0}
