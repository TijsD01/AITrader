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
    settings.LIVE_BROKER = "trading212"

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
             patch.object(live_broker, "_dispatch_real_order", side_effect=lambda **kw: ("t212_1001", kw["shares"], kw["price"])), \
             patch.object(live_broker, "_dispatch_real_close", return_value="t212_exit_1002"):
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
            assert buy_res["broker"] == "TRADING212"
            assert buy_res["broker_order_id"].startswith("t212_")

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
            assert summary["broker"] == "TRADING212"

            # 4. Execute live sell
            sell_res = await live_broker.execute_sell(ticker, reason="Profit Target Hit")
            assert sell_res["success"] is True
            assert sell_res["is_live"] is True
            assert sell_res["broker_order_id"].startswith("t212_exit_")

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
        with patch("src.trading.live.market_data_provider.get_stock_data", return_value={"price": 95.0}):
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
    settings.LIVE_BROKER = "trading212"
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
async def test_portfolio_summary_uses_broker_values():
    """Dashboard total/equity come from the Trading 212 account summary, not local quotes."""
    from unittest.mock import AsyncMock
    await init_db()
    async with AsyncSessionLocal() as session:
        await session.execute(delete(Position).where(Position.is_live == True))
        await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
        session.add(PortfolioSnapshot(cash_eur=100.0, equity_eur=300.0, total_value_eur=400.0, daily_pnl_eur=0.0,
                                      daily_pnl_pct=0.0, open_positions_count=0, is_live=True))
        await session.commit()

    try:
        with patch.object(settings, "LIVE_BROKER", "trading212"),              patch.object(live_broker, "_fetch_t212_account_values", AsyncMock(return_value=(100.0, 431.69, 331.69))):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "trading212"
            assert s["total_value_eur"] == 431.69 and s["equity_eur"] == 331.69
        # Broker down: falls back to the stored snapshot
        with patch.object(settings, "LIVE_BROKER", "trading212"),              patch.object(live_broker, "_fetch_t212_account_values", AsyncMock(side_effect=ConnectionError)):
            s = await live_broker.get_portfolio_summary()
            assert s["value_source"] == "snapshot" and s["total_value_eur"] == 400.0
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.commit()


@pytest.mark.asyncio
async def test_cash_capped_at_broker_buying_power():
    """Buys may only spend up to the broker's buying power (T212 availableToTrade) once it is known."""
    live_broker._available_funds_eur = None
    try:
        assert live_broker._spendable_cash(500.0) == 500.0  # buying power not seen yet
        live_broker._available_funds_eur = 320.0
        assert live_broker._spendable_cash(500.0) == 320.0
        assert live_broker._spendable_cash(200.0) == 200.0
    finally:
        live_broker._available_funds_eur = None


