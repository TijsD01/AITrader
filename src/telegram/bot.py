import asyncio
import datetime
import html
import logging
import re
import math
import time
from typing import Optional, Dict, Any, List, Union
import httpx
from config.settings import settings
from src.data.market import market_data_provider
from src.agents.committee import investment_committee
from src.trading.broker import active_broker as paper_broker, get_active_broker, active_broker, live_broker
from src.trading.risk import derive_trigger_levels, entry_allocation_from_summary
from src.trading.trigger_gate import (
    normalize_ticker, invalid_ticker_result, precheck_manual_trigger, evaluate_trigger_verdict,
    evaluate_manual_trigger, get_trigger_min_score
)

logger = logging.getLogger(__name__)

class TelegramAssistant:
    def __init__(self):
        self.token = settings.TELEGRAM_BOT_TOKEN
        self.chat_id = settings.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}" if self.token else None
        self._polling_task: Optional[asyncio.Task] = None
        self._is_running = False
        self._last_update_id = 0

    def is_configured(self) -> bool:
        return bool(self.token and len(self.token) >= 10 and not self.token.startswith("your_"))

    def _chunk_text(self, text: str, max_len: int = 4000) -> List[str]:
        import textwrap
        return textwrap.wrap(text, width=max_len, replace_whitespace=False, drop_whitespace=False) or [""]

    async def send_message(self, text: str, chat_id: Optional[Union[int, str]] = None, parse_mode: str = "HTML") -> bool:
        """Sends a notification to target chat_id (or configured chat_id) with chunking and fallback."""
        target_chat = str(chat_id if chat_id is not None else (self.chat_id or ""))
        if not self.is_configured() or not target_chat:
            logger.debug(f"[Telegram Push (Mock)]: {text}")
            return False

        chunks = self._chunk_text(text, max_len=4000)
        overall_success = True
        url = f"{self.base_url}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for chunk in chunks:
                    payload = {
                        "chat_id": target_chat,
                        "text": chunk,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True
                    }
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        continue
                    logger.warning(f"Telegram send failed ({resp.status_code}): {resp.text}")
                    # If HTML parsing failed (400 Bad Request), fallback to plain text so notifications are NEVER dropped
                    if resp.status_code == 400 and parse_mode:
                        logger.info("Retrying Telegram notification using plain text fallback...")
                        plain_text = html.unescape(re.sub(r"<[^>]+>", "", chunk))
                        fallback_payload = {
                            "chat_id": target_chat,
                            "text": plain_text,
                            "disable_web_page_preview": True
                        }
                        retry_resp = await client.post(url, json=fallback_payload)
                        if retry_resp.status_code != 200:
                            logger.error(f"Telegram fallback send failed ({retry_resp.status_code}): {retry_resp.text}")
                            overall_success = False
                    else:
                        overall_success = False
        except Exception as e:
            logger.error(f"Error sending Telegram notification: {e}")
            return False
        return overall_success

    async def notify_trade(self, trade_result: Dict[str, Any]):
        """Broadcasts an automated trade execution alert."""
        if not trade_result.get("success"):
            return

        ticker = trade_result.get("ticker")
        shares = trade_result.get("shares")
        price_native = trade_result.get("price_native")
        price_eur = trade_result.get("price_eur")
        cost_eur = trade_result.get("total_eur")
        sl = trade_result.get("stop_loss")
        tp = trade_result.get("take_profit")

        msg = (
            f"🚀 <b>AUTOMATED BUY ORDER EXECUTED</b>\n\n"
            f"<b>Asset:</b> <code>{ticker}</code>\n"
            f"<b>Position:</b> {shares} shares\n"
            f"<b>Entry Price:</b> {price_native} (≈ €{price_eur:.2f})\n"
            f"<b>Total Allocation:</b> €{cost_eur:.2f}\n"
            f"<b>Stop Loss:</b> {sl}\n"
            f"<b>Take Profit:</b> {tp}\n\n"
            f"🛡️ <i>Risk controls active. Trailing stop-loss engaged.</i>"
        )
        await self.send_message(msg)

    @staticmethod
    def _planned_buy_line(trigger_info: Dict[str, Any]) -> str:
        """Shares the trigger will buy at its limit price (same sizing as the dashboard and the order)."""
        shares = trigger_info.get("planned_shares")
        if shares is None:
            return ""
        if shares <= 0:
            return "<b>Planned Buy:</b> budget below one share (skipped when hit)\n"
        capped = " after risk cap" if trigger_info.get("risk_capped") else ""
        return f"<b>Planned Buy:</b> ≈ {shares:g} shares (≈ €{trigger_info.get('planned_cost_eur', 0.0):,.2f}{capped})\n"

    async def notify_trigger_armed(self, trigger_info: Dict[str, Any]):
        """Broadcasts an alert when an AI limit price trigger has been armed."""
        ticker = trigger_info.get("ticker")
        trigger_type = trigger_info.get("trigger_type", "LIMIT_PULLBACK")
        target_pr = trigger_info.get("target_price")
        curr_pr = trigger_info.get("current_price")
        alloc = trigger_info.get("allocation_eur", 0.0)
        curr = trigger_info.get("currency", "USD")
        dist = round(((target_pr / curr_pr) - 1.0) * 100, 1) if curr_pr else 0.0

        msg = (
            f"🎯 <b>AI PRICE TRIGGER ARMED</b>\n\n"
            f"<b>Asset:</b> <code>{ticker}</code>\n"
            f"<b>Order Type:</b> {trigger_type}\n"
            f"<b>Target Trigger Price:</b> {target_pr} {curr} ({dist:+.1f}% from current {curr_pr})\n"
            f"<b>Planned Allocation:</b> €{alloc:,.2f}\n"
            f"{self._planned_buy_line(trigger_info)}"
            f"<b>Trigger ID:</b> #{trigger_info.get('trigger_id')}\n\n"
            f"⏳ <i>When price reaches {target_pr} {curr} during open market hours, the order will execute automatically.</i>"
        )
        await self.send_message(msg)

    async def notify_trigger_executed(self, execution_info: Dict[str, Any]):
        """Broadcasts an alert when an AI limit trigger is hit and bought."""
        ticker = execution_info.get("ticker")
        trigger_type = execution_info.get("trigger_type", "LIMIT_PULLBACK")
        target_pr = execution_info.get("target_price")
        exec_pr = execution_info.get("executed_price") or execution_info.get("price_native")
        shares = execution_info.get("shares")
        total_eur = execution_info.get("total_eur")
        sl = execution_info.get("stop_loss")
        tp = execution_info.get("take_profit")
        rationale = execution_info.get("ai_rationale", "")

        msg = (
            f"🎯 <b>AI PRICE TRIGGER HIT & EXECUTED!</b>\n\n"
            f"<b>Asset:</b> <code>{html.escape(str(ticker))}</code>\n"
            f"<b>Trigger Type:</b> {trigger_type} (Target: {target_pr})\n"
            f"<b>Execution Price:</b> {exec_pr}\n"
            f"<b>Shares Bought:</b> {shares}\n"
            f"<b>Total Cost:</b> €{total_eur:,.2f}\n"
            f"<b>Stop Loss:</b> {sl} | <b>Take Profit:</b> {tp}\n\n"
            f"🧠 <i>{html.escape(str(rationale))}</i>\n\n"
            f"🛡️ <i>Position is now open in paper portfolio with trailing risk management.</i>"
        )
        await self.send_message(msg)

    async def notify_position_closed(
        self,
        sell_info: Dict[str, Any],
        adjustments: Optional[List[str]] = None,
        lesson: Optional[str] = None
    ) -> bool:
        """Broadcasts an automated exit / stop-loss / take-profit alert with post-mortem & adjustments."""
        ticker = html.escape(str(sell_info.get("ticker", "")))
        reason = html.escape(str(sell_info.get("reason", "Automatic Exit")))
        realized_pnl_eur = sell_info.get("realized_pnl_eur", 0.0) or 0.0
        realized_pnl_pct = sell_info.get("realized_pnl_pct", 0.0) or 0.0
        proceeds_eur = sell_info.get("proceeds_eur", 0.0) or 0.0
        exit_price = sell_info.get("exit_price_native", 0.0) or 0.0
        shares = sell_info.get("shares", 0.0) or 0.0

        sign = "+" if realized_pnl_eur >= 0 else ""
        icon = "🟢" if realized_pnl_eur >= 0 else "🛑"
        title = "TAKE-PROFIT TARGET ACHIEVED" if realized_pnl_eur >= 0 else "STOP-LOSS TRIGGERED"
        if "take-profit" in reason.lower() or "target" in reason.lower():
            title = "TAKE-PROFIT TARGET ACHIEVED"
            icon = "🎯"
        elif "stop-loss" in reason.lower() or "stop" in reason.lower():
            title = "STOP-LOSS TRIGGERED"
            icon = "🛑"

        msg = (
            f"{icon} <b>{title}</b>\n\n"
            f"<b>Asset:</b> <code>{ticker}</code>\n"
            f"<b>Exit Price:</b> {exit_price}\n"
            f"<b>Shares Sold:</b> {shares}\n"
            f"<b>Realized PnL:</b> <b>{sign}€{realized_pnl_eur:.2f} ({sign}{realized_pnl_pct:.2f}%)</b>\n"
            f"<b>Proceeds:</b> €{proceeds_eur:,.2f}\n"
            f"<b>Reason:</b> {reason}\n"
        )
        if lesson:
            msg += f"\n🧠 <b>AI Post-Mortem Lesson:</b>\n<i>{html.escape(lesson)}</i>\n"
        if adjustments:
            adj_text = "\n".join(f"• {html.escape(a)}" for a in adjustments)
            msg += f"\n⚙️ <b>AI Strategy & Risk Adaptation:</b>\n{adj_text}\n"

        return await self.send_message(msg)

    async def notify_market_open(
        self,
        market: str,
        reports: List[Dict[str, Any]],
        executed_trades: List[Dict[str, Any]],
        portfolio: Dict[str, Any],
        comparison: Optional[Dict[str, Any]] = None,
        armed_triggers: Optional[List[Dict[str, Any]]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
        target_chat_id: Optional[Union[int, str]] = None
    ) -> str:
        """Broadcasts full comprehensive Market Open & Daily Investment Advice report."""
        from src.data.review_tracker import review_tracker
        next_info = review_tracker.get_next_session_info()
        flag = "🇪🇺" if market.upper() == "EU" else "🇺🇸"
        market_title = "EUROPEAN" if market.upper() == "EU" else "US"
        now_str = datetime.datetime.now().strftime("%A, %b %d | %H:%M CET")

        cash = portfolio.get("cash_eur", 0.0)
        total_val = portfolio.get("total_value_eur", 10000.0)
        cash_pct = round((cash / total_val) * 100, 1) if total_val > 0 else 100.0

        # Build Current Holdings Section
        holdings_blocks = []
        if positions:
            for p in positions:
                pnl_val = p.get("unrealized_pnl_eur", 0.0) or 0.0
                pnl_pct = p.get("unrealized_pnl_pct", 0.0) or 0.0
                sign = "+" if pnl_val >= 0 else ""
                icon = "🟢" if pnl_val >= 0 else ("🔴" if pnl_val < 0 else "⚪")
                sl = p.get("stop_loss_native")
                tp = p.get("take_profit_native")
                sl_str = f"<code>{sl}</code>" if sl else "<code>None</code>"
                tp_str = f"<code>{tp}</code>" if tp else "<code>None</code>"
                curr_pr = p.get("current_price_native", 0.0)
                entry_pr = p.get("entry_price_native", 0.0)
                shs = p.get("shares", 0)
                curr = p.get("currency", "EUR")
                val_eur = p.get("current_value_eur", 0.0)

                holdings_blocks.append(
                    f"{icon} <b>{p['ticker']}</b> ({shs} shs @ {entry_pr} {curr})\n"
                    f"   • Current: <b>{curr_pr} {curr}</b> | Value: <b>€{val_eur:,.2f}</b>\n"
                    f"   • Unrealized PnL: <b>{sign}€{pnl_val:.2f} ({sign}{pnl_pct:.1f}%)</b>\n"
                    f"   • Trailing Stop: {sl_str} | Target: {tp_str}"
                )
            holdings_text = "\n\n".join(holdings_blocks)
        else:
            holdings_text = "• <i>No open stock positions currently held (100% in cash buffer).</i>"

        # Build Candidate Advice Cards
        candidates_blocks = []
        for idx, r in enumerate(reports[:4], 1):
            ticker = r.get("ticker")
            name = html.escape(str(r.get("name", ticker)))
            score = r.get("consensus_score", 0)
            action = r.get("action", "BUY")
            price = r.get("price", 0)
            curr = html.escape(str(r.get("currency", "USD")))
            
            action_icon = "🟢" if "BUY" in action else ("🔴" if "SELL" in action else "🟡")
            tech = r.get("technical", {})
            fund = r.get("fundamental", {})
            risk = r.get("risk", {})

            sl = html.escape(str(risk.get("stop_loss_price", "N/A")))
            tp = html.escape(str(risk.get("take_profit_price", "N/A")))
            rr = risk.get("risk_reward_ratio", 2.0)
            
            # Check execution / position status of this stock
            status_type = r.get("status_type")
            auto_trade = next((t for t in (executed_trades or []) if t.get("ticker") == ticker), None)
            armed_trig = next((t for t in (armed_triggers or []) if t.get("ticker") == ticker), None)

            if auto_trade or status_type == "BOUGHT":
                trade = auto_trade or {}
                status_str = f"🚀 <b>AUTO-BOUGHT:</b> {trade.get('shares', '')} shs for €{trade.get('total_eur', 0):.2f}"
            elif armed_trig or status_type == "TRIGGER_ARMED":
                trig = armed_trig or {}
                target_pr = trig.get("target_price", r.get("entry_trigger_price", "pullback"))
                status_str = f"🎯 <b>TRIGGER ARMED:</b> Limit pullback order at <code>{target_pr} {curr}</code>"
            elif status_type == "ALREADY_HELD":
                status_str = f"💼 <b>CURRENT HOLDING:</b> Position already active in portfolio"
            elif status_type == "TRIGGER_PENDING":
                status_str = f"⏳ <b>TRIGGER PENDING:</b> Limit trigger waiting for entry pullback"
            elif status_type == "INSUFFICIENT_CASH":
                status_str = f"⏸️ <b>ON RADAR:</b> High conviction ({score}/100), awaiting available cash"
            elif status_type == "SHARE_TOO_EXPENSIVE":
                status_str = f"💶 <b>ON RADAR:</b> High conviction ({score}/100), but one share costs more than the position budget"
            elif status_type == "REGIME_FILTER":
                status_str = f"🌧️ <b>ON RADAR:</b> High conviction ({score}/100), but the market index is below its 200-day average"
            else:
                status_str = f"👀 <b>STATUS:</b> On Radar (Conviction: {score}/100)"

            tech_summary = html.escape(str(tech.get("analysis_summary", "")))
            if len(tech_summary) > 130:
                tech_summary = tech_summary[:127] + "..."

            card = (
                f"{idx}. {action_icon} <b>{ticker}</b> — {name}\n"
                f"   • <b>Verdict:</b> {action.replace('_', ' ')} (Conviction: <b>{score}/100</b>)\n"
                f"   • <b>Price:</b> {price} {curr}\n"
                f"   • <b>Technical:</b> {tech.get('bias', 'NEUTRAL')} ({tech_summary})\n"
                f"   • <b>Fundamental:</b> {fund.get('valuation', 'FAIR')} valuation ({fund.get('health', 'MODERATE')} health)\n"
                f"   • <b>Risk Parameters:</b>\n"
                f"     ↳ Stop-Loss: <code>{sl}</code>\n"
                f"     ↳ Take-Profit: <code>{tp}</code> (R:R {rr}:1)\n"
                f"   • {status_str}"
            )
            candidates_blocks.append(card)

        candidates_text = "\n\n".join(candidates_blocks) if candidates_blocks else "• <i>No high-momentum setups passed filtering this session.</i>"

        comparison_section = ""
        if comparison and comparison.get("has_history"):
            comparison_section = f"\n\n{comparison['text_summary']}"

        text = (
            f"🔔 {flag} <b>{market_title} MARKET OPEN & DAILY ADVICE</b>\n"
            f"📅 <code>{now_str}</code>\n\n"
            f"💼 <b>PORTFOLIO STATUS:</b>\n"
            f"• Total Equity: <b>€{total_val:,.2f}</b>\n"
            f"• Available Cash: €{cash:,.2f} ({cash_pct}%)\n"
            f"• Open Holdings: {len(positions) if positions is not None else portfolio.get('open_positions_count', 0)} / {settings.MAX_POSITIONS} max\n\n"
            f"📊 <b>CURRENT STOCK HOLDINGS:</b>\n"
            f"{holdings_text}\n\n"
            f"🎯 <b>TOP COMMITTEE STOCK RECOMMENDATIONS:</b>\n\n"
            f"{candidates_text}"
            f"{comparison_section}\n\n"
            f"🛡️ <i>Risk guardrails & trailing stops active. Next update: <b>{next_info['formatted']}</b>.</i>"
        )
        await self.send_message(text, chat_id=target_chat_id)
        if self.chat_id and target_chat_id and str(self.chat_id) != str(target_chat_id):
            await self.send_message(text, chat_id=self.chat_id)
        return text

    async def notify_market_close(
        self,
        market: str,
        positions: List[Dict[str, Any]],
        closed_trades_today: List[Dict[str, Any]],
        portfolio: Dict[str, Any],
        comparison: Optional[Dict[str, Any]] = None,
        target_chat_id: Optional[Union[int, str]] = None
    ) -> str:
        """Broadcasts full comprehensive Market Close & Portfolio Performance wrap-up."""
        from src.data.review_tracker import review_tracker
        next_info = review_tracker.get_next_session_info()
        flag = "🇪🇺" if market.upper() == "EU" else "🇺🇸"
        market_title = "EUROPEAN" if market.upper() == "EU" else "US"
        now_str = datetime.datetime.now().strftime("%A, %b %d | %H:%M CET")

        total_val = portfolio.get("total_value_eur", 10000.0)
        cash = portfolio.get("cash_eur", 10000.0)
        equity = portfolio.get("equity_eur", 0.0)
        unrealized = portfolio.get("unrealized_pnl_eur", 0.0)
        total_ret = portfolio.get("total_return_pct", 0.0)

        ret_sign = "+" if total_ret >= 0 else ""
        unrealized_sign = "+" if unrealized >= 0 else ""

        # Open positions summary
        pos_lines = []
        for p in positions:
            pnl_val = p.get("unrealized_pnl_eur", 0.0) or 0.0
            pnl_pct = p.get("unrealized_pnl_pct", 0.0) or 0.0
            sign = "+" if pnl_val >= 0 else ""
            icon = "🟢" if pnl_val >= 0 else "🔴"
            p_ticker = html.escape(str(p.get("ticker", "")))
            curr = html.escape(str(p.get("currency", "EUR")))

            pos_lines.append(
                f"{icon} <b>{p_ticker}</b> ({p.get('shares')} shares)\n"
                f"   • Entry: {p.get('entry_price_native')} ➔ Current: <b>{p.get('current_price_native')} {curr}</b>\n"
                f"   • Value: €{p.get('current_value_eur', 0):.2f} (PnL: <b>{sign}€{pnl_val:.2f} / {sign}{pnl_pct}%</b>)\n"
                f"   • Stop: <code>{html.escape(str(p.get('stop_loss_native', 'None')))}</code> | Target: <code>{html.escape(str(p.get('take_profit_native', 'None')))}</code>"
            )

        pos_text = "\n\n".join(pos_lines) if pos_lines else "• <i>No open positions currently held overnight.</i>"

        # Closed trades today
        closed_lines = []
        for t in closed_trades_today:
            pnl = t.get("realized_pnl_eur", 0.0) or 0.0
            pnl_pct = t.get("realized_pnl_pct", 0.0) or 0.0
            sign = "+" if pnl >= 0 else ""
            icon = "💰" if pnl >= 0 else "⚠️"
            t_ticker = html.escape(str(t.get("ticker", "")))
            t_order = html.escape(str(t.get("order_type", "SELL")))
            t_rat = html.escape(str(t.get("rationale", "")))
            closed_lines.append(
                f"{icon} <b>{t_order} {t_ticker}</b>: PnL {sign}€{pnl:.2f} ({sign}{pnl_pct}%) — <i>{t_rat}</i>"
            )
        closed_text = "\n".join(closed_lines) if closed_lines else "• <i>No positions exited during this session.</i>"

        comparison_section = ""
        if comparison and comparison.get("has_history"):
            comparison_section = f"\n\n{comparison['text_summary']}"

        text = (
            f"📊 {flag} <b>{market_title} MARKET CLOSE & PORTFOLIO WRAP-UP</b>\n"
            f"📅 <code>{now_str}</code>\n\n"
            f"📈 <b>PORTFOLIO CLOSE VALUATION:</b>\n"
            f"• Total Portfolio Value: <b>€{total_val:,.2f}</b> ({ret_sign}{total_ret}% total)\n"
            f"• Unrealized PnL: <b>{unrealized_sign}€{unrealized:.2f}</b>\n"
            f"• Cash Balance: €{cash:,.2f}\n"
            f"• Invested Equity: €{equity:,.2f}\n\n"
            f"💼 <b>OVERNIGHT HOLDINGS ({len(positions)}):</b>\n\n"
            f"{pos_text}\n\n"
            f"📜 <b>TRADES CLOSED TODAY:</b>\n"
            f"{closed_text}"
            f"{comparison_section}\n\n"
            f"🌐 <b>NEXT UP:</b> {next_info['formatted']}."
        )
        await self.send_message(text, chat_id=target_chat_id)
        if self.chat_id and target_chat_id and str(self.chat_id) != str(target_chat_id):
            await self.send_message(text, chat_id=self.chat_id)
        return text

    async def register_bot_commands(self) -> bool:
        """Registers bot commands with Telegram via setMyCommands for native autocompletion across scopes."""
        if not self.is_configured():
            return False

        commands = [
            {"command": "portfolio", "description": "Check cash, equity, and open positions"},
            {"command": "buy", "description": "Execute paper buy (e.g. /buy NVDA 500)"},
            {"command": "sell", "description": "Close an open stock position (e.g. /sell NVDA)"},
            {"command": "review", "description": "Instant Market Review & Advice (e.g. /review us)"},
            {"command": "compare", "description": "Past-week reviews comparison & performance"},
            {"command": "triggers", "description": "View active AI limit price triggers"},
            {"command": "trigger", "description": "Arm AI limit buy trigger (e.g. /trigger NVDA)"},
            {"command": "cancel_trigger", "description": "Cancel a pending trigger by ID or ticker"},
            {"command": "analyze", "description": "Multi-agent committee analysis (e.g. /analyze NVDA)"},
            {"command": "learning", "description": "AI trade post-mortems, win/loss stats & lessons"},
            {"command": "calibrate", "description": "Autonomously re-tune risk parameters"},
            {"command": "watchlist", "description": "List or manage radar stocks"},
            {"command": "trades", "description": "View recent closed trades history"},
            {"command": "status", "description": "System health and scheduler status"},
            {"command": "stop", "description": "Pause automated buying"},
            {"command": "resume", "description": "Enable automated buying"},
            {"command": "help", "description": "Show all available bot commands"},
        ]
        set_url = f"{self.base_url}/setMyCommands"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Register for default scope
                await client.post(set_url, json={"commands": commands})

                # Register specifically for all_private_chats scope
                resp = await client.post(set_url, json={"commands": commands, "scope": {"type": "all_private_chats"}})
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Successfully registered Telegram bot commands for private chats and default scope.")
                    return True
                else:
                    logger.warning(f"Failed to register Telegram commands: {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Error registering Telegram commands: {e}")
            return False

    async def start_polling(self):
        """Starts background long-polling for two-way commands."""
        if not self.is_configured():
            logger.info("Telegram Bot token not provided. Bot polling dormant.")
            return

        self._is_running = True
        self._polling_task = asyncio.current_task()
        logger.info("Telegram Assistant bot polling started.")
        await self.register_bot_commands()
        
        async with httpx.AsyncClient(timeout=35.0) as client:
            # Delete any prior webhook to eliminate 409 Conflict errors
            try:
                await client.post(f"{self.base_url}/deleteWebhook", json={"drop_pending_updates": False})
            except Exception as w_err:
                logger.debug(f"Notice deleting existing webhook: {w_err}")

            try:
                while self._is_running:
                    try:
                        url = f"{self.base_url}/getUpdates"
                        params = {"offset": self._last_update_id + 1, "timeout": 25}
                        resp = await client.get(url, params=params)
                        if resp.status_code == 200:
                            data = resp.json()
                            updates = data.get("result", [])
                            for update in updates:
                                self._last_update_id = update["update_id"]
                                await self._handle_update(update)
                        else:
                            logger.warning(f"Telegram polling returned {resp.status_code}: {resp.text}")
                            if resp.status_code == 409:
                                await client.post(f"{self.base_url}/deleteWebhook", json={"drop_pending_updates": False})
                            await asyncio.sleep(4)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.debug(f"Telegram polling tick notice: {e}")
                        await asyncio.sleep(4)
            finally:
                self._is_running = False
                self._polling_task = None

    async def stop(self):
        self._is_running = False
        if self._polling_task:
            self._polling_task.cancel()

    def _plan_trigger(
        self, data: Dict[str, Any], report: Dict[str, Any], available_cash: float, explicit_price: Optional[float] = None,
        summary: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        The levels and euro allocation an armed trigger would use, without arming anything, so the trigger gate
        can judge them first. An explicit price decides the trigger type (below the market: pullback, else breakout).
        """
        cur_pr = data["price"]
        levels_report = {**report, "risk": report.get("risk") or {}}
        if explicit_price:
            levels_report["entry_trigger_price"] = explicit_price
            levels_report["trigger_type"] = "LIMIT_PULLBACK" if explicit_price < cur_pr else "BREAKOUT_BUY"
        trigger_type, trigger_price, sl, tp = derive_trigger_levels(levels_report, cur_pr)

        # The slot the dashboard shows once the trigger is pending (needs the portfolio summary for reserved cash and open slots)
        summary = summary or {"usable_capital_eur": available_cash, "reserved_triggers_eur": 0.0, "open_positions_count": 0}
        alloc = entry_allocation_from_summary(summary, data.get("ticker"), data.get("currency"))
        return {"target_price": trigger_price, "trigger_type": trigger_type, "stop_loss": sl, "take_profit": tp, "alloc_eur": alloc}

    async def _arm_trigger_from_report(
        self, ticker: str, data: Dict[str, Any], report: Dict[str, Any], available_cash: float,
        explicit_price: Optional[float] = None, plan: Optional[Dict[str, Any]] = None,
        summary: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Arms the trigger for a committee report. Pass the plan from _plan_trigger when the gate already used it."""
        plan = plan or self._plan_trigger(data, report, available_cash, explicit_price, summary=summary)

        res = await paper_broker.create_price_trigger(
            ticker=ticker,
            name=data.get("name", ticker),
            currency=data.get("currency", "USD"),
            trigger_type=plan["trigger_type"],
            target_price=plan["target_price"],
            current_price_at_creation=data["price"],
            stop_loss=plan["stop_loss"],
            take_profit=plan["take_profit"],
            allocation_eur=plan["alloc_eur"],
            consensus_score=report.get("consensus_score", 70),
            ai_rationale=report.get("trigger_rationale", report.get("summary", ""))
        )
        res.update(plan)
        # Show the allocation the broker actually stored, not the planned one
        res["alloc_eur"] = res.get("allocation_eur", plan["alloc_eur"])
        return res

    async def _handle_update(self, update: Dict[str, Any]):
        callback_query = update.get("callback_query")
        if callback_query:
            msg = callback_query.get("message")
            raw_text = callback_query.get("data", "")
        else:
            # Edited messages are deliberately ignored so editing "/buy NVDA 500" cannot re-trigger an order.
            msg = update.get("message")
            raw_text = msg.get("text", "") if msg else ""

        if not msg or not raw_text:
            return

        text = raw_text.strip()
        chat = msg.get("chat")
        if not chat or "id" not in chat:
            return
        chat_id = chat["id"]

        # Authorisation: only the configured owner chat may issue commands.
        configured_chat = str(settings.TELEGRAM_CHAT_ID or "").strip()
        if not configured_chat:
            logger.warning(
                f"Ignoring Telegram update from chat id {chat_id}: TELEGRAM_CHAT_ID is not configured."
            )
            return
        if str(chat_id) != configured_chat:
            return

        # Drop stale/replayed messages (e.g. queued while the bot was down).
        msg_date = msg.get("date")
        if not callback_query and isinstance(msg_date, (int, float)) and time.time() - msg_date > 120:
            logger.info(f"Ignoring stale Telegram message (age {int(time.time() - msg_date)}s).")
            return

        parts = text.split()
        if not parts:
            return
        raw_cmd = parts[0].lower().split('@')[0]
        # Commands must start with "/" so plain words like "stop" or "halt" never execute anything.
        if not raw_cmd.startswith('/'):
            return
        command = raw_cmd.lstrip('/')

        if command in ("start", "help", "", "commands", "menu", "hi", "hello"):
            reply = (
                "👋 <b>Welcome to your Multi-Agent Investment Bot!</b>\n\n"
                "Available commands:\n"
                "📊 <code>/portfolio</code> - Check cash, equity, and open positions\n"
                "🟢 <code>/buy TICKER [AMOUNT]</code> - Manually execute a buy order (e.g. <code>/buy NVDA 500</code>)\n"
                "🔴 <code>/sell TICKER</code> - Close an open stock position (e.g. <code>/sell NVDA</code>)\n"
                "🔔 <code>/review [eu|us] [open|close]</code> - Instant Market Review & Daily Advice\n"
                "📜 <code>/compare</code> - Past-week reviews comparison & pick follow-through\n"
                "🎯 <code>/triggers</code> - View active AI limit price triggers\n"
                "⚡ <code>/trigger TICKER [PRICE] [force]</code> - AI calculates entry level (or specify custom level) and arms limit buy trigger. Needs a committee buy verdict at or above the trigger score; <code>force</code> overrides a HOLD verdict or low score, never a SELL, Risk Officer veto, falling market or too-expensive share\n"
                "❌ <code>/cancel_trigger ID_OR_TICKER</code> - Cancel a pending trigger (e.g. <code>/cancel 1</code> or <code>/cancel NVDA</code>)\n"
                "🔍 <code>/analyze TICKER</code> - Instant multi-agent committee debate (e.g. <code>/analyze NVDA</code>)\n"
                "📋 <code>/watchlist [add|del TICKER]</code> - View or manage radar stocks\n"
                "📜 <code>/trades</code> - View recent closed trades history\n"
                "🧠 <code>/learning</code> - AI trade post-mortems, wins/losses & lessons\n"
                "⚙️ <code>/calibrate</code> - Autonomously re-tune risk parameters\n"
                "⚡ <code>/status</code> - Live system health, market hours, and auto-buy status\n"
                "🛑 <code>/stop</code> - Pause automated buying\n"
                "▶️ <code>/resume</code> - Enable automated buying\n"
                "🚨 <code>/killswitch</code> - Emergency circuit breaker to halt all trading\n"
                "❓ <code>/help</code> - Show this menu of available commands"
            )
            await self._reply_chat(chat_id, reply)

        elif command in ("killswitch", "emergency_stop", "halt"):
            from src.db.session import set_kill_switch
            await set_kill_switch(not settings.KILL_SWITCH_ACTIVE)
            status_text = (
                "🚨 <b>EMERGENCY KILL SWITCH ACTIVATED!</b>\nAll order triggers & new buys are immediately blocked."
                if settings.KILL_SWITCH_ACTIVE
                else "✅ <b>EMERGENCY KILL SWITCH DEACTIVATED.</b>\nTrading operations resumed."
            )
            await self._reply_chat(chat_id, status_text)

        elif command in ("portfolio", "positions", "balance", "holdings", "p"):
            try:
                positions = await paper_broker.get_positions()
                summary = await paper_broker.get_portfolio_summary()
                
                pos_lines = []
                for p in positions:
                    unrealized_pnl = p.get("unrealized_pnl_eur", 0.0) or 0.0
                    unrealized_pct = p.get("unrealized_pnl_pct", 0.0) or 0.0
                    pnl_sign = "+" if unrealized_pnl >= 0 else ""
                    pos_lines.append(
                        f"• <b>{p['ticker']}</b>: {p['shares']} shs @ {p['current_price_native']} {p['currency']} "
                        f"({pnl_sign}€{unrealized_pnl:.2f} / {pnl_sign}{unrealized_pct:.2f}%)"
                    )
                pos_text = "\n".join(pos_lines) if pos_lines else "No open positions currently."

                unrealized_pnl = summary.get('unrealized_pnl_eur', 0.0) or 0.0
                total_return = summary.get('total_return_pct', 0.0) or 0.0
                pnl_sign = "+" if unrealized_pnl >= 0 else ""
                ret_sign = "+" if total_return >= 0 else ""
                reply = (
                    f"💼 <b>PORTFOLIO SUMMARY</b>\n\n"
                    f"• Total Value: <b>€{summary.get('total_value_eur', 0.0):,.2f}</b>\n"
                    f"• Cash Balance: €{summary.get('cash_eur', 0.0):,.2f}\n"
                    f"• Equity in Stocks: €{summary.get('equity_eur', 0.0):,.2f}\n"
                    f"• Unrealized PnL: {pnl_sign}€{unrealized_pnl:,.2f} ({ret_sign}{total_return}%)\n\n"
                    f"<b>Open Holdings ({summary.get('open_positions_count', len(positions))}):</b>\n"
                    f"{pos_text}"
                )
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /portfolio: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error retrieving portfolio: {html.escape(str(e))}")

        elif command in ("buy", "order_buy"):
            try:
                if len(parts) < 2:
                    await self._reply_chat(chat_id, "⚠️ Usage: <code>/buy TICKER [AMOUNT_EUR]</code> (e.g. <code>/buy NVDA</code> or <code>/buy ASML.AS 500</code>)")
                    return

                target_ticker = parts[1].upper().lstrip("$")
                explicit_amount = None
                if len(parts) >= 3:
                    try:
                        explicit_amount = float(parts[2].replace(",", ".").lstrip("$€£"))
                    except ValueError:
                        explicit_amount = None
                    if explicit_amount is None or not math.isfinite(explicit_amount) or explicit_amount <= 0:
                        await self._reply_chat(chat_id, "⚠️ Invalid amount. Usage: <code>/buy TICKER [AMOUNT_EUR]</code> (amount must be a number greater than 0, e.g. <code>/buy ASML.AS 500</code>)")
                        return

                from src.trading.risk import check_market_hours
                is_open, open_msg = check_market_hours(target_ticker)
                if not is_open and settings.ENFORCE_MARKET_HOURS:
                    await self._reply_chat(
                        chat_id,
                        f"🛑 <b>Market Closed:</b> {open_msg}\n\n"
                        f"💡 <i>Tip: You can arm an AI limit trigger using <code>/trigger {target_ticker}</code> "
                        f"so it automatically buys when the market opens!</i>"
                    )
                    return

                await self._reply_chat(chat_id, f"⏳ Convening committee & calculating risk allocation for <b>{target_ticker}</b>...")

                data = await asyncio.to_thread(market_data_provider.get_stock_data, target_ticker)
                if not data:
                    await self._reply_chat(chat_id, f"❌ Could not fetch market data for <b>{target_ticker}</b>. Please check ticker symbol.")
                    return

                summary = await paper_broker.get_portfolio_summary()
                available_cash = summary.get("available_cash_eur", 0.0)
                if available_cash < 20.0:
                    await self._reply_chat(chat_id, f"⚠️ Insufficient available cash (€{available_cash:.2f}) to execute buy order.")
                    return

                report = await investment_committee.evaluate_ticker(data, summary.get("total_value_eur", 10000.0))
                risk_cfg = report.get("risk", {})
                alloc_eur = entry_allocation_from_summary(summary, target_ticker, data.get("currency"), requested_alloc=explicit_amount)

                sl = risk_cfg.get("stop_loss_price")
                tp = risk_cfg.get("take_profit_price")

                buy_res = await paper_broker.execute_buy(
                    ticker=target_ticker,
                    name=data.get("name", target_ticker),
                    price_native=data["price"],
                    currency=data.get("currency", "USD"),
                    stop_loss_native=sl,
                    take_profit_native=tp,
                    allocation_eur=alloc_eur,
                    consensus_score=report.get("consensus_score", 75),
                    rationale=f"Manual Buy via Telegram ({report.get('action', 'BUY')})",
                    is_manual=True
                )

                if buy_res.get("success"):
                    sl_str = f"{buy_res.get('stop_loss')}" if buy_res.get('stop_loss') else "None"
                    tp_str = f"{buy_res.get('take_profit')}" if buy_res.get('take_profit') else "None"
                    reply = (
                        f"🚀 <b>MANUAL BUY ORDER EXECUTED</b>\n\n"
                        f"<b>Asset:</b> <code>{target_ticker}</code> ({html.escape(data.get('name', target_ticker))})\n"
                        f"<b>Position:</b> {buy_res.get('shares')} shares\n"
                        f"<b>Execution Price:</b> {buy_res.get('price_native')} {data.get('currency', 'USD')} (≈ €{buy_res.get('price_eur', 0.0):.2f})\n"
                        f"<b>Total Allocation:</b> €{buy_res.get('total_eur', 0.0):,.2f}\n"
                        f"<b>Stop Loss:</b> <code>{sl_str}</code>\n"
                        f"<b>Take Profit:</b> <code>{tp_str}</code>\n\n"
                        f"🛡️ <i>Position is now open with automated trailing risk management.</i>"
                    )
                else:
                    reply = f"❌ <b>Buy Order Failed:</b> {html.escape(buy_res.get('message', 'Unknown error'))}"
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /buy: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error executing buy: {html.escape(str(e))}")

        elif command in ("sell", "close", "exit"):
            try:
                if len(parts) < 2:
                    await self._reply_chat(chat_id, "⚠️ Usage: <code>/sell TICKER [REASON]</code> (e.g. <code>/sell NVDA</code> or <code>/close ASML.AS Take Profit</code>)")
                    return

                target_ticker = parts[1].upper().lstrip("$")
                reason = " ".join(parts[2:]) if len(parts) >= 3 else "Manual Close via Telegram"

                from src.trading.risk import check_market_hours
                is_open, open_msg = check_market_hours(target_ticker)
                if not is_open and settings.ENFORCE_MARKET_HOURS:
                    await self._reply_chat(chat_id, f"🛑 <b>Market Closed:</b> {open_msg}")
                    return

                sell_res = await paper_broker.execute_sell(target_ticker, reason=reason)
                if sell_res.get("success"):
                    pnl_eur = sell_res.get("realized_pnl_eur", 0.0) or 0.0
                    pnl_pct = sell_res.get("realized_pnl_pct", 0.0) or 0.0
                    pnl_sign = "+" if pnl_eur >= 0 else ""
                    icon = "🟢" if pnl_eur >= 0 else "🔴"
                    reply = (
                        f"{icon} <b>POSITION CLOSED</b>\n\n"
                        f"<b>Asset:</b> <code>{target_ticker}</code>\n"
                        f"<b>Exit Price:</b> {sell_res.get('exit_price_native')}\n"
                        f"<b>Shares Sold:</b> {sell_res.get('shares')}\n"
                        f"<b>Total Proceeds:</b> €{sell_res.get('proceeds_eur', 0.0):,.2f}\n"
                        f"<b>Realized PnL:</b> <b>{pnl_sign}€{pnl_eur:.2f} ({pnl_sign}{pnl_pct:.2f}%)</b>\n"
                        f"<b>Reason:</b> {html.escape(reason)}"
                    )
                else:
                    reply = f"❌ <b>Sell Failed:</b> {html.escape(sell_res.get('message', 'Position not found or error closing.'))}"
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /sell: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error executing sell: {html.escape(str(e))}")

        elif command in ("trades", "history"):
            try:
                from src.db.session import AsyncSessionLocal
                from src.db.models import Trade
                from sqlalchemy import select, desc
                async with AsyncSessionLocal() as session:
                    res = await session.execute(
                        select(Trade).order_by(desc(Trade.executed_at)).limit(8)
                    )
                    trades = res.scalars().all()
                if not trades:
                    await self._reply_chat(chat_id, "📜 <b>TRADE HISTORY</b>\n\nNo trades recorded in database yet.")
                else:
                    lines = []
                    for t in trades:
                        icon = "🟢 BUY" if t.order_type == "BUY" else "🔴 SELL"
                        pnl_str = ""
                        if t.order_type == "SELL" and t.realized_pnl_eur is not None:
                            sign = "+" if t.realized_pnl_eur >= 0 else ""
                            pnl_str = f" | PnL: <b>{sign}€{t.realized_pnl_eur:.2f} ({sign}{t.realized_pnl_pct or 0.0:+.1f}%)</b>"
                        dt_str = t.executed_at.strftime("%b %d %H:%M") if t.executed_at else ""
                        lines.append(
                            f"• {icon} <b>{t.ticker}</b> ({t.shares} shs @ {t.price_native} {t.currency})\n"
                            f"  ↳ Cost/Proceeds: €{t.total_eur:,.2f}{pnl_str} [<code>{dt_str}</code>]"
                        )
                    reply = f"📜 <b>RECENT TRADES ({len(trades)}):</b>\n\n" + "\n\n".join(lines)
                    await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /trades: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error retrieving trades: {html.escape(str(e))}")

        elif command in ("triggers", "orders"):
            try:
                triggers = await paper_broker.get_triggers()
                pending = [t for t in triggers if t.get("status") == "PENDING"]
                if not pending:
                    reply = (
                        "🎯 <b>NO ACTIVE AI TRIGGERS</b>\n\n"
                        "There are currently no active limit price triggers.\n"
                        "Use <code>/trigger TICKER</code> (e.g. <code>/trigger NVDA</code>) to have the AI set one!"
                    )
                else:
                    lines = []
                    for t in pending:
                        type_icon = "📉" if t.get("trigger_type") == "LIMIT_PULLBACK" else "📈"
                        dist = t.get("distance_pct", 0.0) or 0.0
                        dist_sign = "+" if dist > 0 else ""
                        rationale = t.get("ai_rationale") or ""
                        rationale_str = f"\n  • <i>{html.escape(rationale[:100])}...</i>" if rationale else ""
                        planned = f" (≈ {t['planned_shares']:g} sh)" if t.get("planned_shares") else ""
                        lines.append(
                            f"{type_icon} <b>#{t['id']} {t['ticker']}</b> ({t.get('trigger_type', 'LIMIT')})\n"
                            f"  • Target: <b>{t.get('target_price')} {t.get('currency')}</b> (Now: {t.get('current_price')} | {dist_sign}{dist:.1f}%)\n"
                            f"  • Alloc: €{t.get('allocation_eur', 0.0):,.2f}{planned} | SL: {t.get('stop_loss')} | TP: {t.get('take_profit')}"
                            f"{rationale_str}"
                        )
                    reply = f"🎯 <b>ACTIVE AI PRICE TRIGGERS ({len(pending)}):</b>\n\n" + "\n\n".join(lines)
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /triggers: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error retrieving triggers: {html.escape(str(e))}")

        elif command in ("trigger", "buy_trigger", "limit"):
            try:
                usage = "<code>/trigger TICKER [TARGET_PRICE] [force]</code>"
                if len(parts) < 2 or parts[1].lower() == "force":
                    await self._reply_chat(chat_id, f"⚠️ Usage: {usage} (e.g. <code>/trigger NVDA</code> or <code>/trigger ASML.AS 750</code>)")
                    return

                # "force" may be the 3rd or 4th word: /trigger NVDA force, /trigger NVDA 750 force
                force = any(p.lower() == "force" for p in parts[2:4])
                price_args = [p for p in parts[2:4] if p.lower() != "force"]

                target_ticker = normalize_ticker(parts[1])
                if not target_ticker:
                    await self._reply_chat(chat_id, f"⚠️ {html.escape(invalid_ticker_result(parts[1]).message)} Usage: {usage}")
                    return

                explicit_price = None
                if price_args:
                    try:
                        explicit_price = float(price_args[0].replace(",", ".").lstrip("$€£"))
                    except ValueError:
                        explicit_price = None
                    if explicit_price is None or not math.isfinite(explicit_price) or explicit_price <= 0:
                        await self._reply_chat(chat_id, f"⚠️ Invalid price. Usage: {usage} (price must be a number greater than 0, e.g. <code>/trigger ASML.AS 750</code>)")
                        return

                await self._reply_chat(chat_id, f"⏳ Analyzing <b>{target_ticker}</b> to determine optimal AI entry trigger level...")

                data = await asyncio.to_thread(market_data_provider.get_stock_data, target_ticker)
                summary = await paper_broker.get_portfolio_summary() if data else {}
                available_cash = summary.get("available_cash_eur", 0.0)

                # Cheap checks first, so a bad ticker or an empty wallet never costs a committee run
                blocked = await precheck_manual_trigger(target_ticker, data, available_cash)
                if blocked:
                    if blocked.code == "NO_MARKET_DATA":
                        await self._reply_chat(chat_id, f"❌ Could not fetch market data for {target_ticker}. Please check ticker symbol.")
                    else:
                        await self._reply_chat(chat_id, f"⚠️ Cannot arm trigger: {html.escape(blocked.message)}")
                    return

                report = await investment_committee.evaluate_ticker(data, summary.get("total_value_eur", 10000.0))
                plan = self._plan_trigger(data, report, available_cash, explicit_price, summary=summary)
                gate = await evaluate_manual_trigger(
                    target_ticker, data, report, plan["target_price"], plan["alloc_eur"], await get_trigger_min_score(), force=force,
                    stop_loss=plan["stop_loss"], portfolio_value_eur=summary.get("total_value_eur", 0.0)
                )
                if not gate.allowed:
                    reply = f"🛑 <b>TRIGGER NOT ARMED:</b> {target_ticker}\n\n{html.escape(gate.message)}"
                    if gate.overridable:
                        price_part = f" {explicit_price}" if explicit_price else ""
                        reply += (
                            f"\n\n💡 <i>To arm it anyway, against the committee's verdict, resend "
                            f"<code>/trigger {target_ticker}{price_part} force</code>.</i>"
                        )
                    await self._reply_chat(chat_id, reply)
                    return

                trig_res = await self._arm_trigger_from_report(target_ticker, data, report, available_cash, explicit_price, plan=plan)

                if trig_res.get("success"):
                    dist_pct = round(((trig_res["target_price"] / data["price"]) - 1.0) * 100, 1)
                    reply = (
                        f"🎯 <b>AI PRICE TRIGGER ARMED #{trig_res['trigger_id']}</b>\n\n"
                        f"<b>Asset:</b> {target_ticker} ({html.escape(data.get('name', target_ticker))})\n"
                        f"<b>Trigger Type:</b> {trig_res['trigger_type']}\n"
                        f"<b>AI Entry Price:</b> <b>{trig_res['target_price']} {data.get('currency', 'USD')}</b> ({dist_pct:+.1f}% from current {data['price']})\n"
                        f"<b>Planned Allocation:</b> €{trig_res['alloc_eur']:,.2f}\n"
                        f"<b>Stop Loss:</b> {trig_res['stop_loss']} | <b>Take Profit:</b> {trig_res['take_profit']}\n\n"
                        f"🧠 <b>AI Rationale:</b>\n{html.escape(report.get('trigger_rationale', report.get('summary', '')))}\n\n"
                        f"⚡ <i>When price hits {trig_res['target_price']} during open market hours, it will automatically buy and notify you.</i>"
                    )
                    if gate.code == "OK_FORCED":
                        reply += (
                            f"\n\n⚠️ <i>Forced: armed against the committee verdict ({html.escape(gate.action or 'no verdict')}, "
                            f"score {gate.consensus_score}/100, trigger threshold {gate.trigger_min_score}).</i>"
                        )
                else:
                    reply = f"⚠️ {html.escape(str(trig_res.get('message')))}"
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /trigger: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error arming trigger: {html.escape(str(e))}")

        elif command in ("cancel_trigger", "del_trigger", "cancel", "delete_trigger"):
            try:
                if len(parts) < 2:
                    await self._reply_chat(chat_id, "⚠️ Usage: <code>/cancel_trigger ID_OR_TICKER</code> (e.g. <code>/cancel 1</code> or <code>/cancel NVDA</code>)")
                    return
                clean_arg = parts[1].lstrip("#$").upper()
                if clean_arg.isdigit():
                    trigger_id = int(clean_arg)
                    res = await paper_broker.cancel_price_trigger(trigger_id)
                    if res.get("success"):
                        reply = f"✅ {html.escape(res.get('message', 'Trigger cancelled.'))}"
                    else:
                        reply = f"❌ {html.escape(res.get('message', 'Failed to cancel trigger.'))}"
                    await self._reply_chat(chat_id, reply)
                else:
                    # Cancel by ticker symbol
                    triggers = await paper_broker.get_triggers()
                    matched = [t for t in triggers if t.get("status") == "PENDING" and t.get("ticker", "").upper() == clean_arg]
                    if not matched:
                        await self._reply_chat(chat_id, f"⚠️ No active pending trigger found for <b>{clean_arg}</b>.")
                        return
                    cancelled_ids = []
                    for t in matched:
                        c_res = await paper_broker.cancel_price_trigger(t["id"])
                        if c_res.get("success"):
                            cancelled_ids.append(str(t["id"]))
                    if cancelled_ids:
                        ids_str = ", #".join(cancelled_ids)
                        reply = f"✅ Cancelled pending trigger(s) #{ids_str} for <b>{clean_arg}</b>."
                    else:
                        reply = f"❌ Failed to cancel trigger(s) for <b>{clean_arg}</b>."
                    await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /cancel_trigger: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error cancelling trigger: {html.escape(str(e))}")

        elif command in ("analyze", "eval", "opinion"):
            try:
                if len(parts) < 2:
                    await self._reply_chat(chat_id, "⚠️ Usage: <code>/analyze TICKER</code> (e.g. <code>/analyze NVDA</code>)")
                    return
                
                target_ticker = parts[1].upper().lstrip("$")
                await self._reply_chat(chat_id, f"⏳ Convening Multi-Agent Investment Committee for <b>{target_ticker}</b>...")
                
                data = await asyncio.to_thread(market_data_provider.get_stock_data, target_ticker)
                if not data:
                    await self._reply_chat(chat_id, f"❌ Could not fetch market data for {target_ticker}. Please check ticker symbol.")
                    return

                summary = await paper_broker.get_portfolio_summary()
                report = await investment_committee.evaluate_ticker(data, summary.get("total_value_eur", 10000.0))
                action_icon = "🟢" if "BUY" in report.get('action', '') else ("🔴" if "SELL" in report.get('action', '') else "🟡")
                score = report.get("consensus_score", 50)

                # Auto-arm only what the trigger gate allows: a buy verdict the Risk Officer approved, at or above the threshold
                auto_armed_note = ""
                trigger_min_score = await get_trigger_min_score()
                verdict = evaluate_trigger_verdict(report, trigger_min_score)
                if verdict.allowed:
                    available_cash = summary.get("available_cash_eur", 0.0)
                    if normalize_ticker(target_ticker):
                        blocked = await precheck_manual_trigger(target_ticker, data, available_cash)
                    else:
                        blocked = invalid_ticker_result(target_ticker)
                    plan = None
                    if not blocked:
                        plan = self._plan_trigger(data, report, available_cash, summary=summary)
                        gate = await evaluate_manual_trigger(
                            target_ticker, data, report, plan["target_price"], plan["alloc_eur"], trigger_min_score,
                            stop_loss=plan["stop_loss"], portfolio_value_eur=summary.get("total_value_eur", 0.0)
                        )
                        blocked = None if gate.allowed else gate

                    if blocked is None:
                        trig_res = await self._arm_trigger_from_report(target_ticker, data, report, available_cash, plan=plan)
                        if trig_res.get("success"):
                            dist_pct = round(((trig_res["target_price"] / data["price"]) - 1.0) * 100, 1)
                            auto_armed_note = (
                                f"\n\n🎯 <b>AUTOMATIC TRIGGER ARMED #{trig_res['trigger_id']}:</b>\n"
                                f"Conviction score (<b>{score}/100</b>) meets the trigger threshold of {trigger_min_score}! Automatically armed {trig_res['trigger_type']} "
                                f"at <b>{trig_res['target_price']} {data.get('currency', 'USD')}</b> ({dist_pct:+.1f}%) with €{trig_res['alloc_eur']:,.2f} reserved."
                            )
                            await self.notify_trigger_armed(trig_res)
                        else:
                            auto_armed_note = f"\n\n⚠️ <i>Could not auto-arm trigger: {html.escape(str(trig_res.get('message')))}</i>"
                    elif blocked.code == "POSITION_OPEN":
                        auto_armed_note = "\n\n💼 <i>Position already open in portfolio (anti-overlap active).</i>"
                    elif blocked.code == "TRIGGER_PENDING":
                        auto_armed_note = f"\n\n⏳ <i>Pending trigger already active for {html.escape(target_ticker)}.</i>"
                    elif blocked.code == "INSUFFICIENT_CASH":
                        auto_armed_note = f"\n\n⏸️ <i>Conviction score {score}/100 meets the trigger threshold of {trigger_min_score}, but cash is insufficient (€{available_cash:.2f}).</i>"
                    else:
                        auto_armed_note = f"\n\n🚫 <i>Not auto-armed: {html.escape(blocked.message)}</i>"
                elif verdict.consensus_score is not None and verdict.consensus_score >= trigger_min_score:
                    # A score that would have armed a trigger, held back by the verdict (HOLD, SELL, Risk Officer veto).
                    # A score below the threshold is simply not high enough, which needs no comment.
                    auto_armed_note = f"\n\n🚫 <i>Not auto-armed: {html.escape(verdict.message)}</i>"

                tech = report.get('technical', {})
                fund = report.get('fundamental', {})
                risk = report.get('risk', {})
                reply = (
                    f"🏛️ {action_icon} <b>COMMITTEE VERDICT: {report.get('action', 'HOLD')}</b> ({score}/100)\n\n"
                    f"<b>Asset:</b> {target_ticker} at {data['price']} {data.get('currency', 'USD')}\n\n"
                    f"<b>Summary:</b>\n{html.escape(report.get('summary', ''))}\n\n"
                    f"<b>AI Entry Trigger:</b> {report.get('trigger_type', 'LIMIT')} @ <code>{report.get('entry_trigger_price', data['price'])}</code>\n"
                    f"<b>Technical:</b> {tech.get('bias', 'NEUTRAL')} ({html.escape(tech.get('analysis_summary', ''))})\n"
                    f"<b>Fundamental:</b> {fund.get('valuation', 'FAIR')}\n"
                    f"<b>Risk Parameters:</b> Stop Loss @ <code>{risk.get('stop_loss_price')}</code>, Take Profit @ <code>{risk.get('take_profit_price')}</code>"
                    f"{auto_armed_note}"
                )
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /analyze: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error analyzing {parts[1] if len(parts) > 1 else ''}: {html.escape(str(e))}")

        elif command in ("watchlist", "watch", "radar"):
            try:
                from src.db.session import AsyncSessionLocal
                from src.db.models import WatchlistItem
                from sqlalchemy import select

                # Missing argument checks for subcommands
                if len(parts) == 2:
                    sub_cmd = parts[1].lower()
                    if sub_cmd in ("add", "+"):
                        await self._reply_chat(chat_id, "⚠️ Usage: <code>/watchlist add TICKER</code> (e.g. <code>/watchlist add NVDA</code>)")
                        return
                    elif sub_cmd in ("del", "delete", "remove", "rm", "-"):
                        await self._reply_chat(chat_id, "⚠️ Usage: <code>/watchlist del TICKER</code> (e.g. <code>/watchlist del NVDA</code>)")
                        return

                target_add_ticker = None
                target_del_ticker = None

                if len(parts) >= 3 and parts[1].lower() in ("add", "+"):
                    target_add_ticker = parts[2].upper().lstrip("$")
                elif len(parts) == 2 and parts[1].startswith("+") and len(parts[1]) > 1:
                    target_add_ticker = parts[1][1:].upper().lstrip("$")
                elif len(parts) >= 3 and parts[1].lower() in ("del", "delete", "remove", "rm", "-"):
                    target_del_ticker = parts[2].upper().lstrip("$")
                elif len(parts) == 2 and parts[1].startswith("-") and len(parts[1]) > 1:
                    target_del_ticker = parts[1][1:].upper().lstrip("$")

                # Subcommand: add
                if target_add_ticker:
                    ticker = target_add_ticker
                    name = ticker
                    try:
                        data = await asyncio.to_thread(market_data_provider.get_stock_data, ticker)
                        if data and data.get("name"):
                            name = data.get("name")
                    except Exception:
                        pass

                    async with AsyncSessionLocal() as session:
                        res = await session.execute(select(WatchlistItem).where(WatchlistItem.ticker == ticker))
                        item = res.scalars().first()
                        if item:
                            item.is_active = True
                            if name != ticker:
                                item.name = name
                        else:
                            session.add(WatchlistItem(ticker=ticker, name=name, sector="General"))
                        await session.commit()
                    await self._reply_chat(chat_id, f"✅ <b>{ticker}</b> ({html.escape(name)}) added to active radar watchlist.")
                    return

                # Subcommand: del
                if target_del_ticker:
                    ticker = target_del_ticker
                    async with AsyncSessionLocal() as session:
                        res = await session.execute(select(WatchlistItem).where(WatchlistItem.ticker == ticker))
                        item = res.scalars().first()
                        if item and item.is_active:
                            item.is_active = False
                            await session.commit()
                            await self._reply_chat(chat_id, f"✅ <b>{ticker}</b> removed from active watchlist.")
                        else:
                            await self._reply_chat(chat_id, f"⚠️ <b>{ticker}</b> is not on the active watchlist.")
                    return

                # Default: list watchlist
                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(WatchlistItem).where(WatchlistItem.is_active == True))
                    items = res.scalars().all()
                    tickers = [f"• <b>{w.ticker}</b> ({html.escape(w.name or '')})" for w in items]
                    tickers_str = "\n".join(tickers) if tickers else "No tickers currently in watchlist."
                    reply = (
                        f"📋 <b>ACTIVE WATCHLIST:</b>\n\n{tickers_str}\n\n"
                        f"💡 <i>Tip: Use <code>/watchlist add TICKER</code> or <code>/watchlist del TICKER</code> to manage.</i>"
                    )
                    await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /watchlist: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error accessing watchlist: {html.escape(str(e))}")

        elif command in ("stop", "pause", "resume", "start_buying"):
            enable = command in ("resume", "start_buying")
            val_str = "true" if enable else "false"
            try:
                from src.db.session import AsyncSessionLocal
                from src.db.models import SystemConfig
                from sqlalchemy import select
                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(SystemConfig).where(SystemConfig.key == "AUTO_BUY_ENABLED"))
                    cfg = res.scalars().first()
                    if cfg:
                        cfg.value = val_str
                    else:
                        session.add(SystemConfig(key="AUTO_BUY_ENABLED", value=val_str))
                    await session.commit()
                settings.AUTO_BUY_ENABLED = enable
                if enable:
                    reply = f"▶️ <b>Automated buying has been ENABLED.</b>\nHigh conviction opportunities (Score ≥ {settings.MIN_CONSENSUS_SCORE}) will execute automatically."
                else:
                    reply = "🛑 <b>Automated buying has been PAUSED.</b>\nThe committee will continue to advise, but no orders will execute automatically."
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                action_word = "enabling" if enable else "pausing"
                logger.error(f"Error handling /{command}: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error {action_word} auto-buying: {html.escape(str(e))}")

        elif command in ("status", "health", "info", "stats", "ping"):
            try:
                from src.db.session import AsyncSessionLocal
                from src.db.models import SystemConfig
                from src.data.market_hours import get_market_statuses
                from sqlalchemy import select
                auto_buy = settings.AUTO_BUY_ENABLED
                min_score = settings.MIN_CONSENSUS_SCORE
                trig_score = settings.TRIGGER_MIN_SCORE
                async with AsyncSessionLocal() as session:
                    res = await session.execute(select(SystemConfig))
                    cfgs = {c.key: c.value for c in res.scalars().all()}
                    if "AUTO_BUY_ENABLED" in cfgs:
                        auto_buy = cfgs["AUTO_BUY_ENABLED"].lower() == "true"
                    if "MIN_CONSENSUS_SCORE" in cfgs:
                        try:
                            min_score = int(cfgs["MIN_CONSENSUS_SCORE"])
                        except ValueError:
                            pass
                    if "TRIGGER_MIN_SCORE" in cfgs:
                        try:
                            trig_score = int(cfgs["TRIGGER_MIN_SCORE"])
                        except ValueError:
                            pass

                market_info = get_market_statuses()
                eu_status = market_info.get("eu_market", {}).get("status", "CLOSED")
                eu_badge = "🟢 OPEN" if eu_status == "OPEN" else "🔴 CLOSED"
                eu_hours = market_info.get("eu_market", {}).get("hours", "")
                us_status = market_info.get("us_market", {}).get("status", "CLOSED")
                us_badge = "🟢 OPEN" if us_status == "OPEN" else "🔴 CLOSED"
                us_hours = market_info.get("us_market", {}).get("hours", "")

                reply = (
                    "✅ <b>SYSTEM OPERATIONAL</b>\n\n"
                    f"• Base Currency: {settings.BASE_CURRENCY}\n"
                    f"• AI Provider: {settings.LLM_PROVIDER.upper()} (Zero Cost Plan)\n"
                    f"• Auto-Buy Mode: {'ENABLED' if auto_buy else 'PAUSED'}\n"
                    f"• Auto-Arm Trigger Threshold: Score ≥ {trig_score}\n"
                    f"• Min Consensus Score: Score ≥ {min_score}\n"
                    f"• Max Open Positions: {settings.MAX_POSITIONS}\n"
                    f"• European Markets (AEX/DAX): {eu_badge} ({eu_hours})\n"
                    f"• US Markets (NYSE/NASDAQ): {us_badge} ({us_hours})\n"
                    f"• Server Time: {market_info.get('current_time_cet', settings.TIMEZONE)}"
                )
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /status: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error retrieving status: {html.escape(str(e))}")

        elif command in ("review", "morning", "open", "close", "wrap", "wrapup", "evening", "market_close"):
            session = "OPEN"
            cmd_lower = command.lower()
            upper_parts = [p.upper() for p in parts]
            if cmd_lower in ("close", "wrap", "wrapup", "evening", "market_close") or any(p in ("CLOSE", "EVENING", "WRAP", "WRAPUP") for p in upper_parts):
                session = "CLOSE"

            target_market = "EU"
            if any(p in ("US", "USA", "AMERICA", "NASDAQ", "NYSE") for p in upper_parts):
                target_market = "US"

            session_name = "Closing Wrap-Up" if session == "CLOSE" else "Opening Market Review"
            await self._reply_chat(chat_id, f"⏳ Generating <b>{target_market} {session_name} & Advice</b>...")
            try:
                from src.scheduler.jobs import trading_scheduler
                await trading_scheduler.run_market_session(market=target_market, session=session, target_chat_id=chat_id)
            except Exception as e:
                logger.error(f"Error handling /review: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Failed generating market review: {html.escape(str(e))}")

        elif command in ("learning", "postmortem", "lessons"):
            try:
                from src.trading.learner import trade_learner
                insights = await trade_learner.get_learning_insights()
                params = insights.get("active_parameters", {})
                lessons = insights.get("recent_lessons", [])

                total_pnl = insights.get("total_realized_pnl_eur", 0.0) or 0.0
                pnl_sign = "+" if total_pnl >= 0 else ""
                
                lesson_lines = []
                for l in lessons[:3]:
                    outcome = l.get("outcome", "TRADE")
                    icon = "🟢" if outcome == "WIN" else ("🔴" if outcome == "LOSS" else "⚪")
                    pnl_pct = l.get("realized_pnl_pct", 0.0) or 0.0
                    pnl_eur = l.get("realized_pnl_eur", 0.0) or 0.0
                    holding_h = l.get("holding_period_hours", 0.0) or 0.0
                    exit_reason = html.escape(str(l.get("exit_reason", "Exit")))
                    what_wrong = html.escape(str(l.get("what_went_wrong", "None identified")))
                    lesson_text = html.escape(str(l.get("lesson", "N/A")))

                    lesson_lines.append(
                        f"{icon} <b>{outcome} on {l.get('ticker', '')}</b> ({pnl_pct:+.1f}% / €{pnl_eur:+.2f})\n"
                        f"  • <i>Exit:</i> {exit_reason} ({holding_h}h holding)\n"
                        f"  • <b>What went wrong:</b> {what_wrong}\n"
                        f"  • <b>Lesson:</b> {lesson_text}"
                    )
                lessons_str = "\n\n".join(lesson_lines) if lesson_lines else "• <i>No trade post-mortems recorded yet. Engine is monitoring active positions.</i>"

                try:
                    pos_size_pct = float(params.get('position_size_pct') or 0.08)
                except (ValueError, TypeError):
                    pos_size_pct = 0.08

                reply = (
                    f"🧠 <b>AI TRADER POST-MORTEMS & SELF-LEARNING</b>\n\n"
                    f"📊 <b>HISTORICAL PERFORMANCE:</b>\n"
                    f"• Closed Trades: <b>{insights.get('total_closed_trades', 0)}</b>\n"
                    f"• Win Rate: <b>{insights.get('win_rate_pct', 0.0)}%</b> ({insights.get('wins', 0)}W / {insights.get('losses', 0)}L)\n"
                    f"• Realized PnL: <b>{pnl_sign}€{total_pnl:.2f}</b>\n\n"
                    f"⚙️ <b>AUTONOMOUS ADAPTED PARAMETERS:</b>\n"
                    f"• Min Consensus Score: <code>{params.get('min_consensus_score', 70)}/100</code>\n"
                    f"• Base Position Size: <code>{pos_size_pct*100:.1f}%</code>\n"
                    f"• Stop ATR Multiplier: <code>{params.get('atr_stop_multiplier', 1.75)}x</code>\n"
                    f"• Target Reward-to-Risk: <code>1:{params.get('take_profit_rr', 2.1)}</code>\n\n"
                    f"📜 <b>RECENT POST-MORTEM LESSONS:</b>\n\n"
                    f"{lessons_str}\n\n"
                    f"💡 <i>Type <code>/calibrate</code> to re-tune parameters based on recent performance.</i>"
                )
                await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /learning: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error retrieving learning insights: {html.escape(str(e))}")

        elif command in ("calibrate", "tune"):
            await self._reply_chat(chat_id, "⏳ Re-evaluating historical trade patterns and calibrating parameters...")
            try:
                from src.trading.learner import trade_learner
                cal_res = await trade_learner.calibrate_internal_parameters()
                if not cal_res.get("success"):
                    msg = cal_res.get("message", "No closed trades available for calibration.")
                    reply = (
                        f"⚠️ <b>CALIBRATION NOTICE:</b> {html.escape(msg)}\n\n"
                        f"Current parameters are operating at baseline defaults. "
                        f"Autonomous risk and threshold re-tuning will automatically activate as trades close."
                    )
                    await self._reply_chat(chat_id, reply)
                else:
                    p = cal_res.get("calibrated_parameters", {})
                    def _cast(val, fn, default):
                        try:
                            return fn(val) if val is not None else default
                        except (ValueError, TypeError):
                            return default

                    min_score = _cast(p.get("min_consensus_score") or p.get("MIN_CONSENSUS_SCORE"), int, 70)
                    pos_size = _cast(p.get("position_size_pct") or p.get("POSITION_SIZE_PCT"), float, 0.08)
                    atr_mult = _cast(p.get("atr_stop_multiplier") or p.get("ATR_STOP_MULTIPLIER"), float, 1.75)
                    tp_rr = _cast(p.get("take_profit_rr") or p.get("TAKE_PROFIT_RR"), float, 2.1)
                    adjustments = "\n".join([f"• {html.escape(str(a))}" for a in cal_res.get("adjustments", [])]) or "• All parameters remain within optimal bounds."
                    reply = (
                        f"⚙️ <b>AUTONOMOUS PARAMETER CALIBRATION COMPLETE</b>\n\n"
                        f"• Evaluated Trades: <b>{cal_res.get('total_trades', 0)}</b> (Win Rate: {cal_res.get('win_rate_pct', 0.0)}%, Consecutive Losses: {cal_res.get('consecutive_losses', 0)})\n\n"
                        f"<b>Calibrated Risk Parameters:</b>\n"
                        f"• Min Consensus Score: <b>{min_score}/100</b>\n"
                        f"• Position Size: <b>{pos_size*100:.1f}%</b>\n"
                        f"• Stop Loss ATR Multiplier: <b>{atr_mult}x</b>\n"
                        f"• Target Take-Profit R:R: <b>1:{tp_rr}</b>\n\n"
                        f"<b>Modifications Made:</b>\n{adjustments}"
                    )
                    await self._reply_chat(chat_id, reply)
            except Exception as e:
                logger.error(f"Error handling /calibrate: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Calibration error: {html.escape(str(e))}")

        elif command in ("compare", "reviews", "weekly"):
            await self._reply_chat(chat_id, "⏳ Comparing market session reviews and pick follow-through from the past 7 days...")
            try:
                from src.data.review_tracker import review_tracker
                comp = await review_tracker.compare_past_week_reviews()
                if not comp.get("has_history"):
                    await self._reply_chat(chat_id, "📜 <b>PAST-WEEK REVIEWS</b>\n\nNo market reviews recorded in the past 7 days yet.")
                else:
                    await self._reply_chat(chat_id, comp.get("text_summary", "No review history available."))
            except Exception as e:
                logger.error(f"Error handling /compare: {e}", exc_info=True)
                await self._reply_chat(chat_id, f"❌ Error comparing reviews: {html.escape(str(e))}")

        else:
            reply = (
                f"❓ Unknown command: <code>/{html.escape(command)}</code>\n\n"
                f"Type <code>/help</code> to see all available commands, or <code>/analyze TICKER</code> to evaluate a stock."
            )
            await self._reply_chat(chat_id, reply)

    async def _reply_chat(self, chat_id: int, text: str) -> bool:
        """Replies to a specific Telegram chat with chunking and HTML plain text fallback."""
        return await self.send_message(text, chat_id=chat_id)

telegram_bot = TelegramAssistant()
