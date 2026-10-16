import logging
import datetime
import asyncio
import time
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy import select, desc

from config.settings import settings
from src.db.models import Position, Trade, PortfolioSnapshot, SystemConfig, OrderTrigger
from src.db.session import AsyncSessionLocal
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.trading.risk import risk_guardrail, get_effective_deployable_cash, allocate_pending_triggers, evaluate_trigger_condition, is_trigger_expired, validate_trigger_levels, compute_trailing_stop, check_market_regime, whole_share_count, new_entry_allocation, risk_capped_allocation, plan_pending_triggers
from src.data.market_hours import check_market_hours
from src.trading.base import BaseBroker
from src.trading.cash_flows import scan_live_cash_flows, net_cash_flows_eur

logger = logging.getLogger(__name__)


def round_to_tick(price: float, increments: List[Tuple[float, float]]) -> float:
    """Rounds a price to the tick that applies at that price; increments is an IBKR market rule [(lowEdge, increment), ...] in ascending order."""
    tick = next((inc for low, inc in reversed(increments) if price >= low), 0.01)
    return round(round(price / tick) * tick, 6)

class LiveBroker(BaseBroker):
    """
    Live Real-Money Broker for the Antigravity Multi-Agent Trading System.
    Connects to Interactive Brokers (IBKR) or Alpaca to execute real orders,
    track real equity, sync balances, and enforce institutional risk controls.
    """

    def __init__(self):
        self._last_pnl_refresh: float = 0.0
        self._pnl_refresh_ttl: float = 10.0
        self._refresh_lock = asyncio.Lock()
        self._ib: Optional[Any] = None
        self._ibkr_price_cache: Dict[str, Tuple[float, float]] = {}
        self._last_connection_check: float = 0.0
        self._connection_check_ttl: float = 15.0  # 15s cache to prevent socket churn
        self._last_connection_status: Optional[Dict[str, Any]] = None
        self._connection_lock = asyncio.Lock()

    def _on_ib_disconnected(self):
        logger.warning("IBKR connection dropped.")
        self._last_connection_status = {
            "broker": "IBKR",
            "trading_mode": "LIVE",
            "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
            "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
            "host": settings.IBKR_HOST,
            "port": settings.IBKR_PORT,
            "connected": False,
            "status": "OFFLINE",
            "latency_ms": None,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "message": "IBKR connection lost (disconnected)."
        }
        self._last_connection_check = time.time()

    def _on_ib_error(self, reqId: int, errorCode: int, errorString: str, contract: Any):
        if errorCode in (2104, 2106, 2108):
            return
        if errorCode in (1100, 1101, 1102, 2110, 2158):
            logger.warning(f"IBKR connectivity notice [{errorCode}]: {errorString}")
            if self._last_connection_status:
                self._last_connection_status["message"] = f"IBKR [{errorCode}]: {errorString}"
            return
        logger.info(f"IBKR note [{errorCode}]: {errorString}")

    async def _get_ib(self, timeout: float = 5.0) -> Optional[Any]:
        """
        Returns shared persistent IB client.
        Reuses existing active connection to eliminate socket churn.
        """
        async with self._connection_lock:
            if self._ib is not None and self._ib.isConnected():
                return self._ib

            try:
                from ib_async import IB
                if self._ib is None:
                    self._ib = IB()
                    self._ib.disconnectedEvent += self._on_ib_disconnected
                    self._ib.errorEvent += self._on_ib_error

                if not self._ib.isConnected():
                    await self._ib.connectAsync(
                        host=settings.IBKR_HOST,
                        port=settings.IBKR_PORT,
                        clientId=settings.IBKR_CLIENT_ID,
                        timeout=timeout,
                        readonly=False
                    )
                return self._ib
            except Exception as e:
                logger.debug(f"IBKR connect note: {e}")
                return None

    async def disconnect(self):
        """Cleanly disconnects the persistent IB client and clears cache."""
        async with self._connection_lock:
            if self._ib and self._ib.isConnected():
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
            self._ib = None
            self._last_connection_status = None
            self._last_connection_check = 0.0

    async def reconnect(self) -> Dict[str, Any]:
        """Forces clean disconnection and immediately re-probes broker connectivity."""
        await self.disconnect()
        return await self.check_connection(force=True)


    async def _fetch_ibkr_prices(self, tickers: List[Tuple[str, str]]) -> Dict[str, float]:
        """
        Fetches current or delayed market prices directly from IBKR via ib_async.
        Uses in-memory TTL cache (30s) to minimize socket connections and roundtrips.
        """
        now = time.time()
        needed = [
            (t, c) for t, c in tickers
            if t not in self._ibkr_price_cache or (now - self._ibkr_price_cache[t][0]) > 30.0
        ]
        if not needed:
            return {t: self._ibkr_price_cache[t][1] for t, _ in tickers if t in self._ibkr_price_cache}

        if settings.LIVE_BROKER.lower() == "ibkr":
            try:
                from ib_async import Stock
                ib = await self._get_ib()
                if ib and ib.isConnected():
                    ib.reqMarketDataType(3)  # 3 = Delayed (supports accounts without paid live subscriptions)
                    contracts = [
                        Stock(t.split(".")[0], "SMART", c.upper())
                        for t, c in needed
                    ]
                    await ib.qualifyContractsAsync(*contracts)
                    stream = [ib.reqMktData(c, "", False, False) for c in contracts]
                    await asyncio.sleep(1.0)
                    for (orig_ticker, _), tk in zip(needed, stream):
                        p = tk.marketPrice()
                        if p != p or p <= 0:
                            p = tk.last if (tk.last == tk.last and tk.last > 0) else tk.close
                        if p and p == p and p > 0:
                            self._ibkr_price_cache[orig_ticker] = (time.time(), float(p))
                    for c in contracts:
                        ib.cancelMktData(c)
            except Exception as e:
                logger.debug(f"IBKR live market data note: {e}")

        return {t: self._ibkr_price_cache[t][1] for t, _ in tickers if t in self._ibkr_price_cache}


    async def _dispatch_real_order(
        self,
        ticker: str,
        shares: float,
        price: float,
        currency: str,
        stop_loss: Optional[float],
        take_profit: Optional[float]
    ) -> Optional[str]:
        """
        Dispatches live bracket order to configured broker API (IBKR or Alpaca).
        Returns broker order ID if connected, or None.
        """
        broker = settings.LIVE_BROKER.lower()
        if broker == "ibkr":
            try:
                from ib_async import Stock
                ib = await self._get_ib()
                if ib and ib.isConnected():
                    contract = Stock(ticker.upper().split(".")[0], "SMART", currency.upper())
                    await ib.qualifyContractsAsync(contract)
                    # Exchanges reject prices off their tick grid (Error 110); Euronext/Xetra ticks grow with the price
                    details = (await ib.reqContractDetailsAsync(contract))[0]
                    exchanges, rule_ids = details.validExchanges.split(","), details.marketRuleIds.split(",")
                    rule_id = rule_ids[exchanges.index(contract.primaryExchange)] if contract.primaryExchange in exchanges else rule_ids[0]
                    increments = [(r.lowEdge, r.increment) for r in await ib.reqMarketRuleAsync(int(rule_id))]
                    bracket = ib.bracketOrder(
                        action="BUY",
                        quantity=shares,
                        limitPrice=round_to_tick(price, increments),
                        takeProfitPrice=round_to_tick(take_profit or price * 1.10, increments),
                        stopLossPrice=round_to_tick(stop_loss or price * 0.95, increments)
                    )
                    # Explicit TIF avoids IBKR's preset-override notice (10349); TP/SL must survive the close
                    for order in bracket:
                        order.tif = "DAY" if order.action == "BUY" else "GTC"
                    placed = [ib.placeOrder(contract, order) for order in bracket]
                    if placed:
                        # placeOrder returns before IBKR answers; a rejected order must not become a recorded position
                        rejection = await self._ibkr_order_rejection(placed[0])
                        if rejection:
                            logger.error(f"IBKR rejected BUY {shares} {ticker}: {rejection}")
                            return None
                        return f"ibkr_{placed[0].order.orderId}"
            except Exception as e:
                logger.info(f"IBKR gateway communication note: {e}")
        elif broker == "alpaca":
            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
                from alpaca.trading.enums import OrderSide, TimeInForce
                if settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
                    client = TradingClient(
                        api_key=settings.ALPACA_API_KEY,
                        secret_key=settings.ALPACA_SECRET_KEY,
                        paper=not settings.LIVE_TRADING_ENABLED
                    )
                    req = MarketOrderRequest(
                        symbol=ticker.upper(),
                        qty=shares,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC,
                        take_profit=TakeProfitRequest(limit_price=take_profit) if take_profit else None,
                        stop_loss=StopLossRequest(stop_price=stop_loss) if stop_loss else None
                    )
                    resp = client.submit_order(req)
                    return f"alpaca_{resp.id}"
            except Exception as e:
                logger.info(f"Alpaca communication note: {e}")
        return None

    @staticmethod
    async def _ibkr_order_rejection(trade: Any, timeout: float = 2.0) -> Optional[str]:
        """
        Waits briefly for IBKR to accept or reject a placed order. Returns the rejection reason,
        or None once the order is live (or still pending when the timeout runs out).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            log = getattr(trade, "log", None) or []
            # ib_async lacks 10349 (informational "TIF set to DAY by preset") in its warningCodes and marks the order Cancelled; the real status follows
            if status in ("Cancelled", "ApiCancelled", "Inactive") and not (log and getattr(log[-1], "errorCode", 0) == 10349):
                messages = [getattr(entry, "message", "") for entry in log]
                return "; ".join(m for m in messages if m) or status
            if status in ("PreSubmitted", "Submitted", "Filled") or loop.time() >= deadline:
                return None
            await asyncio.sleep(0.2)

    async def _dispatch_real_close(self, ticker: str, shares: float, currency: str = "USD") -> Optional[str]:
        """Dispatches live position exit to configured broker API."""
        broker = settings.LIVE_BROKER.lower()
        if broker == "ibkr":
            try:
                from ib_async import Stock, MarketOrder
                ib = await self._get_ib()
                if ib and ib.isConnected():
                    symbol = ticker.upper().split(".")[0]
                    contract = Stock(symbol, "SMART", currency.upper())
                    await ib.qualifyContractsAsync(contract)

                    # Cancel resting bracket legs (stop / take-profit) first, otherwise a leg
                    # can still fill after our market sell and open a short position.
                    legs = []
                    for open_trade in ib.openTrades():
                        oc = open_trade.contract
                        if oc.symbol.upper() == symbol and (oc.currency or "").upper() == currency.upper() \
                                and open_trade.order.action == "SELL":
                            ib.cancelOrder(open_trade.order)
                            legs.append(open_trade)
                    # Give the cancels up to 2s to settle: a leg that fills meanwhile must show up in the position below
                    for _ in range(20):
                        if all(leg.isDone() for leg in legs):
                            break
                        await asyncio.sleep(0.1)

                    # Never sell more than the broker actually holds (fresh positions, not the cached list)
                    held = sum(
                        float(bp.position) for bp in await ib.reqPositionsAsync()
                        if bp.contract.symbol.upper() == symbol and (bp.contract.currency or "").upper() == currency.upper()
                    )
                    if held <= 0:
                        logger.info(f"IBKR position for {ticker} already flat (bracket leg filled); no exit order sent.")
                        return "ibkr_exit_already_flat"
                    order = MarketOrder("SELL", min(shares, held), tif="DAY")
                    placed = ib.placeOrder(contract, order)
                    return f"ibkr_exit_{placed.order.orderId}"
            except Exception as e:
                logger.info(f"IBKR close note: {e}")
        elif broker == "alpaca":
            try:
                from alpaca.trading.client import TradingClient
                if settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
                    client = TradingClient(
                        api_key=settings.ALPACA_API_KEY,
                        secret_key=settings.ALPACA_SECRET_KEY,
                        paper=not settings.LIVE_TRADING_ENABLED
                    )
                    # Cancel resting bracket legs before closing so none can fill afterwards
                    try:
                        from alpaca.trading.requests import GetOrdersRequest
                        from alpaca.trading.enums import QueryOrderStatus
                        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker.upper()])):
                            client.cancel_order_by_id(o.id)
                    except Exception as cancel_err:
                        logger.info(f"Alpaca cancel open orders note: {cancel_err}")
                    # A filled bracket leg already closed the position: report it as closed instead of
                    # failing, otherwise the local position could never be closed
                    from alpaca.common.exceptions import APIError
                    try:
                        client.get_open_position(ticker.upper())
                    except APIError as pos_err:
                        if pos_err.status_code == 404:
                            logger.info(f"Alpaca position for {ticker} already flat (bracket leg filled); no exit order sent.")
                            return "alpaca_exit_already_flat"
                        raise
                    pos = client.close_position(ticker.upper())
                    return f"alpaca_exit_{pos.id}"
            except Exception as e:
                logger.info(f"Alpaca close note: {e}")
        return None

    def is_live_armed(self) -> Tuple[bool, str]:
        """Verifies if live real-money execution is safely enabled."""
        if settings.KILL_SWITCH_ACTIVE:
            return False, "Emergency kill switch is ACTIVE. Trading halted."
        if not settings.LIVE_TRADING_ENABLED:
            return False, "LIVE_TRADING_ENABLED is False. Enable in settings/.env to trade real money."
        if settings.TRADING_MODE.upper() != "LIVE":
            return False, f"Current TRADING_MODE is '{settings.TRADING_MODE}', not 'LIVE'."
        return True, "Live execution armed."

    async def get_daily_realized_loss_eur(self) -> float:
        """Calculates total net realized loss from trades executed today."""
        today_start = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Trade).where(
                    Trade.executed_at >= today_start,
                    Trade.order_type == "SELL",
                    Trade.is_live == True
                )
            )
            trades = res.scalars().all()
            net_pnl = sum(t.realized_pnl_eur or 0.0 for t in trades)
            return abs(net_pnl) if net_pnl < 0 else 0.0

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Fetches live portfolio state (from broker and local mirror)."""
        await self.refresh_positions_pnl()
        await self.adjust_triggers_dynamically()
        try:
            await scan_live_cash_flows()
        except Exception as e:
            logger.warning(f"Deposit scan failed: {e}")
        net_deposits = await net_cash_flows_eur(is_live=True)
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = res.scalars().first()

            pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
            positions = pos_res.scalars().all()

            trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True))
            pending_triggers = trig_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)

            cash = snapshot.cash_eur if snapshot else 0.0
            usable_cash, protected_reserve = get_effective_deployable_cash(cash)
            available_cash = max(0.0, usable_cash - pending_alloc)
            equity = sum(round(p.current_price_eur * p.shares, 2) for p in positions)
            total_val = round(cash + equity, 2)
            # Portfolio value comes from IBKR: fresh account summary, else the last synced snapshot, else local quotes
            value_source = "local"
            ib_cash = ib_total = ib_equity = None
            if settings.LIVE_BROKER.lower() == "ibkr":
                try:
                    async def _fresh():
                        ib = await self._get_ib(timeout=2.0)
                        if not (ib and ib.isConnected()):
                            return None, None, None
                        return await self._fetch_ibkr_account_values(ib)
                    ib_cash, ib_total, ib_equity = await asyncio.wait_for(_fresh(), timeout=4.0)
                except Exception as e:
                    logger.debug(f"Fresh IBKR account values unavailable: {e}")
                if ib_total is not None:
                    value_source = "ibkr"
                    # Same moment as the total, so cash + equity add up
                    if ib_cash is not None:
                        cash = ib_cash
                elif snapshot and snapshot.total_value_eur:
                    ib_total, ib_equity, value_source = snapshot.total_value_eur, snapshot.equity_eur, "snapshot"
            if ib_total is not None:
                total_val = round(ib_total, 2)
                equity = ib_equity if ib_equity is not None else total_val - cash
            unrealized_pnl = sum(round(p.unrealized_pnl_eur, 2) for p in positions)
            daily_loss = await self.get_daily_realized_loss_eur()

            # Baseline initial capital for live trading
            first_snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(PortfolioSnapshot.timestamp.asc())
            )
            first_snap = first_snap_res.scalars().first()
            initial_cap = first_snap.total_value_eur if first_snap else total_val
            # Deposits and withdrawals are not gain: measure against everything that was put in
            invested = round(initial_cap + net_deposits, 2)
            total_gain = round(total_val - invested, 2)
            total_return_pct = round(total_gain / invested * 100, 2) if invested > 0 else 0.0

            return {
                "broker": settings.LIVE_BROKER.upper(),
                "trading_mode": "LIVE",
                "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
                "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
                "full_budget_utilization": settings.FULL_BUDGET_UTILIZATION,
                "base_currency": settings.BASE_CURRENCY,
                "cash_eur": round(cash, 2),
                "total_account_cash_eur": round(cash, 2),
                "max_capital_usage_pct": round(settings.MAX_CAPITAL_USAGE_PCT, 1),
                "max_deployable_capital_eur": settings.MAX_DEPLOYABLE_CAPITAL_EUR,
                "usable_capital_eur": usable_cash,
                "protected_reserve_eur": protected_reserve,
                "available_cash_eur": round(available_cash, 2),
                "reserved_triggers_eur": round(pending_alloc, 2),
                "equity_eur": round(equity, 2),
                "total_value_eur": total_val,
                "value_source": value_source,
                "unrealized_pnl_eur": round(unrealized_pnl, 2),
                "total_return_pct": total_return_pct,
                "initial_capital_eur": round(initial_cap, 2),
                "net_deposits_eur": net_deposits,
                "invested_capital_eur": invested,
                "total_gain_eur": total_gain,
                "daily_realized_loss_eur": round(daily_loss, 2),
                "max_daily_loss_limit_eur": settings.MAX_DAILY_LOSS_EUR,
                "open_positions_count": len(positions),
                "pending_triggers_count": len(pending_triggers),
                "ibkr_pricing_tier": getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                "max_fee_drag_pct": getattr(settings, "MAX_FEE_DRAG_PCT", 3.5),
            }

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Returns all open live positions."""
        await self.refresh_positions_pnl()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
            positions = res.scalars().all()
            return [
                {
                    "ticker": p.ticker,
                    "name": p.name,
                    "shares": round(p.shares, 4),
                    "currency": p.currency,
                    "entry_price_native": round(p.entry_price_native, 2),
                    "current_price_native": round(p.current_price_native, 2),
                    "stop_loss_native": round(p.stop_loss_native, 2) if p.stop_loss_native else None,
                    "take_profit_native": round(p.take_profit_native, 2) if p.take_profit_native else None,
                    "entry_price_eur": round(p.entry_price_eur, 2),
                    "current_price_eur": round(p.current_price_eur, 2),
                    "cost_basis_eur": round(p.cost_basis_eur, 2),
                    "current_value_eur": round(p.current_price_eur * p.shares, 2),
                    "fees_eur": round(p.fees_eur or 0.0, 2),
                    "unrealized_pnl_eur": round(p.unrealized_pnl_eur, 2),
                    "unrealized_pnl_pct": round(p.unrealized_pnl_pct, 2),
                    "is_live": p.is_live,
                    "broker_order_id": p.broker_order_id,
                    "opened_at": p.opened_at.strftime("%Y-%m-%d %H:%M") if p.opened_at else "",
                }
                for p in positions
            ]

    async def execute_buy(
        self,
        ticker: str,
        name: str,
        price_native: float,
        currency: str,
        stop_loss_native: float,
        take_profit_native: float,
        allocation_eur: float,
        consensus_score: int,
        rationale: str,
        is_manual: bool = False,
        trigger_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Submits real buy order to the brokerage API (IBKR / Alpaca) with real money.
        Validates kill switch, daily drawdown, market hours, and settled capital.
        """
        # 1. Verify live execution interlocks
        armed, arm_msg = self.is_live_armed()
        if not armed:
            logger.warning(f"Live BUY blocked for {ticker}: {arm_msg}")
            return {"success": False, "message": arm_msg}

        # 2. Check Daily Max Loss Limit
        daily_loss = await self.get_daily_realized_loss_eur()
        if daily_loss >= settings.MAX_DAILY_LOSS_EUR:
            msg = f"Live BUY blocked: Daily loss limit exceeded (€{daily_loss:.2f} >= €{settings.MAX_DAILY_LOSS_EUR:.2f})."
            logger.critical(msg)
            return {"success": False, "message": msg}

        # 3. Market Hours Check
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            logger.info(f"Live BUY blocked for {ticker}: {open_msg}")
            return {"success": False, "message": open_msg}

        async with AsyncSessionLocal() as session:
            # 4. Check auto-buy toggle
            if is_manual:
                auto_buy_enabled = True
            else:
                cfg_res = await session.execute(select(SystemConfig).where(SystemConfig.key == "AUTO_BUY_ENABLED"))
                cfg = cfg_res.scalars().first()
                auto_buy_enabled = (cfg.value.lower() == "true") if cfg else settings.AUTO_BUY_ENABLED

            # 5. Get cash and positions
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
            open_positions = pos_res.scalars().all()

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else 0.0
            usable_cash, _ = get_effective_deployable_cash(cash_eur)
            equity_eur = sum(p.current_price_eur * p.shares for p in open_positions)
            total_eur = cash_eur + equity_eur

            # Reserved cash for other triggers
            trig_query = select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True)
            if trigger_id:
                trig_query = trig_query.where(OrderTrigger.id != trigger_id)
            trig_res = await session.execute(trig_query)
            pending_triggers = trig_res.scalars().all()
            other_committed = sum(t.allocation_eur for t in pending_triggers)
            available_cash = max(0.0, usable_cash - other_committed)

            # 6. Apply Full Budget Utilization if requested allocation is default or zero
            if (allocation_eur is None or allocation_eur <= 0) or settings.FULL_BUDGET_UTILIZATION:
                calc_alloc = new_entry_allocation(
                    usable_cash=usable_cash,
                    reserved_eur=other_committed,
                    current_positions_count=len(open_positions),
                    ticker=ticker,
                    currency=currency,
                    requested_alloc=allocation_eur if allocation_eur and allocation_eur > 0 else None
                )
                if calc_alloc > 0:
                    allocation_eur = calc_alloc

            # Automated buys risk the same share of their slot (or portfolio, per RISK_BUDGET_BASE) at their stop
            if not is_manual:
                allocation_eur = risk_capped_allocation(allocation_eur, price_native, stop_loss_native, total_eur)

            # 7. Validate risk guardrails (including broker fee viability)
            valid, msg = risk_guardrail.validate_buy_order(
                ticker=ticker,
                current_positions=open_positions,
                cash_eur=cash_eur,
                portfolio_total_eur=total_eur,
                allocation_eur=allocation_eur,
                consensus_score=consensus_score,
                auto_buy_enabled=auto_buy_enabled,
                available_cash_eur=usable_cash if trigger_id else available_cash,
                price_native=price_native,
                currency=currency,
                stop_loss_native=stop_loss_native,
                take_profit_native=take_profit_native
            )
            if not valid:
                logger.info(f"Live BUY order blocked for {ticker}: {msg}")
                return {"success": False, "message": msg}

            # 8. Hard single order cap
            if settings.MAX_SINGLE_ORDER_EUR and allocation_eur > settings.MAX_SINGLE_ORDER_EUR:
                allocation_eur = settings.MAX_SINGLE_ORDER_EUR

            if allocation_eur > usable_cash:
                allocation_eur = usable_cash

            price_eur = currency_service.to_eur(price_native, currency)
            if price_eur <= 0:
                return {"success": False, "message": f"Invalid price EUR for {ticker}."}

            if settings.WHOLE_SHARES_ONLY:
                # The broker only fills whole shares: take the count closest to the allocation
                shares = float(whole_share_count(allocation_eur, price_eur, max_eur=max(available_cash, allocation_eur)))
                if shares < 1:
                    msg = f"One share of {ticker} costs €{price_eur:.2f}, more than the €{allocation_eur:.2f} allocation."
                    logger.info(f"Buy order blocked: {msg}")
                    return {"success": False, "unaffordable": True, "message": msg}
            else:
                shares = round(allocation_eur / price_eur, 4)
            actual_cost_eur = round(shares * price_eur, 2)

            if shares <= 0 or actual_cost_eur <= 0:
                return {"success": False, "message": f"Insufficient capital to buy {ticker}."}

            # Calculate estimated broker commissions
            from src.trading.fees import calculate_broker_fee
            fee_info = calculate_broker_fee(
                ticker=ticker,
                shares=shares,
                price_native=price_native,
                currency=currency,
                broker=settings.LIVE_BROKER,
                pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                order_action="BUY"
            )
            fee_eur = fee_info["fee_eur"]
            # Leave room for the commission when the allocation uses all remaining cash (same as paper)
            if actual_cost_eur + fee_eur > cash_eur and cash_eur > fee_eur:
                import math
                step = 1.0 if settings.WHOLE_SHARES_ONLY else 10000.0
                shares = math.floor(((cash_eur - fee_eur) / price_eur) * step) / step
                actual_cost_eur = round(shares * price_eur, 2)
            if shares <= 0 or actual_cost_eur + fee_eur > cash_eur:
                return {"success": False, "message": f"Insufficient cash (€{cash_eur:.2f}) for {ticker} cost €{actual_cost_eur:.2f} plus fee €{fee_eur:.2f}."}

            # 9. Dispatch real order to broker API
            dispatched_id = await self._dispatch_real_order(
                ticker=ticker,
                shares=shares,
                price=price_native,
                currency=currency,
                stop_loss=stop_loss_native,
                take_profit=take_profit_native
            )
            if not dispatched_id:
                logger.error(f"[LIVE EXECUTION] BUY {ticker} was not accepted by {settings.LIVE_BROKER.upper()}; nothing recorded.")
                return {"success": False, "message": f"Order for {ticker} could not be placed at {settings.LIVE_BROKER.upper()} (broker not connected or order rejected)."}
            broker_order_id = dispatched_id
            logger.info(
                f"[LIVE EXECUTION] Submitting real order to {settings.LIVE_BROKER.upper()}: "
                f"BUY {shares} {ticker} @ {price_native} {currency} (Cost: €{actual_cost_eur}, Fee: €{fee_eur:.2f} [{fee_info['breakdown']}]) | "
                f"SL: {stop_loss_native} | TP: {take_profit_native} | Order ID: {broker_order_id}"
            )

            utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

            # Record live position in database
            new_pos = Position(
                ticker=ticker.upper(),
                name=name,
                currency=currency,
                shares=shares,
                entry_price_native=price_native,
                current_price_native=price_native,
                highest_price_native=price_native,
                stop_loss_native=stop_loss_native,
                take_profit_native=take_profit_native,
                entry_price_eur=price_eur,
                current_price_eur=price_eur,
                cost_basis_eur=actual_cost_eur,
                fees_eur=fee_eur,
                unrealized_pnl_eur=0.0,
                unrealized_pnl_pct=0.0,
                status="OPEN",
                is_live=True,
                broker_order_id=broker_order_id,
                opened_at=utc_now
            )
            session.add(new_pos)

            # Record Trade
            trade = Trade(
                ticker=ticker.upper(),
                name=name,
                order_type="BUY",
                shares=shares,
                currency=currency,
                price_native=price_native,
                price_eur=price_eur,
                total_eur=actual_cost_eur,
                fee_eur=fee_eur,
                rationale=f"[LIVE {settings.LIVE_BROKER.upper()}] {rationale} (Fee: €{fee_eur:.2f})",
                consensus_score=consensus_score,
                is_live=True,
                broker_order_id=broker_order_id,
                executed_at=utc_now
            )
            session.add(trade)

            # Update Portfolio Snapshot (cash decreases by cost + commission fee)
            new_cash_eur = round(cash_eur - actual_cost_eur - fee_eur, 2)
            new_equity_eur = equity_eur + actual_cost_eur
            new_snapshot = PortfolioSnapshot(
                cash_eur=new_cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=round(new_cash_eur + new_equity_eur, 2),
                open_positions_count=len(open_positions) + 1,
                is_live=True,
                timestamp=utc_now
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            await self.adjust_triggers_dynamically()

            # Record into TencentDB Agent Memory
            try:
                import asyncio
                from src.memory.client import agent_memory_client
                asyncio.create_task(agent_memory_client.record_trade_event(
                    ticker=ticker,
                    order_type="BUY",
                    shares=shares,
                    price=price_native,
                    currency=currency,
                    fee_eur=fee_eur,
                    rationale=rationale
                ))
            except Exception:
                pass

            return {
                "success": True,
                "is_live": True,
                "broker": settings.LIVE_BROKER.upper(),
                "broker_order_id": broker_order_id,
                "ticker": ticker,
                "shares": shares,
                "price_native": price_native,
                "price_eur": price_eur,
                "total_eur": actual_cost_eur,
                "fee_eur": fee_eur,
                "fee_breakdown": fee_info["breakdown"],
                "stop_loss": stop_loss_native,
                "take_profit": take_profit_native,
                "message": f"Real money order submitted to {settings.LIVE_BROKER.upper()}: {shares} shares of {ticker} for €{actual_cost_eur:.2f} (Fee: €{fee_eur:.2f})."
            }

    async def execute_sell(self, ticker: str, reason: str = "Manual Close") -> Dict[str, Any]:
        """Submits real sell order to brokerage API to close live position."""
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            return {"success": False, "message": open_msg}

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN", Position.is_live == True)
            )
            pos = res.scalars().first()
            if not pos:
                return {"success": False, "message": f"No open live position found for {ticker}."}

            quote = market_data_provider.get_stock_data(ticker)
            exit_price_native = quote["price"] if quote else pos.current_price_native
            exit_price_eur = currency_service.to_eur(exit_price_native, pos.currency)
            proceeds_eur = round(pos.shares * exit_price_eur, 2)

            # Calculate exit broker fee
            from src.trading.fees import calculate_broker_fee
            exit_fee_info = calculate_broker_fee(
                ticker=ticker,
                shares=pos.shares,
                price_native=exit_price_native,
                currency=pos.currency,
                broker=settings.LIVE_BROKER,
                pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                order_action="SELL"
            )
            exit_fee_eur = exit_fee_info["fee_eur"]
            total_fees_eur = round((pos.fees_eur or 0.0) + exit_fee_eur, 2)

            # Net realized PnL deducting both entry and exit broker commissions
            realized_pnl_eur = round(proceeds_eur - pos.cost_basis_eur - total_fees_eur, 2)
            realized_pnl_pct = round((realized_pnl_eur / pos.cost_basis_eur) * 100, 2) if pos.cost_basis_eur > 0 else 0.0

            dispatched_id = await self._dispatch_real_close(ticker=ticker, shares=pos.shares, currency=pos.currency)
            if not dispatched_id:
                logger.error(f"[LIVE EXECUTION] SELL {ticker} was not accepted by {settings.LIVE_BROKER.upper()}; position left open.")
                return {"success": False, "message": f"Exit order for {ticker} could not be placed at {settings.LIVE_BROKER.upper()} (broker not connected or order rejected)."}
            broker_order_id = dispatched_id
            logger.info(
                f"[LIVE EXECUTION] Submitting exit order to {settings.LIVE_BROKER.upper()}: "
                f"SELL {pos.shares} {ticker} @ {exit_price_native} {pos.currency} | "
                f"Gross: €{proceeds_eur} | Exit Fee: €{exit_fee_eur:.2f} ({exit_fee_info['breakdown']}) | "
                f"Net PnL: €{realized_pnl_eur} ({realized_pnl_pct}%)"
            )

            utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

            pos.status = "CLOSED"
            pos.current_price_native = exit_price_native
            pos.current_price_eur = exit_price_eur
            pos.fees_eur = total_fees_eur

            trade = Trade(
                ticker=ticker.upper(),
                name=pos.name,
                order_type="SELL",
                shares=pos.shares,
                currency=pos.currency,
                price_native=exit_price_native,
                price_eur=exit_price_eur,
                total_eur=proceeds_eur,
                fee_eur=exit_fee_eur,
                realized_pnl_eur=realized_pnl_eur,
                realized_pnl_pct=realized_pnl_pct,
                rationale=f"[LIVE {settings.LIVE_BROKER.upper()}] {reason} (Total Fees: €{total_fees_eur:.2f})",
                is_live=True,
                broker_order_id=broker_order_id,
                executed_at=utc_now
            )
            session.add(trade)

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            # Net cash received is proceeds minus exit fee
            net_proceeds_eur = round(proceeds_eur - exit_fee_eur, 2)
            cash_eur = (snapshot.cash_eur if snapshot else 0.0) + net_proceeds_eur

            remaining_res = await session.execute(
                select(Position).where(Position.status == "OPEN", Position.is_live == True, Position.ticker != ticker.upper())
            )
            remaining = remaining_res.scalars().all()
            new_equity_eur = sum(p.current_price_eur * p.shares for p in remaining)

            new_snapshot = PortfolioSnapshot(
                cash_eur=cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=round(cash_eur + new_equity_eur, 2),
                open_positions_count=len(remaining),
                is_live=True,
                timestamp=utc_now
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            await self.adjust_triggers_dynamically()

            # Record into TencentDB Agent Memory
            try:
                import asyncio
                from src.memory.client import agent_memory_client
                asyncio.create_task(agent_memory_client.record_trade_event(
                    ticker=ticker,
                    order_type="SELL",
                    shares=pos.shares,
                    price=exit_price_native,
                    currency=pos.currency,
                    pnl_eur=realized_pnl_eur,
                    fee_eur=exit_fee_eur,
                    rationale=reason
                ))
            except Exception:
                pass

            sell_res = {
                "success": True,
                "is_live": True,
                "broker": settings.LIVE_BROKER.upper(),
                "broker_order_id": broker_order_id,
                "ticker": ticker,
                "shares": pos.shares,
                "exit_price_native": exit_price_native,
                "exit_price_eur": exit_price_eur,
                "proceeds_eur": proceeds_eur,
                "realized_pnl_eur": realized_pnl_eur,
                "realized_pnl_pct": realized_pnl_pct,
                "reason": reason,
                "message": f"Closed live position on {settings.LIVE_BROKER.upper()}: {pos.shares} {ticker} for €{proceeds_eur:.2f} (PnL: €{realized_pnl_eur:+.2f})."
            }

            try:
                from src.telegram.bot import telegram_bot
                await telegram_bot.notify_position_closed(sell_info=sell_res, adjustments=[], lesson=None)
            except Exception as notif_err:
                logger.error(f"Error dispatching live sell notification: {notif_err}")

            return sell_res

    async def create_price_trigger(
        self,
        ticker: str,
        name: str,
        currency: str,
        trigger_type: str,
        target_price: float,
        current_price_at_creation: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        allocation_eur: float,
        consensus_score: int,
        ai_rationale: str
    ) -> Dict[str, Any]:
        """Creates an armed price trigger for live execution."""
        target_price = round(target_price, 2)
        current_price_at_creation = round(current_price_at_creation, 2)
        stop_loss = round(stop_loss, 2) if stop_loss else None
        take_profit = round(take_profit, 2) if take_profit else None
        level_error = validate_trigger_levels(trigger_type, target_price, current_price_at_creation, stop_loss, take_profit)
        if level_error:
            return {"success": False, "message": level_error}

        async with AsyncSessionLocal() as session:
            pos_res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN", Position.is_live == True)
            )
            if pos_res.scalars().first():
                return {"success": False, "message": f"Active position already exists for {ticker.upper()}."}

            res = await session.execute(
                select(OrderTrigger).where(OrderTrigger.ticker == ticker.upper(), OrderTrigger.status == "PENDING", OrderTrigger.is_live == True)
            )
            if res.scalars().first():
                return {"success": False, "message": f"A pending trigger already exists for {ticker.upper()}."}

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else 0.0
            usable_cash, _ = get_effective_deployable_cash(cash_eur)

            pending_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True))
            pending_triggers = pending_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)
            available_cash = round(max(0.0, usable_cash - pending_alloc), 2)

            if allocation_eur is not None and allocation_eur > available_cash:
                return {
                    "success": False,
                    "message": f"Insufficient available cash (€{available_cash:.2f} available, required €{allocation_eur:.2f})."
                }

            # Without an explicit allocation, take the slot the dynamic re-allocation would give it
            if allocation_eur is None or allocation_eur <= 0:
                pos_count_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
                current_open = len(pos_count_res.scalars().all())
                calc_alloc = new_entry_allocation(usable_cash, pending_alloc, current_open, ticker=ticker, currency=currency)
                if calc_alloc > 0:
                    allocation_eur = calc_alloc

            # Factor in broker fees & economic viability
            from src.trading.fees import evaluate_trade_economic_viability
            viability = evaluate_trade_economic_viability(
                ticker=ticker,
                allocation_eur=allocation_eur,
                price_native=target_price,
                currency=currency,
                stop_loss_native=stop_loss,
                take_profit_native=take_profit,
                broker=settings.LIVE_BROKER,
                pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                max_fee_drag_pct=getattr(settings, "MAX_FEE_DRAG_PCT", 3.5)
            )
            if not viability["viable"]:
                min_v = viability.get("min_viable_allocation_eur", 75.0)
                if allocation_eur < min_v <= available_cash:
                    allocation_eur = min_v
                else:
                    return {
                        "success": False,
                        "message": f"Cannot arm trigger: {viability['reason']}"
                    }

            if allocation_eur > available_cash:
                return {
                    "success": False,
                    "message": f"Insufficient available cash (€{available_cash:.2f} available, required €{allocation_eur:.2f})."
                }

            trigger = OrderTrigger(
                ticker=ticker.upper(),
                name=name,
                currency=currency,
                trigger_type=trigger_type,
                target_price=round(target_price, 2),
                current_price_at_creation=round(current_price_at_creation, 2),
                stop_loss=round(stop_loss, 2) if stop_loss else None,
                take_profit=round(take_profit, 2) if take_profit else None,
                allocation_eur=round(allocation_eur, 2),
                consensus_score=consensus_score,
                ai_rationale=f"[LIVE ARMED] {ai_rationale}",
                status="PENDING",
                is_live=True,
                created_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trigger)
            await session.commit()
            await session.refresh(trigger)

            # Record into TencentDB Agent Memory
            try:
                import asyncio
                from src.memory.client import agent_memory_client
                asyncio.create_task(agent_memory_client.record_trigger_event(
                    trigger_id=trigger.id,
                    ticker=trigger.ticker,
                    trigger_type=trigger.trigger_type,
                    target_price=trigger.target_price,
                    allocation_eur=trigger.allocation_eur,
                    rationale=ai_rationale or ""
                ))
            except Exception:
                pass

            result = {
                "success": True,
                "is_live": True,
                "trigger_id": trigger.id,
                "ticker": trigger.ticker,
                "currency": trigger.currency,
                "trigger_type": trigger.trigger_type,
                "target_price": trigger.target_price,
                "current_price": trigger.current_price_at_creation,
                "stop_loss": trigger.stop_loss,
                "take_profit": trigger.take_profit,
                "allocation_eur": trigger.allocation_eur,
                "message": f"Real money AI Trigger armed for {trigger.ticker}: {trigger.trigger_type} @ {trigger.target_price}."
            }

        # Report the allocation the dashboard and the buy will use (after the dynamic re-allocation), not the amount asked for
        result.update(await self.get_trigger_plan(result["trigger_id"]))
        return result

    async def _pending_trigger_plans(self, session) -> Dict[int, Dict[str, Any]]:
        """Planned budget and share count per pending live trigger, sized like execute_buy at the limit price."""
        trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True))
        pending = trig_res.scalars().all()
        if not pending:
            return {}
        pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
        positions = pos_res.scalars().all()
        snap_res = await session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
        )
        snapshot = snap_res.scalars().first()
        cash_eur = snapshot.cash_eur if snapshot else 0.0
        usable_cash, _ = get_effective_deployable_cash(cash_eur)
        portfolio_value = cash_eur + sum(p.current_price_eur * p.shares for p in positions)
        # Same ranking adjust_triggers_dynamically applies before the dashboard shows or a trigger fires
        allocations = allocate_pending_triggers(usable_cash, len(positions), pending)
        return plan_pending_triggers(pending, usable_cash, portfolio_value, allocations)

    async def get_trigger_plan(self, trigger_id: int) -> Dict[str, Any]:
        """Allocation and planned buy of one pending trigger, as the dashboard shows it."""
        async with AsyncSessionLocal() as session:
            plan = (await self._pending_trigger_plans(session)).get(trigger_id)
        if not plan:
            return {}
        return {
            "allocation_eur": plan["allocation_eur"],
            "planned_shares": plan["shares"],
            "planned_cost_eur": plan["cost_eur"],
            "planned_budget_eur": plan["budget_eur"],
            "risk_capped": plan["risk_capped"],
        }

    async def cancel_price_trigger(self, trigger_id: int) -> Dict[str, Any]:
        """Cancels a pending live trigger."""
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == trigger_id))
            trigger = res.scalars().first()
            if not trigger:
                return {"success": False, "message": f"Trigger #{trigger_id} not found."}
            if trigger.status != "PENDING":
                return {"success": False, "message": f"Trigger #{trigger_id} is already {trigger.status}."}
            trigger.status = "CANCELLED"
            await session.commit()
            return {"success": True, "message": f"Trigger #{trigger_id} for {trigger.ticker} cancelled."}

    async def adjust_triggers_dynamically(self) -> List[Dict[str, Any]]:
        """
        Dynamically adjusts allocation_eur on all pending live triggers based on
        current cash balance, open positions, and MAX_POSITIONS settings.
        Highest consensus scores are funded first; triggers left without cash or a
        free position slot are set to €0 and stay armed until funding frees up.
        """
        async with AsyncSessionLocal() as session:
            trig_res = await session.execute(
                select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True)
            )
            pending = trig_res.scalars().all()
            if not pending:
                return []

            pos_res = await session.execute(
                select(Position).where(Position.status == "OPEN", Position.is_live == True)
            )
            open_positions = pos_res.scalars().all()
            current_open = len(open_positions)

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else 0.0
            usable_cash, _ = get_effective_deployable_cash(cash_eur)

            allocations = allocate_pending_triggers(usable_cash, current_open, pending)

            adjusted = []
            for t in pending:
                target_alloc = allocations[t.id]
                if round(t.allocation_eur, 2) != target_alloc:
                    t.allocation_eur = target_alloc
                    adjusted.append({"id": t.id, "ticker": t.ticker, "allocation_eur": target_alloc})

            if adjusted:
                await session.commit()
                logger.info(f"Dynamically adjusted {len(adjusted)} live triggers (cash €{usable_cash:.2f}, {current_open}/{settings.MAX_POSITIONS} slots)")

            return adjusted

    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves order triggers with current price and live distance % calculations."""
        await self.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            query = select(OrderTrigger).where(OrderTrigger.is_live == True).order_by(desc(OrderTrigger.created_at))
            if status:
                query = query.where(OrderTrigger.status == status.upper())
            res = await session.execute(query)
            triggers = res.scalars().all()
            if not triggers:
                return []
            plans = await self._pending_trigger_plans(session)

            unique_tickers = list({(t.ticker, t.currency) for t in triggers if t.status == "PENDING"})
            ib_prices = await self._fetch_ibkr_prices(unique_tickers) if unique_tickers else {}

            results = []
            quote_cache = {}
            for t in triggers:
                curr_price = ib_prices.get(t.ticker)
                if not curr_price and t.status == "PENDING":
                    # Last known quote, also while the market is closed: the price at creation is stale
                    if t.ticker not in quote_cache:
                        quote_cache[t.ticker] = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                    latest_data = quote_cache[t.ticker]
                    curr_price = latest_data["price"] if latest_data else None

                curr_price = curr_price or t.current_price_at_creation or t.target_price
                dist_pct = round(((t.target_price / curr_price) - 1.0) * 100, 2) if curr_price > 0 else 0.0

                results.append({
                    "id": t.id,
                    "ticker": t.ticker,
                    "name": t.name,
                    "currency": t.currency,
                    "trigger_type": t.trigger_type,
                    "target_price": round(t.target_price, 2),
                    "current_price_at_creation": round(t.current_price_at_creation, 2),
                    "current_price": round(curr_price, 2),
                    "distance_pct": dist_pct,
                    "stop_loss": round(t.stop_loss, 2) if t.stop_loss else None,
                    "take_profit": round(t.take_profit, 2) if t.take_profit else None,
                    "allocation_eur": round(t.allocation_eur, 2),
                    "planned_shares": plans[t.id]["shares"] if t.id in plans else None,
                    "planned_cost_eur": plans[t.id]["cost_eur"] if t.id in plans else None,
                    "risk_capped": plans[t.id]["risk_capped"] if t.id in plans else False,
                    "consensus_score": t.consensus_score,
                    "ai_rationale": t.ai_rationale,
                    "status": t.status,
                    "is_live": t.is_live,
                    "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
                    "triggered_at": t.triggered_at.strftime("%Y-%m-%d %H:%M") if t.triggered_at else None
                })
            return results

    async def check_and_execute_triggers(self) -> List[Dict[str, Any]]:
        """Evaluates pending triggers and executes live orders when conditions are met."""
        await self.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == True))
            pending = res.scalars().all()
            if not pending:
                return []

            executed = []
            for t in pending:
                if is_trigger_expired(t.created_at):
                    logger.info(f"Trigger #{t.id} for {t.ticker} expired (older than {settings.TRIGGER_EXPIRY_DAYS} days).")
                    t.status = "EXPIRED"
                    await session.commit()
                    continue

                # Unfunded triggers (outranked for cash or slots) wait until funding frees up
                if t.allocation_eur <= 0:
                    continue

                is_open, open_msg = check_market_hours(t.ticker)
                if not is_open:
                    continue

                curr_price = None
                if settings.LIVE_BROKER.lower() == "ibkr":
                    ib_prices = await self._fetch_ibkr_prices([(t.ticker, t.currency)])
                    curr_price = ib_prices.get(t.ticker)
                if not curr_price:
                    data = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                    if data:
                        curr_price = data["price"]
                if not curr_price:
                    continue

                # Check trigger condition; cancel setups the price has already run through
                condition_met, invalid_reason = evaluate_trigger_condition(
                    t.trigger_type, t.target_price, curr_price, t.stop_loss, t.take_profit
                )
                if invalid_reason:
                    logger.info(f"Cancelling trigger #{t.id} for {t.ticker}: {invalid_reason}")
                    t.status = "CANCELLED"
                    await session.commit()
                    continue

                if condition_met:
                    # Market regime filter: the trigger stays armed (and expires as usual) while the index is in a downtrend
                    regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, t.ticker, t.currency)
                    if not regime_ok:
                        logger.info(f"Live trigger #{t.id} for {t.ticker} held back: {regime_msg}")
                        continue
                    trade_res = await self.execute_buy(
                        ticker=t.ticker,
                        name=t.name or t.ticker,
                        price_native=curr_price,
                        currency=t.currency,
                        stop_loss_native=t.stop_loss,
                        take_profit_native=t.take_profit,
                        allocation_eur=t.allocation_eur,
                        consensus_score=t.consensus_score or 80,
                        rationale=f"Live Trigger #{t.id} Hit ({t.trigger_type} @ {t.target_price})",
                        is_manual=False,
                        trigger_id=t.id
                    )
                    if trade_res.get("success"):
                        t.status = "TRIGGERED"
                        t.triggered_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                        await session.commit()
                        trade_res["trigger_id"] = t.id
                        executed.append(trade_res)
                    elif trade_res.get("unaffordable"):
                        # Not even one whole share fits: release the reserved cash for other triggers
                        logger.info(f"Cancelling live trigger #{t.id} for {t.ticker}: {trade_res.get('message')}")
                        t.status = "CANCELLED"
                        await session.commit()

            return executed

    async def check_stops_and_targets(self) -> List[Dict[str, Any]]:
        """Monitors open live positions for stop-loss and take-profit."""
        await self.refresh_positions_pnl()
        triggered = []
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
            positions = res.scalars().all()
            tickers_to_close = []

            for p in positions:
                if p.stop_loss_native and p.current_price_native <= p.stop_loss_native:
                    tickers_to_close.append((p.ticker, f"Live Stop-Loss triggered at {p.current_price_native} <= {p.stop_loss_native}"))
                elif p.take_profit_native and p.current_price_native >= p.take_profit_native:
                    tickers_to_close.append((p.ticker, f"Live Take-Profit triggered at {p.current_price_native} >= {p.take_profit_native}"))

        for ticker, reason in tickers_to_close:
            sell_res = await self.execute_sell(ticker, reason=reason)
            if sell_res.get("success"):
                triggered.append(sell_res)

        return triggered

    async def refresh_positions_pnl(self):
        """Refreshes quotes, unrealized PnL, trailing high and breakeven stop for active positions."""
        async with self._refresh_lock:
            now = time.time()
            if (now - self._last_pnl_refresh) < self._pnl_refresh_ttl:
                return

            async with AsyncSessionLocal() as session:
                # Query dynamic breakeven trigger pct
                be_cfg = await session.execute(
                    select(SystemConfig).where(SystemConfig.key == "BREAKEVEN_TRIGGER_PCT")
                )
                be_row = be_cfg.scalars().first()
                breakeven_trigger_pct = float(be_row.value) if be_row else 0.025

                res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
                positions = res.scalars().all()
                if not positions:
                    self._last_pnl_refresh = now
                    return

                unique_pos = list({(p.ticker, p.currency) for p in positions})
                ib_prices = await self._fetch_ibkr_prices(unique_pos) if (unique_pos and settings.LIVE_BROKER.lower() == "ibkr") else {}
                # IBKR's own per-position marketPrice (native currency) wins so values match the broker's.
                try:
                    ib = self._ib if settings.LIVE_BROKER.lower() == "ibkr" else None
                    pf = {
                        (i.contract.symbol.upper(), (i.contract.currency or "USD").upper()): float(i.marketPrice)
                        for i in (ib.portfolio() if ib and ib.isConnected() else [])
                        if i.marketPrice == i.marketPrice and i.marketPrice > 0
                    }
                except Exception as e:
                    logger.debug(f"IBKR portfolio() prices unavailable: {e}")
                    pf = {}

                for p in positions:
                    is_open, _ = check_market_hours(p.ticker)
                    if not is_open and p.current_price_native and p.current_price_native > 0:
                        continue

                    curr_price = pf.get((p.ticker.upper().split(".")[0], (p.currency or "USD").upper())) or ib_prices.get(p.ticker)
                    if not curr_price:
                        data = await asyncio.to_thread(market_data_provider.get_stock_data, p.ticker)
                        if data:
                            curr_price = data["price"]

                    if curr_price:
                        p.current_price_native = curr_price
                        p.current_price_eur = currency_service.to_eur(curr_price, p.currency)
                        if curr_price > (p.highest_price_native or 0.0):
                            p.highest_price_native = curr_price
                            # Trailing stop (same as paper): past breakeven_trigger_pct the stop moves to breakeven + 1%,
                            # then trails by N x ATR. This is the bot's own stop; the broker's bracket stop keeps its level.
                            # Positions without a stop are left alone: those are holdings synced from the broker, not bot trades.
                            if p.stop_loss_native and p.entry_price_native:
                                gain_pct = (curr_price / p.entry_price_native) - 1.0
                                if gain_pct >= breakeven_trigger_pct:
                                    atr = None
                                    if settings.TRAILING_STOP_ATR_MULT > 0:
                                        atr_data = await asyncio.to_thread(market_data_provider.get_stock_data, p.ticker)
                                        atr = atr_data.get("technicals", {}).get("atr14") if atr_data else None
                                    new_stop = compute_trailing_stop(
                                        p.entry_price_native, p.stop_loss_native, p.highest_price_native, atr, breakeven_trigger_pct
                                    )
                                    if new_stop is not None and new_stop != p.stop_loss_native:
                                        p.stop_loss_native = new_stop
                                        logger.info(f"Live trailing stop raised for {p.ticker} to {new_stop} (+{gain_pct*100:.1f}%)")
                        curr_val_eur = p.current_price_eur * p.shares
                        p.unrealized_pnl_eur = round(curr_val_eur - p.cost_basis_eur, 2)
                        p.unrealized_pnl_pct = round(((p.current_price_eur / p.entry_price_eur) - 1.0) * 100, 2)

                await session.commit()
                self._last_pnl_refresh = time.time()

    async def _fetch_ibkr_account_values(self, ib: Any) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Reads (cash, net liquidation, gross position value) in base currency from the IBKR account summary."""
        ib_cash = None
        ib_total = None
        ib_equity = None
        for s in await ib.accountSummaryAsync():
            if s.currency in (settings.BASE_CURRENCY, "EUR", "BASE"):
                if s.tag in ("TotalCashValue", "CashBalance") and ib_cash is None:
                    ib_cash = float(s.value)
                elif s.tag == "NetLiquidation" and ib_total is None:
                    ib_total = float(s.value)
                elif s.tag == "GrossPositionValue" and ib_equity is None:
                    ib_equity = float(s.value)
        return ib_cash, ib_total, ib_equity

    async def sync_cash_balance(self) -> None:
        """
        Records the broker's current cash balance in a new snapshot without reconciling positions,
        so a transient empty position list from the broker can never close local positions.
        Keeps the last known balance when the broker is unreachable.
        """
        if settings.LIVE_BROKER.lower() != "ibkr":
            # The Alpaca sync only records balances and never touches positions
            await self.sync_account_state()
            return
        try:
            ib = await self._get_ib(timeout=5.0)
            if not (ib and ib.isConnected()):
                raise ConnectionError("Persistent IB client not connected.")
            ib_cash, ib_total, ib_equity = await self._fetch_ibkr_account_values(ib)
            if ib_cash is None:
                return

            async with AsyncSessionLocal() as session:
                snap_res = await session.execute(
                    select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == True).order_by(desc(PortfolioSnapshot.timestamp), desc(PortfolioSnapshot.id))
                )
                latest = snap_res.scalars().first()
                if latest and latest.cash_eur == ib_cash and (ib_total is None or latest.total_value_eur == ib_total) and (ib_equity is None or latest.equity_eur == ib_equity):
                    return

                pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
                equity_val = ib_equity if ib_equity is not None else 0.0
                session.add(PortfolioSnapshot(
                    cash_eur=ib_cash,
                    equity_eur=equity_val,
                    total_value_eur=ib_total if ib_total is not None else (ib_cash + equity_val),
                    daily_pnl_eur=0.0,
                    daily_pnl_pct=0.0,
                    open_positions_count=len(pos_res.scalars().all()),
                    is_live=True
                ))
                await session.commit()
                logger.info(f"Live cash balance synced from IBKR: €{ib_cash:.2f}")
        except Exception as e:
            logger.warning(f"IBKR cash balance sync note: {e}")

    async def _planned_levels(self, session, sym: str, entry: float) -> Tuple[float, float]:
        """Stop/target for a broker holding: the latest buy-plan trigger for the symbol, else the default percentages."""
        res = await session.execute(
            select(OrderTrigger).where(
                OrderTrigger.is_live == True,
                OrderTrigger.status.in_(["PENDING", "TRIGGERED"]),
                (OrderTrigger.ticker == sym) | OrderTrigger.ticker.like(f"{sym}.%"),
            ).order_by(desc(OrderTrigger.created_at)).limit(1)
        )
        t = res.scalars().first()
        sl = t.stop_loss if t and t.stop_loss else round(entry * (1.0 - settings.DEFAULT_STOP_LOSS_PCT), 2)
        tp = t.take_profit if t and t.take_profit else round(entry * (1.0 + settings.DEFAULT_TAKE_PROFIT_PCT), 2)
        return sl, tp

    async def sync_account_state(self, adopt_only: bool = False) -> Dict[str, Any]:
        """
        Polls broker API (IBKR / Alpaca) for live account values and positions,
        and aligns the local database snapshot. adopt_only (periodic use) never closes local positions.
        """
        logger.info(f"Syncing account state with {settings.LIVE_BROKER.upper()}...")
        broker = settings.LIVE_BROKER.lower()
        if broker == "ibkr":
            try:
                ib = await self._get_ib(timeout=5.0)
                if not (ib and ib.isConnected()):
                    raise ConnectionError("Persistent IB client not connected.")
                ib_cash, ib_total, ib_equity = await self._fetch_ibkr_account_values(ib)

                ib_positions = ib.positions()

                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == True))
                    # Local tickers carry Yahoo suffixes (ASML.AS) while IBKR reports the bare
                    # symbol (ASML), so match on (base symbol, currency).
                    existing_live = {
                        (p.ticker.upper().split(".")[0], (p.currency or "USD").upper()): p
                        for p in res.scalars().all()
                    }

                    ib_keys = set()
                    adopted = []
                    for p in ib_positions:
                        sym = p.contract.symbol.upper()
                        shares = float(p.position)
                        cost_native = float(p.avgCost)
                        curr_code = p.contract.currency or "USD"
                        key = (sym, curr_code.upper())
                        if shares <= 0:
                            logger.warning(f"IBKR reports non-long position {shares} {sym}; not tracked as an open position.")
                            continue
                        ib_keys.add(key)
                        cost_eur = currency_service.to_eur(cost_native, curr_code) * shares
                        if key in existing_live:
                            pos = existing_live[key]
                            # Keep the bot's own euro cost basis (incl. fees); re-converting avgCost at today's FX drifts it.
                            if abs(pos.shares - shares) > 1e-9:
                                if shares > pos.shares:
                                    pos.cost_basis_eur += currency_service.to_eur(cost_native, curr_code) * (shares - pos.shares)
                                else:
                                    pos.cost_basis_eur *= shares / pos.shares
                                pos.shares = shares
                            if not pos.stop_loss_native or not pos.take_profit_native:
                                sl, tp = await self._planned_levels(session, sym, pos.entry_price_native or cost_native)
                                pos.stop_loss_native = pos.stop_loss_native or sl
                                pos.take_profit_native = pos.take_profit_native or tp
                        else:
                            sl, tp = await self._planned_levels(session, sym, cost_native)
                            # IBKR avgCost already includes commission; sum the session's BUY commissions for fees_eur.
                            # shortcut: ib.fills() only covers this gateway session, so older adoptions keep fees 0; upgrade via reqExecutions.
                            fees_eur = sum(
                                currency_service.to_eur(f.commissionReport.commission, f.commissionReport.currency or curr_code)
                                for f in ib.fills()
                                if f.contract.conId == p.contract.conId and f.execution.side == "BOT"
                                and f.commissionReport and f.commissionReport.commission == f.commissionReport.commission
                            )
                            new_pos = Position(
                                ticker=sym,
                                name=sym,
                                currency=curr_code,
                                shares=shares,
                                entry_price_native=cost_native,
                                current_price_native=cost_native,
                                highest_price_native=cost_native,
                                stop_loss_native=sl,
                                take_profit_native=tp,
                                entry_price_eur=currency_service.to_eur(cost_native, curr_code),
                                current_price_eur=currency_service.to_eur(cost_native, curr_code),
                                cost_basis_eur=cost_eur,
                                fees_eur=round(fees_eur, 2),
                                status="OPEN",
                                is_live=True
                            )
                            session.add(new_pos)
                            adopted.append(f"{shares:g} {sym} @ {cost_native:.2f} {curr_code} | SL {sl} | TP {tp}")

                    for key, pos in existing_live.items():
                        if key not in ib_keys and not adopt_only:
                            pos.status = "CLOSED"

                    if ib_cash is not None:
                        equity_val = ib_equity if ib_equity is not None else 0.0
                        total_val = ib_total if ib_total is not None else (ib_cash + equity_val)
                        snapshot = PortfolioSnapshot(
                            cash_eur=ib_cash,
                            equity_eur=equity_val,
                            total_value_eur=total_val,
                            daily_pnl_eur=0.0,
                            daily_pnl_pct=0.0,
                            open_positions_count=len(ib_positions),
                            is_live=True
                        )
                        session.add(snapshot)
                    await session.commit()
                for a in adopted:
                    logger.warning(f"Adopted untracked IBKR holding: {a}")
                    try:
                        from src.telegram.bot import telegram_bot
                        await telegram_bot.send_message(f"⚠️ <b>Untracked broker position adopted</b>\n{a}")
                    except Exception as e:
                        logger.error(f"Failed to send adoption alert: {e}")
            except Exception as e:
                logger.warning(f"IBKR sync communication note: {e}")
        elif broker == "alpaca":
            try:
                from alpaca.trading.client import TradingClient
                if settings.ALPACA_API_KEY and settings.ALPACA_SECRET_KEY:
                    client = TradingClient(
                        api_key=settings.ALPACA_API_KEY,
                        secret_key=settings.ALPACA_SECRET_KEY,
                        paper=not settings.LIVE_TRADING_ENABLED
                    )
                    acct = client.get_account()
                    alpaca_cash = float(acct.cash)
                    alpaca_portfolio = float(acct.portfolio_value)
                    async with AsyncSessionLocal() as session:
                        snapshot = PortfolioSnapshot(
                            cash_eur=alpaca_cash,
                            equity_eur=alpaca_portfolio - alpaca_cash,
                            total_value_eur=alpaca_portfolio,
                            daily_pnl_eur=0.0,
                            daily_pnl_pct=0.0,
                            open_positions_count=0,
                            is_live=True
                        )
                        session.add(snapshot)
                        await session.commit()
            except Exception as e:
                logger.warning(f"Alpaca sync communication note: {e}")

        await self.refresh_positions_pnl()
        summary = await self.get_portfolio_summary()
        logger.info(
            f"Account sync complete. Mode: LIVE ({settings.LIVE_BROKER.upper()}) | "
            f"Cash: €{summary['cash_eur']} | Equity: €{summary['equity_eur']} | "
            f"Total: €{summary['total_value_eur']}"
        )
        return summary

    async def check_connection(self, force: bool = False) -> Dict[str, Any]:
        """
        Actively probes connectivity to the configured live brokerage API (IBKR / Alpaca).
        Caches the status for self._connection_check_ttl seconds unless force=True.
        """
        async with self._connection_lock:
            now = time.time()
            if not force and self._last_connection_status and (now - self._last_connection_check) < self._connection_check_ttl:
                return self._last_connection_status

            broker = settings.LIVE_BROKER.lower()
            utc_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            status_data: Dict[str, Any] = {
                "broker": settings.LIVE_BROKER.upper(),
                "trading_mode": "LIVE",
                "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
                "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
                "checked_at": utc_iso,
            }

            if broker == "ibkr":
                status_data["host"] = settings.IBKR_HOST
                status_data["port"] = settings.IBKR_PORT
                start_t = time.perf_counter()
                try:
                    from ib_async import IB
                    if self._ib is None:
                        self._ib = IB()
                        self._ib.disconnectedEvent += self._on_ib_disconnected
                        self._ib.errorEvent += self._on_ib_error

                    if not self._ib.isConnected():
                        await self._ib.connectAsync(
                            host=settings.IBKR_HOST,
                            port=settings.IBKR_PORT,
                            clientId=settings.IBKR_CLIENT_ID,
                            timeout=5.0,
                            readonly=False
                        )

                    latency = round((time.perf_counter() - start_t) * 1000, 1)
                    managed_accts = self._ib.managedAccounts()
                    server_version = self._ib.client.serverVersion()
                    acct_id = settings.IBKR_ACCOUNT_ID or (managed_accts[0] if managed_accts else None)

                    status_data.update({
                        "connected": True,
                        "status": "ONLINE",
                        "latency_ms": latency,
                        "server_version": server_version,
                        "accounts": managed_accts,
                        "account_id": acct_id,
                        "message": f"Connected to IBKR Gateway (Latency: {latency}ms, Server v{server_version})"
                    })
                except Exception as e:
                    elapsed = round((time.perf_counter() - start_t) * 1000, 1)
                    err_msg = str(e).strip() or type(e).__name__
                    hint = "Check if IB Gateway container is running and 2FA notification is approved on IBKR Mobile."
                    status_data.update({
                        "connected": False,
                        "status": "OFFLINE",
                        "latency_ms": elapsed,
                        "error": err_msg,
                        "account_id": settings.IBKR_ACCOUNT_ID,
                        "message": f"IBKR Gateway unreachable at {settings.IBKR_HOST}:{settings.IBKR_PORT} ({err_msg}). {hint}"
                    })
            elif broker == "alpaca":
                start_t = time.perf_counter()
                try:
                    from alpaca.trading.client import TradingClient
                    if not settings.ALPACA_API_KEY or not settings.ALPACA_SECRET_KEY:
                        raise ValueError("Alpaca API credentials missing in configuration.")
                    client = TradingClient(
                        api_key=settings.ALPACA_API_KEY,
                        secret_key=settings.ALPACA_SECRET_KEY,
                        paper=not settings.LIVE_TRADING_ENABLED
                    )
                    acct = await asyncio.to_thread(client.get_account)
                    latency = round((time.perf_counter() - start_t) * 1000, 1)
                    status_data.update({
                        "connected": True,
                        "status": "ONLINE",
                        "latency_ms": latency,
                        "account_number": acct.account_number,
                        "account_status": str(acct.status),
                        "message": f"Connected to Alpaca Trading API (Latency: {latency}ms, Status: {acct.status})"
                    })
                except Exception as e:
                    elapsed = round((time.perf_counter() - start_t) * 1000, 1)
                    err_msg = str(e).strip() or type(e).__name__
                    status_data.update({
                        "connected": False,
                        "status": "OFFLINE",
                        "latency_ms": elapsed,
                        "error": err_msg,
                        "message": f"Alpaca API unreachable: {err_msg}"
                    })
            else:
                status_data.update({
                    "connected": False,
                    "status": "UNKNOWN",
                    "latency_ms": None,
                    "message": f"Unknown broker configured: {broker}"
                })

            self._last_connection_status = status_data
            self._last_connection_check = now
            return status_data

live_broker = LiveBroker()
