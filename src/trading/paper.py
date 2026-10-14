import logging
import datetime
import asyncio
import time
from typing import Dict, Any, List, Optional
from sqlalchemy import select, desc

from config.settings import settings
from src.db.models import Position, Trade, PortfolioSnapshot, SystemConfig, OrderTrigger
from src.db.session import AsyncSessionLocal
from src.data.currency import currency_service
from src.data.market import market_data_provider
from src.trading.risk import risk_guardrail, get_effective_deployable_cash, allocate_pending_triggers, evaluate_trigger_condition, is_trigger_expired, validate_trigger_levels, compute_trailing_stop, check_market_regime, whole_share_count, new_entry_allocation, risk_capped_allocation, plan_pending_triggers
from src.data.market_hours import check_market_hours
from src.trading.base import BaseBroker

logger = logging.getLogger(__name__)

class PaperBroker(BaseBroker):
    def __init__(self):
        self._last_pnl_refresh: float = 0.0
        self._pnl_refresh_ttl: float = 15.0  # 15 seconds cache
        self._refresh_lock = asyncio.Lock()

    async def sync_account_state(self) -> Dict[str, Any]:
        """Refreshes positions and returns current summary for paper trading."""
        await self.refresh_positions_pnl()
        return await self.get_portfolio_summary()

    async def sync_cash_balance(self) -> None:
        """Paper cash is tracked locally, so there is no broker balance to pull."""
        return None

    async def check_connection(self, force: bool = False) -> Dict[str, Any]:
        """Returns simulated paper broker connection status."""
        return {
            "connected": True,
            "broker": "PAPER",
            "trading_mode": "PAPER",
            "status": "ONLINE",
            "message": "Paper Trading Simulation is active and ready.",
            "latency_ms": 0.1,
            "kill_switch_active": settings.KILL_SWITCH_ACTIVE,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        await self.refresh_positions_pnl()
        await self.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            # Latest snapshot
            res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = res.scalars().first()
            
            # Active positions
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
            positions = pos_res.scalars().all()

            # Active pending triggers
            trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False))
            pending_triggers = trig_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)

            cash = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            usable_cash, protected_reserve = get_effective_deployable_cash(cash)
            available_cash = max(0.0, usable_cash - pending_alloc)
            equity = sum(round(p.current_price_eur * p.shares, 2) for p in positions)
            total_val = round(cash + equity, 2)
            unrealized_pnl = sum(round(p.unrealized_pnl_eur, 2) for p in positions)
            initial_cap = settings.INITIAL_PAPER_CAPITAL
            total_return_pct = round(((total_val / initial_cap) - 1.0) * 100, 2)
            total_gain = round(total_val - initial_cap, 2)

            return {
                "trading_mode": "PAPER",
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
                "unrealized_pnl_eur": round(unrealized_pnl, 2),
                "total_return_pct": total_return_pct,
                "open_positions_count": len(positions),
                "pending_triggers_count": len(pending_triggers),
                "initial_capital_eur": initial_cap,
                "net_deposits_eur": 0.0,
                "invested_capital_eur": initial_cap,
                "total_gain_eur": total_gain,
            }

    async def get_positions(self) -> List[Dict[str, Any]]:
        await self.refresh_positions_pnl()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
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
        # Check market hours (No buying when market is closed)
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            logger.info(f"Buy order blocked for {ticker}: {open_msg}")
            return {"success": False, "message": open_msg}

        async with AsyncSessionLocal() as session:
            # Check system config for auto-buy toggle unless explicitly initiated/triggered
            if is_manual:
                auto_buy_enabled = True
            else:
                cfg_res = await session.execute(select(SystemConfig).where(SystemConfig.key == "AUTO_BUY_ENABLED"))
                cfg = cfg_res.scalars().first()
                auto_buy_enabled = (cfg.value.lower() == "true") if cfg else settings.AUTO_BUY_ENABLED

            # Get open positions & latest cash
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
            open_positions = pos_res.scalars().all()

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            usable_cash, _ = get_effective_deployable_cash(cash_eur)
            equity_eur = sum(p.current_price_eur * p.shares for p in open_positions)
            total_eur = cash_eur + equity_eur

            # Calculate available cash excluding the trigger being executed (if any)
            trig_query = select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False)
            if trigger_id:
                trig_query = trig_query.where(OrderTrigger.id != trigger_id)
            trig_res = await session.execute(trig_query)
            pending_triggers = trig_res.scalars().all()
            other_committed = sum(t.allocation_eur for t in pending_triggers)
            available_cash = max(0.0, usable_cash - other_committed)

            # Automated buys risk the same share of their slot (or portfolio, per RISK_BUDGET_BASE) at their stop
            if not is_manual:
                allocation_eur = risk_capped_allocation(allocation_eur, price_native, stop_loss_native, total_eur)

            # Validate risk guardrails
            valid, msg = risk_guardrail.validate_buy_order(
                ticker=ticker,
                current_positions=open_positions,
                cash_eur=cash_eur,
                portfolio_total_eur=total_eur,
                allocation_eur=allocation_eur,
                consensus_score=consensus_score,
                auto_buy_enabled=auto_buy_enabled,
                available_cash_eur=cash_eur if trigger_id else available_cash,
                price_native=price_native,
                currency=currency,
                stop_loss_native=stop_loss_native,
                take_profit_native=take_profit_native
            )
            if not valid:
                logger.info(f"Buy order blocked for {ticker}: {msg}")
                return {"success": False, "message": msg}

            # Hard cap: cannot invest more than available cash
            if allocation_eur > cash_eur:
                allocation_eur = cash_eur

            # Convert prices to EUR
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

            # Calculate estimated broker fee
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

            # Guard against rounding pushing cost + fee above cash
            if actual_cost_eur + fee_eur > cash_eur and cash_eur > fee_eur:
                import math
                net_spend = cash_eur - fee_eur
                step = 1.0 if settings.WHOLE_SHARES_ONLY else 10000.0
                shares = math.floor((net_spend / price_eur) * step) / step
                actual_cost_eur = round(shares * price_eur, 2)

            if shares <= 0 or actual_cost_eur <= 0:
                return {"success": False, "message": f"Insufficient cash (€{cash_eur:.2f}) to purchase {ticker}."}

            # Deduct cash strictly without negative balance (cost + fee)
            new_cash_eur = round(cash_eur - actual_cost_eur - fee_eur, 2)
            if new_cash_eur < 0.0:
                return {"success": False, "message": f"Execution halted: cash balance cannot be negative (€{new_cash_eur:.2f})."}

            # Create Position
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
                opened_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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
                rationale=f"{rationale} (Fee: €{fee_eur:.2f})",
                consensus_score=consensus_score,
                executed_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trade)

            # Update Portfolio Snapshot
            new_equity_eur = equity_eur + actual_cost_eur
            new_snapshot = PortfolioSnapshot(
                cash_eur=new_cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=round(new_cash_eur + new_equity_eur, 2),
                open_positions_count=len(open_positions) + 1,
                is_live=False,
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            logger.info(f"Executed BUY {shares} {ticker} @ {price_native} {currency} (Cost: €{actual_cost_eur})")
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
                "ticker": ticker,
                "shares": shares,
                "price_native": price_native,
                "price_eur": price_eur,
                "total_eur": actual_cost_eur,
                "stop_loss": stop_loss_native,
                "take_profit": take_profit_native,
                "message": f"Successfully bought {shares} shares of {ticker} for €{actual_cost_eur:.2f}."
            }

    async def execute_sell(self, ticker: str, reason: str = "Manual Close") -> Dict[str, Any]:
        # Check market hours (No selling when market is closed)
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            return {"success": False, "message": open_msg}

        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN", Position.is_live == False)
            )
            pos = res.scalars().first()
            if not pos:
                return {"success": False, "message": f"No open position found for {ticker}."}

            # Fetch live exit price
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

            # Net realized PnL deducting both buy and sell commissions
            realized_pnl_eur = round(proceeds_eur - pos.cost_basis_eur - total_fees_eur, 2)
            realized_pnl_pct = round(((realized_pnl_eur / pos.cost_basis_eur)) * 100, 2) if pos.cost_basis_eur > 0 else 0.0

            # Mark position closed
            pos.status = "CLOSED"
            pos.current_price_native = exit_price_native
            pos.current_price_eur = exit_price_eur
            pos.fees_eur = total_fees_eur

            # Record Trade
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
                rationale=f"{reason} (Total Fees: €{total_fees_eur:.2f})",
                is_live=False,
                executed_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trade)

            # Update cash and snapshot (proceeds minus exit fee)
            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            net_proceeds_eur = round(proceeds_eur - exit_fee_eur, 2)
            cash_eur = (snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL) + net_proceeds_eur

            # Remaining active positions
            remaining_res = await session.execute(
                select(Position).where(Position.status == "OPEN", Position.is_live == False, Position.ticker != ticker.upper())
            )
            remaining = remaining_res.scalars().all()
            new_equity_eur = sum(p.current_price_eur * p.shares for p in remaining)

            new_snapshot = PortfolioSnapshot(
                cash_eur=cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=round(cash_eur + new_equity_eur, 2),
                open_positions_count=len(remaining),
                is_live=False,
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            logger.info(f"Executed SELL {pos.shares} {ticker} @ {exit_price_native} {pos.currency} | PnL: €{realized_pnl_eur} ({realized_pnl_pct}%)")
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

            # Autonomous Post-Mortem & Parameter Calibration
            lesson_text = None
            adjustments = []
            try:
                from src.trading.learner import trade_learner
                post_mortem = await trade_learner.conduct_trade_post_mortem(trade.id)
                lesson_text = post_mortem.get("lesson") if post_mortem else None
                calib = await trade_learner.calibrate_internal_parameters(trade.id, automatic=True)
                adjustments = calib.get("adjustments", []) if calib else []
            except Exception as learn_err:
                logger.error(f"Error in trade learning post-mortem/calibration for trade #{trade.id}: {learn_err}")

            sell_res = {
                "success": True,
                "ticker": ticker,
                "shares": pos.shares,
                "exit_price_native": exit_price_native,
                "exit_price_eur": exit_price_eur,
                "proceeds_eur": proceeds_eur,
                "realized_pnl_eur": realized_pnl_eur,
                "realized_pnl_pct": realized_pnl_pct,
                "reason": reason,
                "message": f"Sold {pos.shares} {ticker} for €{proceeds_eur:.2f} (PnL: €{realized_pnl_eur:+.2f} / {realized_pnl_pct:+.2f}%)."
            }

            # Outbound Telegram notification with post-mortem & calibrated parameter adjustments
            try:
                from src.telegram.bot import telegram_bot
                await telegram_bot.notify_position_closed(
                    sell_info=sell_res,
                    adjustments=adjustments,
                    lesson=lesson_text
                )
            except Exception as notif_err:
                logger.error(f"Error dispatching position closed notification: {notif_err}")

            return sell_res

    async def refresh_positions_pnl(self):
        """Fetches latest quotes and updates unrealized PnL and trailing high for all open positions."""
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

                res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
                positions = res.scalars().all()
                if not positions:
                    self._last_pnl_refresh = now
                    return

                for p in positions:
                    # Skip expensive remote quote fetches if market is currently closed and price is already recorded
                    is_open, _ = check_market_hours(p.ticker)
                    if not is_open and p.current_price_native and p.current_price_native > 0:
                        continue

                    data = await asyncio.to_thread(market_data_provider.get_stock_data, p.ticker)
                    if data:
                        curr_price = data["price"]
                        p.current_price_native = curr_price
                        p.current_price_eur = currency_service.to_eur(curr_price, p.currency)
                        if curr_price > p.highest_price_native:
                            p.highest_price_native = curr_price
                            # Trailing stop: past breakeven_trigger_pct the stop moves to breakeven + 1%, then trails by N x ATR
                            new_stop = compute_trailing_stop(
                                p.entry_price_native, p.stop_loss_native, p.highest_price_native,
                                data.get("technicals", {}).get("atr14"), breakeven_trigger_pct
                            )
                            if new_stop is not None and new_stop != p.stop_loss_native:
                                p.stop_loss_native = new_stop
                                logger.info(f"Trailing stop raised for {p.ticker} to {new_stop} (high {p.highest_price_native})")
                        
                        # Calculate unrealized PnL
                        curr_val_eur = p.current_price_eur * p.shares
                        p.unrealized_pnl_eur = round(curr_val_eur - p.cost_basis_eur, 2)
                        p.unrealized_pnl_pct = round(((p.current_price_eur / p.entry_price_eur) - 1.0) * 100, 2)

                await session.commit()
                self._last_pnl_refresh = time.time()

    async def check_stops_and_targets(self) -> List[Dict[str, Any]]:
        """Scans open positions to trigger stop loss or take profit orders."""
        await self.refresh_positions_pnl()
        triggered = []
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
            positions = res.scalars().all()
            tickers_to_close = []

            for p in positions:
                if p.stop_loss_native and p.current_price_native <= p.stop_loss_native:
                    tickers_to_close.append((p.ticker, f"Stop-Loss triggered at {p.current_price_native} <= {p.stop_loss_native}"))
                elif p.take_profit_native and p.current_price_native >= p.take_profit_native:
                    tickers_to_close.append((p.ticker, f"Take-Profit triggered at {p.current_price_native} >= {p.take_profit_native}"))

        for ticker, reason in tickers_to_close:
            sell_res = await self.execute_sell(ticker, reason=reason)
            if sell_res.get("success"):
                triggered.append(sell_res)

        return triggered

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
        """Creates an active pending price trigger (limit pullback or breakout order)."""
        async with AsyncSessionLocal() as session:
            # 1. Anti-Overlap Check: Prevent arming trigger if position already open for this ticker
            pos_res = await session.execute(
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN")
            )
            existing_pos = pos_res.scalars().first()
            if existing_pos:
                return {
                    "success": False,
                    "message": f"Cannot arm trigger: an active position already exists for {ticker.upper()}."
                }

            # 2. Anti-Overlap Check: Prevent arming multiple pending triggers for the same ticker
            res = await session.execute(
                select(OrderTrigger).where(
                    OrderTrigger.ticker == ticker.upper(),
                    OrderTrigger.status == "PENDING",
                    OrderTrigger.is_live == False
                )
            )
            existing = res.scalars().first()
            if existing:
                return {
                    "success": False,
                    "message": f"A pending trigger already exists for {ticker.upper()} (ID: #{existing.id} @ {existing.target_price} {existing.currency})."
                }

            # 3. Anti-Overlap Check: Price level validation
            target_price = round(target_price, 2)
            current_price_at_creation = round(current_price_at_creation, 2)
            sl = round(stop_loss, 2) if stop_loss else None
            tp = round(take_profit, 2) if take_profit else None

            level_error = validate_trigger_levels(trigger_type, target_price, current_price_at_creation, sl, tp)
            if level_error:
                return {"success": False, "message": level_error}

            # 4. Anti-Overlap Check: Capital overlap (Cannot commit more cash than you have, no budget)
            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            usable_cash, _ = get_effective_deployable_cash(cash_eur)

            pending_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False))
            pending_triggers = pending_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)
            available_cash = round(max(0.0, usable_cash - pending_alloc), 2)

            if allocation_eur is not None and allocation_eur > available_cash:
                return {
                    "success": False,
                    "message": f"Cannot arm trigger: insufficient available cash (€{available_cash:.2f} available after €{pending_alloc:.2f} reserved for other triggers, required €{allocation_eur:.2f})."
                }

            # Without an explicit allocation, take the slot the dynamic re-allocation would give it
            if allocation_eur is None or allocation_eur <= 0:
                pos_count_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
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
                stop_loss_native=sl,
                take_profit_native=tp,
                broker="paper",
                pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
                max_fee_drag_pct=getattr(settings, "MAX_FEE_DRAG_PCT", 3.8),
                max_profit_erosion_pct=getattr(settings, "MAX_PROFIT_EROSION_PCT", 48.0)
            )
            if not viability["viable"]:
                min_v = viability.get("min_viable_allocation_eur", 50.0)
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
                    "message": f"Cannot arm trigger: insufficient available cash (€{available_cash:.2f} available after €{pending_alloc:.2f} reserved for other triggers, required €{allocation_eur:.2f})."
                }
            if allocation_eur < 20.0:
                return {
                    "success": False,
                    "message": f"Cannot arm trigger: allocation amount €{allocation_eur:.2f} is too small (< €20)."
                }

            trigger = OrderTrigger(
                ticker=ticker.upper(),
                name=name,
                currency=currency,
                trigger_type=trigger_type,
                target_price=target_price,
                current_price_at_creation=current_price_at_creation,
                stop_loss=sl,
                take_profit=tp,
                allocation_eur=round(allocation_eur, 2),
                consensus_score=consensus_score,
                ai_rationale=ai_rationale,
                status="PENDING",
                is_live=False,
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

            logger.info(f"Created {trigger_type} trigger for {ticker} at {target_price} {currency} (ID: {trigger.id})")
            result = {
                "success": True,
                "trigger_id": trigger.id,
                "ticker": trigger.ticker,
                "currency": trigger.currency,
                "trigger_type": trigger.trigger_type,
                "target_price": trigger.target_price,
                "current_price": trigger.current_price_at_creation,
                "stop_loss": trigger.stop_loss,
                "take_profit": trigger.take_profit,
                "allocation_eur": trigger.allocation_eur,
                "message": f"AI Trigger armed for {trigger.ticker}: {trigger.trigger_type} @ {trigger.target_price} {trigger.currency}."
            }

        # Report the allocation the dashboard and the buy will use (after the dynamic re-allocation), not the amount asked for
        result.update(await self.get_trigger_plan(result["trigger_id"]))
        return result

    async def _pending_trigger_plans(self, session) -> Dict[int, Dict[str, Any]]:
        """Planned budget and share count per pending paper trigger, sized like execute_buy at the limit price."""
        trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False))
        pending = trig_res.scalars().all()
        if not pending:
            return {}
        pos_res = await session.execute(select(Position).where(Position.status == "OPEN", Position.is_live == False))
        positions = pos_res.scalars().all()
        snap_res = await session.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
        )
        snapshot = snap_res.scalars().first()
        cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
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
        """Cancels a pending order trigger."""
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.id == trigger_id))
            trigger = res.scalars().first()
            if not trigger:
                return {"success": False, "message": f"Trigger #{trigger_id} not found."}
            if trigger.status != "PENDING":
                return {"success": False, "message": f"Trigger #{trigger_id} is already {trigger.status}."}
            
            trigger.status = "CANCELLED"
            await session.commit()
            logger.info(f"Cancelled trigger #{trigger_id} for {trigger.ticker}")
            return {"success": True, "message": f"Trigger #{trigger_id} for {trigger.ticker} cancelled."}

    async def adjust_triggers_dynamically(self) -> List[Dict[str, Any]]:
        """
        Dynamically adjusts allocation_eur on all pending paper triggers based on
        current cash balance, open positions, and MAX_POSITIONS settings.
        Highest consensus scores are funded first; triggers left without cash or a
        free position slot are set to €0 and stay armed until funding frees up.
        """
        async with AsyncSessionLocal() as session:
            trig_res = await session.execute(
                select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False)
            )
            pending = trig_res.scalars().all()
            if not pending:
                return []

            pos_res = await session.execute(
                select(Position).where(Position.status == "OPEN", Position.is_live == False)
            )
            open_positions = pos_res.scalars().all()
            current_open = len(open_positions)

            snap_res = await session.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.is_live == False).order_by(desc(PortfolioSnapshot.timestamp))
            )
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
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
                logger.info(f"Dynamically adjusted {len(adjusted)} paper triggers (cash €{usable_cash:.2f}, {current_open}/{settings.MAX_POSITIONS} slots)")

            return adjusted

    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves order triggers with live distance % calculations."""
        await self.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            query = select(OrderTrigger).where(OrderTrigger.is_live == False).order_by(desc(OrderTrigger.created_at))
            if status:
                query = query.where(OrderTrigger.status == status.upper())
            res = await session.execute(query)
            triggers = res.scalars().all()
            plans = await self._pending_trigger_plans(session)

            results = []
            quote_cache = {}
            for t in triggers:
                curr_price = None
                if t.status == "PENDING":
                    # Last known quote, also while the market is closed: the price at creation is stale
                    if t.ticker not in quote_cache:
                        quote_cache[t.ticker] = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                    latest_data = quote_cache[t.ticker]
                    curr_price = latest_data["price"] if latest_data else None

                curr_price = curr_price or t.current_price_at_creation or t.target_price
                # Distance % to trigger: ((target_price / curr_price) - 1) * 100
                dist_pct = round(((t.target_price / curr_price) - 1.0) * 100, 2) if (curr_price and curr_price > 0) else 0.0

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
                    "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
                    "triggered_at": t.triggered_at.strftime("%Y-%m-%d %H:%M") if t.triggered_at else None
                })
            return results

    async def check_and_execute_triggers(self) -> List[Dict[str, Any]]:
        """
        Scans all pending triggers, verifies market hours,
        evaluates price condition, and executes automated buys.
        """
        await self.adjust_triggers_dynamically()
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING", OrderTrigger.is_live == False))
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

                # 0. Unfunded triggers (outranked for cash or slots) wait until funding frees up
                if t.allocation_eur <= 0:
                    continue

                # 1. Market hours check (STRICT: no trades when market is closed)
                is_open, open_msg = check_market_hours(t.ticker)
                if not is_open:
                    logger.debug(f"Trigger #{t.id} for {t.ticker} paused: {open_msg}")
                    continue

                # 2. Live market quote
                data = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                if not data:
                    continue
                curr_price = data["price"]

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
                        logger.info(f"Trigger #{t.id} for {t.ticker} held back: {regime_msg}")
                        continue
                    logger.info(f"🎯 AI Trigger hit for {t.ticker}: {t.trigger_type} at {curr_price} (Target: {t.target_price})")
                    trade_res = await self.execute_buy(
                        ticker=t.ticker,
                        name=t.name or t.ticker,
                        price_native=curr_price,
                        currency=t.currency,
                        stop_loss_native=t.stop_loss,
                        take_profit_native=t.take_profit,
                        allocation_eur=t.allocation_eur,
                        consensus_score=t.consensus_score or 75,
                        rationale=f"AI Trigger #{t.id} Hit ({t.trigger_type} @ {t.target_price}): {t.ai_rationale}",
                        is_manual=False,
                        trigger_id=t.id
                    )
                    if trade_res.get("success"):
                        t.status = "TRIGGERED"
                        t.triggered_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                        await session.commit()

                        trade_res["trigger_id"] = t.id
                        trade_res["trigger_type"] = t.trigger_type
                        trade_res["target_price"] = t.target_price
                        trade_res["executed_price"] = curr_price
                        trade_res["currency"] = t.currency
                        trade_res["ai_rationale"] = t.ai_rationale
                        executed.append(trade_res)
                    elif trade_res.get("unaffordable"):
                        # Not even one whole share fits: release the reserved cash for other triggers
                        logger.info(f"Cancelling trigger #{t.id} for {t.ticker}: {trade_res.get('message')}")
                        t.status = "CANCELLED"
                        await session.commit()
                    else:
                        logger.warning(f"Trigger #{t.id} buy order blocked for {t.ticker}: {trade_res.get('message')}")

            return executed

paper_broker = PaperBroker()
