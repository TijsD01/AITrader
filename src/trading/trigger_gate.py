"""
Validation gate for price triggers that a person arms by hand (dashboard, Telegram).
The scheduler only arms approved buy verdicts; this applies the same bar to manual requests, with one
difference: a soft block (HOLD verdict, low score) can be overridden by an explicit force from the user,
a hard block (Risk Officer veto, SELL verdict, falling market, share too expensive) never.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

from sqlalchemy import select

from config.settings import settings
from src.db.session import AsyncSessionLocal
from src.db.models import Position, OrderTrigger, SystemConfig
from src.trading.risk import check_market_regime, plan_buy

logger = logging.getLogger(__name__)

# Letters, digits, dots and dashes (BRK-B, ASML.AS): no ^indices, =X currencies or free text
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
# yfinance quoteType values that are not a stock or ETF the broker can buy
_NON_TRADABLE_QUOTE_TYPES = frozenset({"INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "MUTUALFUND", "OPTION"})
_BUY_ACTIONS = ("BUY", "STRONG_BUY")
_MIN_TRIGGER_CASH_EUR = 20.0
_MAX_REASON_CHARS = 200


@dataclass
class TriggerGateResult:
    allowed: bool
    code: str                    # "OK", "OK_FORCED" or the code of the rule that blocked
    message: str                 # one plain English sentence for the user
    overridable: bool = False    # True only for soft blocks, which force=True turns into OK_FORCED
    action: Optional[str] = None
    consensus_score: Optional[int] = None
    trigger_min_score: Optional[int] = None

    def to_detail(self) -> Dict[str, Any]:
        """JSON-safe dict of all fields, used as the API error detail and the success 'gate' entry."""
        return asdict(self)


def normalize_ticker(raw: str) -> Optional[str]:
    """Upper-cased ticker without a leading '$', or None when it is not a plausible stock symbol."""
    if not isinstance(raw, str):
        return None
    ticker = raw.strip().upper().lstrip("$")
    return ticker if _TICKER_RE.match(ticker) else None


def invalid_ticker_result(raw: Any) -> TriggerGateResult:
    shown = str(raw).strip()[:30]
    return TriggerGateResult(False, "INVALID_TICKER", f"'{shown}' is not a valid stock ticker symbol.")


def is_tradable_quote_type(quote_type: Optional[str]) -> bool:
    """False for indices, currencies, crypto, futures, mutual funds and options. An unknown type is allowed."""
    if not quote_type:
        return True
    return str(quote_type).strip().upper() not in _NON_TRADABLE_QUOTE_TYPES


async def get_trigger_min_score() -> int:
    """Score needed to arm a trigger: the saved TRIGGER_MIN_SCORE setting, else the configured default."""
    threshold = settings.TRIGGER_MIN_SCORE
    try:
        async with AsyncSessionLocal() as db_session:
            res = await db_session.execute(select(SystemConfig).where(SystemConfig.key == "TRIGGER_MIN_SCORE"))
            cfg = res.scalars().first()
        if cfg and cfg.value:
            threshold = int(cfg.value)
    except ValueError:
        pass
    except Exception as e:
        logger.warning(f"Could not read TRIGGER_MIN_SCORE from the database, using {threshold}: {e}")
    return threshold


def _short(text: Any) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _MAX_REASON_CHARS else text[:_MAX_REASON_CHARS - 3].rstrip() + "..."


def evaluate_trigger_verdict(report: Dict[str, Any], trigger_min_score: int, force: bool = False) -> TriggerGateResult:
    """
    Rules 1-4 on a committee report, in order: Risk Officer veto and SELL verdict (hard), then a non-buy
    verdict and a score below the trigger threshold (soft). The first failing rule decides.
    A soft block comes back allowed with code OK_FORCED when force is set.
    """
    risk = report.get("risk") or {}
    action = str(report.get("action") or "").strip().upper()
    try:
        score = int(report.get("consensus_score") or 0)
    except (TypeError, ValueError):
        score = 0
    shown_action = action or None

    def result(allowed: bool, code: str, message: str, overridable: bool = False) -> TriggerGateResult:
        return TriggerGateResult(allowed, code, message, overridable, shown_action, score, trigger_min_score)

    if not risk.get("approved", True):
        reason = _short(risk.get("risk_summary"))
        return result(False, "RISK_VETO", f"The Risk Officer vetoed this trade: {reason}" if reason else "The Risk Officer vetoed this trade.")

    if action.startswith(("SELL", "STRONG_SELL")):
        return result(False, "SELL_VERDICT", f"The committee's verdict is {action} (score {score}/100), so there is nothing to buy.")

    block = None
    if action not in _BUY_ACTIONS:
        verdict = action or "missing"
        block = result(False, "NOT_BUY_VERDICT", f"The committee's verdict is {verdict} (score {score}/100), not a buy.", True)
    elif score < trigger_min_score:
        block = result(False, "LOW_SCORE", f"The committee says {action}, but its score of {score}/100 is below the trigger threshold of {trigger_min_score}.", True)

    if block is None:
        return result(True, "OK", f"{action} verdict with a score of {score}/100, at or above the trigger threshold of {trigger_min_score}.")
    if force:
        return result(True, "OK_FORCED", f"Armed against the committee's advice at your request. {block.message}")
    return block


async def _hard_block(
    ticker: str, data: Dict[str, Any], trigger_price: float, allocation_eur: float, verdict: TriggerGateResult,
    stop_loss: Optional[float] = None, portfolio_value_eur: float = 0.0
) -> Optional[TriggerGateResult]:
    """Rules 5-6: the market regime filter and the one-share check (after the risk cap). Never overridable."""
    def blocked(code: str, message: str) -> TriggerGateResult:
        return TriggerGateResult(False, code, message, False, verdict.action, verdict.consensus_score, verdict.trigger_min_score)

    currency = data.get("currency") or "EUR"
    regime_ok, regime_msg = await asyncio.to_thread(check_market_regime, ticker, data.get("currency"))
    if not regime_ok:
        return blocked("MARKET_REGIME", regime_msg)

    if settings.WHOLE_SHARES_ONLY and trigger_price and trigger_price > 0:
        # Sized like the buy itself: risk cap, then whole shares
        plan = await asyncio.to_thread(plan_buy, allocation_eur, trigger_price, currency, stop_loss, portfolio_value_eur)
        if plan["shares"] < 1:
            return blocked(
                "SHARE_TOO_EXPENSIVE",
                f"One share of {ticker} costs about €{plan['price_eur']:.2f}, more than the €{plan['budget_eur']:.2f} available for this trade."
            )
    return None


async def evaluate_manual_trigger(
    ticker: str,
    data: Dict[str, Any],
    report: Dict[str, Any],
    trigger_price: float,
    allocation_eur: float,
    trigger_min_score: int,
    force: bool = False,
    stop_loss: Optional[float] = None,
    portfolio_value_eur: float = 0.0
) -> TriggerGateResult:
    """
    Rules 1-6 for a manually armed trigger: the verdict rules (evaluate_trigger_verdict), then the market
    regime and one-share checks. Hard blocks win over a soft block, so a result with overridable=True
    always means that force=True would arm the trigger.
    """
    verdict = evaluate_trigger_verdict(report, trigger_min_score, force=force)
    if not verdict.allowed and not verdict.overridable:
        return verdict
    hard = await _hard_block(ticker, data, trigger_price, allocation_eur, verdict, stop_loss, portfolio_value_eur)
    return hard or verdict


async def _ticker_in_use(ticker: str) -> Optional[TriggerGateResult]:
    """POSITION_OPEN / TRIGGER_PENDING when the broker would refuse the trigger anyway, without an LLM call."""
    # Same condition as get_active_broker(); the queries mirror the brokers' create_price_trigger checks
    live = settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED
    pos_query = select(Position.id).where(Position.ticker == ticker, Position.status == "OPEN")
    if live:
        pos_query = pos_query.where(Position.is_live == True)
    trig_query = select(OrderTrigger.id).where(
        OrderTrigger.ticker == ticker, OrderTrigger.status == "PENDING", OrderTrigger.is_live == live
    )
    async with AsyncSessionLocal() as db_session:
        has_position = (await db_session.execute(pos_query)).first() is not None
        has_trigger = (await db_session.execute(trig_query)).first() is not None
    if has_position:
        return TriggerGateResult(False, "POSITION_OPEN", f"There is already an open position in {ticker}.")
    if has_trigger:
        return TriggerGateResult(False, "TRIGGER_PENDING", f"A trigger is already pending for {ticker}.")
    return None


async def precheck_manual_trigger(ticker: str, data: Optional[Dict[str, Any]], available_cash_eur: Optional[float] = None) -> Optional[TriggerGateResult]:
    """
    Cheap checks before the committee runs, all hard: market data found, a tradable instrument type, no open
    position or pending trigger for the ticker, and (when given) enough available cash.
    Returns the blocking result, or None when the committee may run. The ticker must already be normalized.
    """
    if not data:
        return TriggerGateResult(False, "NO_MARKET_DATA", f"Market data not found for '{ticker}'.")
    if not is_tradable_quote_type(data.get("quote_type")):
        kind = str(data.get("quote_type")).strip().upper()
        return TriggerGateResult(False, "NOT_TRADABLE", f"{ticker} is a {kind} quote, not a stock or ETF that can be bought.")
    in_use = await _ticker_in_use(ticker)
    if in_use:
        return in_use
    if available_cash_eur is not None and available_cash_eur < _MIN_TRIGGER_CASH_EUR:
        return TriggerGateResult(False, "INSUFFICIENT_CASH", f"Insufficient available cash (€{available_cash_eur:.2f}) to arm trigger.")
    return None