@pytest.mark.asyncio
async def test_sync_keeps_cost_basis_and_adopts_hand_bought_holdings():
    """Sync must not re-convert an existing position's cost at today's FX; a hand-bought holding gets no stop/target."""
    await init_db()
    tickers = ("SYNCKEEP", "SYNCADOPT")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Position).where(Position.ticker.in_(tickers)))
            keep = Position(ticker="SYNCKEEP", name="k", currency="USD", shares=2.0, entry_price_native=100.0,
                            current_price_native=100.0, highest_price_native=100.0, entry_price_eur=90.0,
                            current_price_eur=90.0, cost_basis_eur=183.0, status="OPEN", is_live=True)
            session.add(keep)
            await session.flush()
            with patch("src.trading.live.currency_service.to_eur", side_effect=lambda v, c: v * 0.5):
                await live_broker._update_tracked_holding(session, keep, "SYNCKEEP", 2.0, 100.0, "USD")
                await live_broker._adopt_holding(session, "SYNCADOPT", "SYNCADOPT", "USD", 1.0, 200.0, 0.5, [], [])
            await session.commit()

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
async def test_broker_switch_starts_a_new_history_for_loss_and_return():
    import datetime as dt
    from unittest.mock import AsyncMock
    from src.db.models import CashFlow
    await init_db()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.execute(delete(CashFlow).where(CashFlow.is_live == True))
            # The old IBKR account (and untagged rows from before the broker column) was worth €10,000
            legacy = PortfolioSnapshot(cash_eur=10000.0, equity_eur=0.0, total_value_eur=10000.0, is_live=True,
                                       timestamp=now - dt.timedelta(days=3))
            session.add(legacy)
            await session.flush()
            legacy.broker = None  # rows written before the column existed are NULL (an insert would apply the default)
            session.add(PortfolioSnapshot(cash_eur=10000.0, equity_eur=0.0, total_value_eur=10000.0, is_live=True,
                                          broker="ibkr", timestamp=now - dt.timedelta(days=1)))
            # The Trading 212 account starts with €2,000, funded by a deposit before its first snapshot
            session.add(CashFlow(amount_eur=2000.0, source="broker", is_live=True, timestamp=now - dt.timedelta(hours=2)))
            session.add(PortfolioSnapshot(cash_eur=2000.0, equity_eur=0.0, total_value_eur=2000.0, is_live=True,
                                          broker="trading212", timestamp=now - dt.timedelta(hours=1)))
            await session.commit()
        with patch.object(settings, "LIVE_BROKER", "trading212"), \
             patch.object(live_broker, "_fetch_t212_account_values", AsyncMock(return_value=(2000.0, 2000.0, 0.0))), \
             patch("src.trading.live.scan_live_cash_flows", AsyncMock(return_value=0)), \
             patch.object(live_broker, "refresh_positions_pnl", AsyncMock()), \
             patch.object(live_broker, "adjust_triggers_dynamically", AsyncMock(return_value=[])):
            assert await live_broker.get_daily_loss_eur() == 0.0  # not €8,000
            summary = await live_broker.get_portfolio_summary()
        assert summary["initial_capital_eur"] == 2000.0
        assert summary["net_deposits_eur"] == 0.0  # the funding deposit is inside the start value
        assert summary["total_gain_eur"] == 0.0
        # Only untagged rows before it (the switch happened before the broker column existed): still a new history
        from src.trading.cash_flows import live_history_start
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.broker == "ibkr"))
            await session.commit()
            with patch.object(settings, "LIVE_BROKER", "trading212"):
                assert await live_history_start(session) == now - dt.timedelta(hours=1)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True))
            await session.execute(delete(CashFlow).where(CashFlow.is_live == True))
            await session.commit()


async def _live_trigger(ticker, broker_order_id=None):
    from src.db.models import OrderTrigger
    async with AsyncSessionLocal() as session:
        await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
        t = OrderTrigger(
            ticker=ticker, name=ticker, currency="EUR", trigger_type="LIMIT_PULLBACK",
            target_price=90.0, current_price_at_creation=100.0, stop_loss=85.0, take_profit=110.0,
            allocation_eur=500.0, consensus_score=85, status="PENDING", is_live=True,
            broker_order_id=broker_order_id,
        )
        session.add(t)
        await session.commit()
        return t.id


@pytest.mark.asyncio
async def test_live_cancel_unconfirmed_leaves_trigger_pending_without_reason():
    from unittest.mock import AsyncMock, MagicMock
    from src.db.models import OrderTrigger
    await init_db()
    ticker = "CANCEL_UNCONF_TEST"
    trigger_id = await _live_trigger(ticker, broker_order_id="t212_999")
    try:
        # _t212_cancel_entry returns no fills and keeps broker_order_id when the cancel is not confirmed
        with patch("src.trading.live.trading212.get_client", return_value=MagicMock()), \
             patch.object(live_broker, "_t212_cancel_entry", AsyncMock(return_value=[])):
            async with AsyncSessionLocal() as session:
                trigger = await session.get(OrderTrigger, trigger_id)
                res = await live_broker._cancel_trigger_locked(session, trigger)
        assert res["success"] is False
        async with AsyncSessionLocal() as session:
            row = await session.get(OrderTrigger, trigger_id)
        assert row.status == "PENDING"
        assert row.broker_order_id == "t212_999"
        assert row.cancel_reason is None
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
            await session.commit()


@pytest.mark.asyncio
async def test_live_cancel_records_reason():
    from src.db.models import OrderTrigger
    await init_db()
    ticker = "CANCEL_REASON_TEST"
    try:
        for reason_kwargs, expected in (({}, "Cancelled by user"), ({"reason": "Ticker released to user"}, "Ticker released to user")):
            trigger_id = await _live_trigger(ticker)
            async with AsyncSessionLocal() as session:
                trigger = await session.get(OrderTrigger, trigger_id)
                res = await live_broker._cancel_trigger_locked(session, trigger, **reason_kwargs)
            assert res["success"] is True
            async with AsyncSessionLocal() as session:
                row = await session.get(OrderTrigger, trigger_id)
            assert (row.status, row.cancel_reason) == ("CANCELLED", expected)
    finally:
        async with AsyncSessionLocal() as session:
            await session.execute(delete(OrderTrigger).where(OrderTrigger.ticker == ticker))
            await session.commit()
