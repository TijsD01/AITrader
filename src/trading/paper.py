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
from src.trading.risk import risk_guardrail
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

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        await self.refresh_positions_pnl()
        async with AsyncSessionLocal() as session:
            # Latest snapshot
            res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = res.scalars().first()
            
            # Active positions
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN"))
            positions = pos_res.scalars().all()

            # Active pending triggers
            trig_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending_triggers = trig_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)

            cash = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            available_cash = max(0.0, cash - pending_alloc)
            equity = sum(round(p.current_price_eur * p.shares, 2) for p in positions)
            total_val = round(cash + equity, 2)
            unrealized_pnl = sum(round(p.unrealized_pnl_eur, 2) for p in positions)
            initial_cap = settings.INITIAL_PAPER_CAPITAL
            total_return_pct = round(((total_val / initial_cap) - 1.0) * 100, 2)

            return {
                "base_currency": settings.BASE_CURRENCY,
                "cash_eur": round(cash, 2),
                "available_cash_eur": round(available_cash, 2),
                "reserved_triggers_eur": round(pending_alloc, 2),
                "equity_eur": round(equity, 2),
                "total_value_eur": total_val,
                "unrealized_pnl_eur": round(unrealized_pnl, 2),
                "total_return_pct": total_return_pct,
                "open_positions_count": len(positions),
                "pending_triggers_count": len(pending_triggers),
                "initial_capital_eur": initial_cap,
            }

    async def get_positions(self) -> List[Dict[str, Any]]:
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
            pos_res = await session.execute(select(Position).where(Position.status == "OPEN"))
            open_positions = pos_res.scalars().all()

            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL
            equity_eur = sum(p.current_price_eur * p.shares for p in open_positions)
            total_eur = cash_eur + equity_eur

            # Calculate available cash excluding the trigger being executed (if any)
            trig_query = select(OrderTrigger).where(OrderTrigger.status == "PENDING")
            if trigger_id:
                trig_query = trig_query.where(OrderTrigger.id != trigger_id)
            trig_res = await session.execute(trig_query)
            pending_triggers = trig_res.scalars().all()
            other_committed = sum(t.allocation_eur for t in pending_triggers)
            available_cash = max(0.0, cash_eur - other_committed)

            # Validate risk guardrails
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
                logger.info(f"Buy order blocked for {ticker}: {msg}")
                return {"success": False, "message": msg}

            # Hard cap: cannot invest more than available cash
            if allocation_eur > cash_eur:
                allocation_eur = cash_eur

            # Convert prices to EUR
            price_eur = currency_service.to_eur(price_native, currency)
            if price_eur <= 0:
                return {"success": False, "message": f"Invalid price EUR for {ticker}."}

            shares = round(allocation_eur / price_eur, 4)
            actual_cost_eur = round(shares * price_eur, 2)

            # Guard against rounding pushing cost above cash
            if actual_cost_eur > cash_eur:
                import math
                shares = math.floor((cash_eur / price_eur) * 10000.0) / 10000.0
                actual_cost_eur = round(shares * price_eur, 2)

            if shares <= 0 or actual_cost_eur <= 0:
                return {"success": False, "message": f"Insufficient cash (€{cash_eur:.2f}) to purchase {ticker}."}

            # Deduct cash strictly without negative balance
            new_cash_eur = round(cash_eur - actual_cost_eur, 2)
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
                rationale=rationale,
                consensus_score=consensus_score,
                executed_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trade)

            # Update Portfolio Snapshot
            new_equity_eur = equity_eur + actual_cost_eur
            new_snapshot = PortfolioSnapshot(
                cash_eur=new_cash_eur,
                equity_eur=new_equity_eur,
                total_value_eur=new_cash_eur + new_equity_eur,
                open_positions_count=len(open_positions) + 1,
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            logger.info(f"Executed BUY {shares} {ticker} @ {price_native} {currency} (Cost: €{actual_cost_eur})")

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
                select(Position).where(Position.ticker == ticker.upper(), Position.status == "OPEN")
            )
            pos = res.scalars().first()
            if not pos:
                return {"success": False, "message": f"No open position found for {ticker}."}

            # Fetch live exit price
            quote = market_data_provider.get_stock_data(ticker)
            exit_price_native = quote["price"] if quote else pos.current_price_native
            exit_price_eur = currency_service.to_eur(exit_price_native, pos.currency)
            proceeds_eur = round(pos.shares * exit_price_eur, 2)
            realized_pnl_eur = round(proceeds_eur - pos.cost_basis_eur, 2)
            realized_pnl_pct = round(((exit_price_eur / pos.entry_price_eur) - 1.0) * 100, 2)

            # Mark position closed
            pos.status = "CLOSED"
            pos.current_price_native = exit_price_native
            pos.current_price_eur = exit_price_eur

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
                realized_pnl_eur=realized_pnl_eur,
                realized_pnl_pct=realized_pnl_pct,
                rationale=reason,
                executed_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trade)

            # Update cash and snapshot
            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = (snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL) + proceeds_eur

            # Remaining active positions
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
                timestamp=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(new_snapshot)

            await session.commit()
            self._last_pnl_refresh = 0.0
            logger.info(f"Executed SELL {pos.shares} {ticker} @ {exit_price_native} {pos.currency} | PnL: €{realized_pnl_eur} ({realized_pnl_pct}%)")

            # Autonomous Post-Mortem & Parameter Calibration
            lesson_text = None
            adjustments = []
            try:
                from src.trading.learner import trade_learner
                post_mortem = await trade_learner.conduct_trade_post_mortem(trade.id)
                lesson_text = post_mortem.get("lesson") if post_mortem else None
                calib = await trade_learner.calibrate_internal_parameters(trade.id)
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

                res = await session.execute(select(Position).where(Position.status == "OPEN"))
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
                            # Dynamic Trailing Stop: If price moves up past breakeven_trigger_pct, ratchet up stop loss to breakeven or higher
                            gain_pct = (curr_price / p.entry_price_native) - 1.0
                            if gain_pct >= breakeven_trigger_pct and p.stop_loss_native < p.entry_price_native:
                                p.stop_loss_native = round(p.entry_price_native * 1.01, 2)  # Secure breakeven + 1%
                                logger.info(f"Trailing Stop ratcheted to breakeven for {p.ticker} at {p.stop_loss_native} (+{gain_pct*100:.1f}%)")
                        
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
            res = await session.execute(select(Position).where(Position.status == "OPEN"))
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
                    OrderTrigger.status == "PENDING"
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

            if trigger_type == "LIMIT_PULLBACK" and target_price >= current_price_at_creation:
                return {
                    "success": False,
                    "message": f"Trigger overlap error: LIMIT_PULLBACK target price ({target_price}) must be below current price ({current_price_at_creation})."
                }
            elif trigger_type == "BREAKOUT_BUY" and target_price <= current_price_at_creation:
                return {
                    "success": False,
                    "message": f"Trigger overlap error: BREAKOUT_BUY target price ({target_price}) must be above current price ({current_price_at_creation})."
                }

            if sl is not None and sl >= target_price:
                return {
                    "success": False,
                    "message": f"Trigger overlap error: Stop Loss ({sl}) must be below target price ({target_price})."
                }
            if tp is not None and tp <= target_price:
                return {
                    "success": False,
                    "message": f"Trigger overlap error: Take Profit ({tp}) must be above target price ({target_price})."
                }
            if sl is not None and tp is not None and sl >= tp:
                return {
                    "success": False,
                    "message": f"Trigger overlap error: Stop Loss ({sl}) must be below Take Profit ({tp})."
                }

            # 4. Anti-Overlap Check: Capital overlap (Cannot commit more cash than you have, no budget)
            snap_res = await session.execute(select(PortfolioSnapshot).order_by(desc(PortfolioSnapshot.timestamp)))
            snapshot = snap_res.scalars().first()
            cash_eur = snapshot.cash_eur if snapshot else settings.INITIAL_PAPER_CAPITAL

            pending_res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending_triggers = pending_res.scalars().all()
            pending_alloc = sum(t.allocation_eur for t in pending_triggers)
            available_cash = round(max(0.0, cash_eur - pending_alloc), 2)

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
                created_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            )
            session.add(trigger)
            await session.commit()
            await session.refresh(trigger)

            logger.info(f"Created {trigger_type} trigger for {ticker} at {target_price} {currency} (ID: {trigger.id})")
            return {
                "success": True,
                "trigger_id": trigger.id,
                "ticker": trigger.ticker,
                "currency": trigger.currency,
                "trigger_type": trigger.trigger_type,
                "target_price": trigger.target_price,
                "current_price": trigger.current_price_at_creation,
                "allocation_eur": trigger.allocation_eur,
                "message": f"AI Trigger armed for {trigger.ticker}: {trigger.trigger_type} @ {trigger.target_price} {trigger.currency}."
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

    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves order triggers with live distance % calculations."""
        async with AsyncSessionLocal() as session:
            query = select(OrderTrigger).order_by(desc(OrderTrigger.created_at))
            if status:
                query = query.where(OrderTrigger.status == status.upper())
            res = await session.execute(query)
            triggers = res.scalars().all()

            results = []
            quote_cache = {}
            for t in triggers:
                is_open, _ = check_market_hours(t.ticker)
                if not is_open:
                    curr_price = t.current_price_at_creation
                else:
                    if t.ticker not in quote_cache:
                        quote_cache[t.ticker] = await asyncio.to_thread(market_data_provider.get_stock_data, t.ticker)
                    latest_data = quote_cache[t.ticker]
                    curr_price = latest_data["price"] if latest_data else t.current_price_at_creation
                
                # Distance % to trigger: ((target_price / curr_price) - 1) * 100
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
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(OrderTrigger).where(OrderTrigger.status == "PENDING"))
            pending = res.scalars().all()
            if not pending:
                return []

            executed = []
            for t in pending:
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

                # 3. Check trigger condition
                condition_met = False
                if t.trigger_type == "LIMIT_PULLBACK" and curr_price <= t.target_price:
                    condition_met = True
                elif t.trigger_type == "BREAKOUT_BUY" and curr_price >= t.target_price:
                    condition_met = True

                if condition_met:
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
                        is_manual=True,
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
                    else:
                        logger.warning(f"Trigger #{t.id} buy order blocked for {t.ticker}: {trade_res.get('message')}")

            return executed

paper_broker = PaperBroker()
