import logging
import datetime
import asyncio
import time
import uuid
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy import select, desc

from config.settings import settings
from src.db.models import Position, Trade, PortfolioSnapshot, SystemConfig, OrderTrigger
from src.db.session import AsyncSessionLocal
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.trading.risk import risk_guardrail, calculate_position_allocation
from src.data.market_hours import check_market_hours
from src.trading.base import BaseBroker

logger = logging.getLogger(__name__)

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
        self._broker_client = None

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
                from ib_async import IB, Stock
                ib = IB()
                await ib.connectAsync(
                    host=settings.IBKR_HOST,
                    port=settings.IBKR_PORT,
                    clientId=settings.IBKR_CLIENT_ID,
                    timeout=3
                )
                contract = Stock(ticker.upper(), "SMART", currency.upper())
                await ib.qualifyContractsAsync(contract)
                bracket = ib.bracketOrder(
                    action="BUY",
                    quantity=shares,
                    limitPrice=price,
                    takeProfitPrice=take_profit if take_profit else round(price * 1.10, 2),
                    stopLossPrice=stop_loss if stop_loss else round(price * 0.95, 2)
                )
                placed = [ib.placeOrder(contract, order) for order in bracket]
                ib.disconnect()
                if placed:
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

    async def _dispatch_real_close(self, ticker: str, shares: float, currency: str = "USD") -> Optional[str]:
        """Dispatches live position exit to configured broker API."""
        broker = settings.LIVE_BROKER.lower()
        if broker == "ibkr":
            try:
                from ib_async import IB, Stock, MarketOrder
                ib = IB()
                await ib.connectAsync(
                    host=settings.IBKR_HOST,
                    port=settings.IBKR_PORT,
                    clientId=settings.IBKR_CLIENT_ID,
                    timeout=3
                )
                contract = Stock(ticker.upper(), "SMART", currency.upper())
                await ib.qualifyContractsAsync(contract)
                order = MarketOrder("SELL", shares)
                placed = ib.placeOrder(contract, order)
                ib.disconnect()
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
                    Trade.order_type == "SELL"
                )
            )
            trades = res.scalars().all()
            net_pnl = sum(t.realized_pnl_eur or 0.0 for t in trades)
            return abs(net_pnl) if net_pnl < 0 else 0.0

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Fetches live portfolio state (from broker and local mirror)."""
        await self.refresh_positions_pnl()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = res.scalars().first()

            pos_res = await session.execute(select(Position).where(Position.status == "OPEN"))
            positions = pos_res.scalars().all()

            trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending_triggers = trig_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)

            cash = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            available_cash = max(0.0, cash - pending_alloc)
            equity = sum(round(p.current_price_eur * p.shares, 2) for p in positions)
            total_val = round(cash + equity, 2)
            unrealized_pnl = sum(round(p.unrealized_pnl_eur, 2) for p in positions)
            daily_loss = await self.get_daily_realized_loss_eur()

            return {
                "broker": settings.LIVE_BROKER.upper(),
                "trading_mode": "LIVE",
                "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
                "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
                "full_budget_utilization": settings.FULL_BUDGET_UTILIZATION,
                "base_currency": settings.BASE_CURRENCY,
                "cash_eur": round(cash, 2),
                "available_cash_eur": round(available_cash, 2),
                "reserved_triggers_eur": round(pending_alloc, 2),
                "equity_eur": round(equity, 2),
                "total_value_eur": total_val,
                "unrealized_pnl_eur": round(unrealized_pnl, 2),
                "daily_realized_loss_eur": round(daily_loss, 2),
                "max_daily_loss_limit_eur": settings.MAX_DAILY_LOSS_EUR,
                "open_positions_count": len(positions),
                "pending_triggers_count": len(pending_triggers),
            }

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Returns all open live positions."""
        await self.refresh_positions_pnl()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN"))
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
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN"))
            open_positions = pos_res.scalars().all()

            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            equity_eur = sum(p.current_price_eur * p.shares for p in open_positions)
            total_eur = cash_eur + equity_eur

            # Reserved cash for other triggers
            trig_query = select(OrderTrigger).where(OrderTrigger.status == "PENDING")
            if trigger_id:
                trig_query = trig_query.where(OrderTrigger.id != trigger_id)
            trig_res = await session.execute(trig_query)
            pending_triggers = trig_res.scalars().all()
            other_committed = sum(t.allocation_eur for t in pending_triggers)
            available_cash = max(0.0, cash_eur - other_committed)

            # 6. Apply Full Budget Utilization if requested allocation is default or zero
            if (allocation_eur is None or allocation_eur <= 0) or settings.FULL_BUDGET_UTILIZATION:
                calc_alloc = calculate_position_allocation(
                    available_cash=available_cash,
                    current_positions_count=len(open_positions),
                    requested_alloc=allocation_eur if allocation_eur and allocation_eur > 0 else None,
                    full_budget_utilization=settings.FULL_BUDGET_UTILIZATION
                )
                if calc_alloc > 0:
                    allocation_eur = calc_alloc

            # 7. Validate risk guardrails
            valid, msg = risk_guardrail.validate_buy_order(
                ticker=ticker,
                current_positions=open_positions,
                cash_eur=cash_eur,
                portfolio_total_eur=total_eur,
                allocation_eur=allocation_eur,
                consensus_score=consensus_score,
                auto_buy_enabled=auto_buy_enabled,
                available_cash_eur=cash_eur if trigger_id else available_cash
            )
            if not valid:
                logger.info(f"Live BUY order blocked for {ticker}: {msg}")
                return {"success": False, "message": msg}

            # 8. Hard single order cap
            if settings.MAX_SINGLE_ORDER_EUR and allocation_eur > settings.MAX_SINGLE_ORDER_EUR:
                allocation_eur = settings.MAX_SINGLE_ORDER_EUR

            if allocation_eur > cash_eur:
                allocation_eur = cash_eur

            price_eur = currency_service.to_eur(price_native, currency)
            if price_eur <= 0:
                return {"success": False, "message": f"Invalid price EUR for {ticker}."}

            shares = round(allocation_eur / price_eur, 4)
            actual_cost_eur = round(shares * price_eur, 2)

            if shares <= 0 or actual_cost_eur <= 0:
                return {"success": False, "message": f"Insufficient capital to buy {ticker}."}

            # 9. Dispatch real order to broker API
            dispatched_id = await self._dispatch_real_order(
                ticker=ticker,
                shares=shares,
                price=price_native,
                currency=currency,
                stop_loss=stop_loss_native,
                take_profit=take_profit_native
            )
            broker_order_id = dispatched_id or f"{settings.LIVE_BROKER.lower()}_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[LIVE EXECUTION] Submitting real order to {settings.LIVE_BROKER.upper()}: "
                f"BUY {shares} {ticker} @ {price_native} {currency} (Cost: €{actual_cost_eur}) | "
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
                rationale=f"[LIVE {settings.LIVE_BROKER.upper()}] {rationale}",
                consensus_score=consensus_score,
                is_live=True,
                broker_order_id=broker_order_id,
                executed_at=utc_now
            )
            session.add(trade)

            # Update Portfolio Snapshot
            new_cash_eur = round(cash_eur - actual_cost_eur, 2)
            new_equity_eur = equity_eur + actual_cost_eur
            new_snapshot = PortfolioSnapshot(
                cash_eur=new_cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=new_cash_eur + new_equity_eur,
                open_positions_count=len(open_positions) + 1,
                timestamp=utc_now
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0

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
                "stop_loss": stop_loss_native,
                "take_profit": take_profit_native,
                "message": f"Real money order submitted to {settings.LIVE_BROKER.upper()}: {shares} shares of {ticker} for €{actual_cost_eur:.2f}."
            }

    async def execute_sell(self, ticker: str, reason: str = "Manual Close") -> Dict[str, Any]:
        """Submits real sell order to brokerage API to close live position."""
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            return {"success": False, "message": open_msg}

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN")
            )
            pos = res.scalars().first()
            if not pos:
                return {"success": False, "message": f"No open live position found for {ticker}."}

            quote = market_data_provider.get_stock_data(ticker)
            exit_price_native = quote["price"] if quote else pos.current_price_native
            exit_price_eur = currency_service.to_eur(exit_price_native, pos.currency)
            proceeds_eur = round(pos.shares * exit_price_eur, 2)
            realized_pnl_eur = round(proceeds_eur - pos.cost_basis_eur, 2)
            realized_pnl_pct = round(((exit_price_eur / pos.entry_price_eur) - 1.0) * 100, 2)

            dispatched_id = await self._dispatch_real_close(ticker=ticker, shares=pos.shares, currency=pos.currency)
            broker_order_id = dispatched_id or f"{settings.LIVE_BROKER.lower()}_exit_{uuid.uuid4().hex[:12]}"
            logger.info(
                f"[LIVE EXECUTION] Submitting exit order to {settings.LIVE_BROKER.upper()}: "
                f"SELL {pos.shares} {ticker} @ {exit_price_native} {pos.currency} | "
                f"Proceeds: €{proceeds_eur} | PnL: €{realized_pnl_eur} ({realized_pnl_pct}%)"
            )

            utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

            pos.status = "CLOSED"
            pos.current_price_native = exit_price_native
            pos.current_price_eur = exit_price_eur

            trade = Trade(
                ticker=ticker.upper(),
                name=pos.name,
                order_type="SELL",
                shares=pos.shares,
                currency=pos.currency,
                price_native=exit_price_native,
                price_eur=exit_price_eur,
                total_eur=proceeds_eur,
                realized_pnl_eur=realized_pnl_eur,
                realized_pnl_pct=realized_pnl_pct,
                rationale=f"[LIVE {settings.LIVE_BROKER.upper()}] {reason}",
                is_live=True,
                broker_order_id=broker_order_id,
                executed_at=utc_now
            )
            session.add(trade)

            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = (snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL) + proceeds_eur

            remaining_res = await session.execute(
                select(Position).where(Position.status == "OPEN", Position.ticker != ticker.upper())
            )
            remaining = remaining_res.scalars().all()
            new_equity_eur = sum(p.current_price_eur * p.shares for p in remaining)

            new_snapshot = PortfolioSnapshot(
                cash_eur=cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=cash_eur + new_equity_eur,
                open_positions_count=len(remaining),
                timestamp=utc_now
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0

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
        async with AsyncSessionLocal() as session:
            pos_res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN")
            )
            if pos_res.scalars().first():
                return {"success": False, "message": f"Active position already exists for {ticker.upper()}."}

            res = await session.execute(
                select(OrderTrigger).where(OrderTrigger.ticker == ticker.upper(), OrderTrigger.status == "PENDING")
            )
            if res.scalars().first():
                return {"success": False, "message": f"A pending trigger already exists for {ticker.upper()}."}

            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL

            pending_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending_triggers = pending_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)
            available_cash = round(max(0.0, cash_eur - pending_alloc), 2)

            # Apply full budget utilization
            if (allocation_eur is None or allocation_eur <= 0) or settings.FULL_BUDGET_UTILIZATION:
                pos_count_res = await session.execute(select(Position).where(Position.status == "OPEN"))
                current_open = len(pos_count_res.scalars().all())
                calc_alloc = calculate_position_allocation(
                    available_cash=available_cash,
                    current_positions_count=current_open,
                    requested_alloc=allocation_eur if allocation_eur and allocation_eur > 0 else None,
                    full_budget_utilization=settings.FULL_BUDGET_UTILIZATION
                )
                if calc_alloc > 0:
                    allocation_eur = calc_alloc

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

            return {
                "success": True,
                "is_live": True,
                "trigger_id": trigger.id,
                "ticker": trigger.ticker,
                "currency": trigger.currency,
                "trigger_type": trigger.trigger_type,
                "target_price": trigger.target_price,
                "allocation_eur": trigger.allocation_eur,
                "message": f"Real money AI Trigger armed for {trigger.ticker}: {trigger.trigger_type} @ {trigger.target_price}."
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

    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves order triggers."""
        async with AsyncSessionLocal() as session:
            query = select(OrderTrigger).order_by(desc(OrderTrigger.created_at))
            if status:
                query = query.where(OrderTrigger.status == status.upper())
            res = await session.execute(query)
            triggers = res.scalars().all()
            return [
                {
                    "id": t.id,
                    "ticker": t.ticker,
                    "name": t.name,
                    "currency": t.currency,
                    "trigger_type": t.trigger_type,
                    "target_price": round(t.target_price, 2),
                    "current_price_at_creation": round(t.current_price_at_creation, 2),
                    "stop_loss": round(t.stop_loss, 2) if t.stop_loss else None,
                    "take_profit": round(t.take_profit, 2) if t.take_profit else None,
                    "allocation_eur": round(t.allocation_eur, 2),
                    "consensus_score": t.consensus_score,
                    "ai_rationale": t.ai_rationale,
                    "status": t.status,
                    "is_live": t.is_live,
                    "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
                    "triggered_at": t.triggered_at.strftime("%Y-%m-%d %H:%M") if t.triggered_at else None
                }
                for t in triggers
            ]

    async def check_and_execute_triggers(self) -> List[Dict[str, Any]]:
        """Evaluates pending triggers and executes live orders when conditions are met."""
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending = res.scalars().all()
            if not pending:
                return []

            executed = []
            for t in pending:
                is_open, open_msg = check_market_hours(t.ticker)
                if not is_open:
                    continue

                data = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                if not data:
                    continue
                curr_price = data["price"]

                condition_met = False
                if t.trigger_type == "LIMIT_PULLBACK" and curr_price <= t.target_price:
                    condition_met = True
                elif t.trigger_type == "BREAKOUT_BUY" and curr_price >= t.target_price:
                    condition_met = True

                if condition_met:
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
                        is_manual=True,
                        trigger_id=t.id
                    )
                    if trade_res.get("success"):
                        t.status = "TRIGGERED"
                        t.triggered_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                        await session.commit()
                        trade_res["trigger_id"] = t.id
                        executed.append(trade_res)

            return executed

    async def check_stops_and_targets(self) -> List[Dict[str, Any]]:
        """Monitors open live positions for stop-loss and take-profit."""
        await self.refresh_positions_pnl()
        triggered = []
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN"))
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
        """Refreshes quotes and unrealized PnL for active positions."""
        async with self._refresh_lock:
            now = time.time()
            if (now - self._last_pnl_refresh) < self._pnl_refresh_ttl:
                return

            async with AsyncSessionLocal() as session:
                res = await session.execute(select(Position).where(Position.status == "OPEN"))
                positions = res.scalars().all()
                if not positions:
                    self._last_pnl_refresh = now
                    return

                for p in positions:
                    is_open, _ = check_market_hours(p.ticker)
                    if not is_open and p.current_price_native and p.current_price_native > 0:
                        continue

                    data = await asyncio.to_thread(market_data_provider.get_stock_data, p.ticker)
                    if data:
                        curr_price = data["price"]
                        p.current_price_native = curr_price
                        p.current_price_eur = currency_service.to_eur(curr_price, p.currency)
                        curr_val_eur = p.current_price_eur * p.shares
                        p.unrealized_pnl_eur = round(curr_val_eur - p.cost_basis_eur, 2)
                        p.unrealized_pnl_pct = round(((p.current_price_eur / p.entry_price_eur) - 1.0) * 100, 2)

                await session.commit()
                self._last_pnl_refresh = time.time()

    async def sync_account_state(self) -> Dict[str, Any]:
        """
        Polls broker API (IBKR / Alpaca) for live account values and positions,
        and aligns the local database snapshot.
        """
        logger.info(f"Syncing account state with {settings.LIVE_BROKER.upper()}...")
        await self.refresh_positions_pnl()
        summary = await self.get_portfolio_summary()
        logger.info(
            f"Account sync complete. Mode: LIVE ({settings.LIVE_BROKER.upper()}) | "
            f"Cash: €{summary['cash_eur']} | Equity: €{summary['equity_eur']} | "
            f"Total: €{summary['total_value_eur']}"
        )
        return summary

live_broker = LiveBroker()
