import logging
from typing import Dict, Any, Tuple, Optional, List
from config.settings import settings
from src.data.market_hours import check_market_hours

logger = logging.getLogger(__name__)

def get_effective_deployable_cash(
    cash_eur: float,
    max_capital_usage_pct: Optional[float] = None,
    max_deployable_capital_eur: Optional[float] = None
) -> Tuple[float, float]:
    """
    Calculates the portion of total account cash allowed to be traded versus kept in reserve.
    Returns (usable_cash, protected_reserve).
    """
    if cash_eur <= 0.0:
        return 0.0, 0.0
    usage_pct = max_capital_usage_pct if max_capital_usage_pct is not None else settings.MAX_CAPITAL_USAGE_PCT
    if usage_pct > 1.0:
        usage_pct = usage_pct / 100.0
    usage_pct = max(0.0, min(1.0, usage_pct))
    usable = cash_eur * usage_pct
    cap = max_deployable_capital_eur if max_deployable_capital_eur is not None else settings.MAX_DEPLOYABLE_CAPITAL_EUR
    if cap is not None and cap > 0:
        usable = min(usable, cap)
    usable = round(max(0.0, min(cash_eur, usable)), 2)
    reserve = round(max(0.0, cash_eur - usable), 2)
    return usable, reserve

def calculate_position_allocation(
    available_cash: float,
    current_positions_count: int = 0,
    requested_alloc: Optional[float] = None,
    recommended_size_pct: Optional[float] = None,
    full_budget_utilization: Optional[bool] = None,
    max_single_order_eur: Optional[float] = None,
    max_positions: Optional[int] = None,
    ticker: Optional[str] = None,
    currency: Optional[str] = None
) -> float:
    """
    Calculates the allocation for a new position or order trigger.
    
    When full_budget_utilization is True (or default settings.FULL_BUDGET_UTILIZATION):
      The bot dynamically divides the remaining available budget across the remaining open slots:
      allocation = available_cash / max(1, max_positions - current_positions_count)
      Capped at max_single_order_eur and available_cash.
      If dividing across all slots yields allocations that are too small to justify
      broker fees (e.g. IBKR European minimum commission €2.00-€3.00), it dynamically
      consolidates slots so each trade carries sufficient capital to keep fee drag under ~3.5%.
    
    When full_budget_utilization is False:
      Uses the traditional conservative size percentage (e.g. 8% of equity).
    """
    if available_cash <= 0.0:
        return 0.0

    # Explicit user-requested allocation takes precedence (capped at available cash)
    if requested_alloc is not None and requested_alloc > 0:
        return round(min(available_cash, requested_alloc), 2)

    use_full_budget = full_budget_utilization if full_budget_utilization is not None else settings.FULL_BUDGET_UTILIZATION
    single_cap = max_single_order_eur if max_single_order_eur is not None else settings.MAX_SINGLE_ORDER_EUR
    total_slots = max_positions if max_positions is not None else settings.MAX_POSITIONS

    if use_full_budget:
        remaining_slots = max(1, total_slots - current_positions_count)
        raw_alloc = available_cash / float(remaining_slots)

        # Check broker fee viable floor for the target asset
        from src.trading.fees import get_min_viable_order_eur
        min_viable = get_min_viable_order_eur(ticker, currency=currency or "EUR") if ticker else get_min_viable_order_eur("DEFAULT.EU", currency=currency or "EUR")

        # If dividing across all slots yields allocations that dilute below min_viable:
        if raw_alloc < min_viable:
            if available_cash >= min_viable:
                max_viable_slots = max(1, int(available_cash // min_viable))
                effective_slots = min(remaining_slots, max_viable_slots)
                alloc = available_cash / float(effective_slots)
            else:
                # Available cash cannot support min_viable across multiple slots;
                # consolidate into 1 slot to maximize viability instead of creating micro-orders
                alloc = available_cash
        else:
            alloc = raw_alloc

        # Cap by single order ceiling (if set) and available cash
        alloc = min(alloc, single_cap, available_cash)
        # If allocation is smaller than minimum €20, attempt deploying entire available cash if cash >= €20
        if alloc < 20.0 and available_cash >= 20.0:
            alloc = min(available_cash, single_cap)
        return round(alloc, 2)
    else:
        # Standard conservative sizing
        raw_size = recommended_size_pct if recommended_size_pct is not None else settings.POSITION_SIZE_PCT
        if raw_size > 1.0:
            raw_size = raw_size / 100.0
        size_pct = max(0.02, min(settings.MAX_POSITION_SIZE_PCT, raw_size))
        alloc = round(available_cash * size_pct, 2)
        alloc = min(alloc, single_cap, available_cash)
        return round(alloc, 2)

def allocate_pending_triggers(
    usable_cash: float,
    current_positions_count: int,
    triggers: List[Any],
    max_positions: Optional[int] = None
) -> Dict[int, float]:
    """
    Distributes usable cash across pending order triggers, highest consensus score first
    (oldest first on ties). Each funded trigger receives the per-slot allocation until
    cash or free position slots run out; the remaining triggers are left unfunded (€0).
    Returns {trigger_id: allocation_eur}; the total never exceeds usable_cash.
    """
    total_slots = max_positions if max_positions is not None else settings.MAX_POSITIONS
    free_slots = max(0, total_slots - current_positions_count)
    ranked = sorted(triggers, key=lambda t: (-(t.consensus_score or 0), t.created_at, t.id))

    allocations: Dict[int, float] = {}
    remaining = max(0.0, usable_cash)
    for rank, t in enumerate(ranked):
        alloc = 0.0
        if rank < free_slots:
            slot_alloc = calculate_position_allocation(
                available_cash=usable_cash,
                current_positions_count=current_positions_count,
                full_budget_utilization=settings.FULL_BUDGET_UTILIZATION,
                max_positions=total_slots,
                ticker=t.ticker,
                currency=t.currency
            )
            alloc = round(min(slot_alloc, remaining), 2)
            if alloc < 20.0:
                alloc = 0.0
        allocations[t.id] = alloc
        remaining = round(remaining - alloc, 2)
    return allocations

def validate_buy_order(
    ticker: str,
    current_positions: list,
    cash_eur: float,
    portfolio_total_eur: float,
    allocation_eur: float,
    consensus_score: int,
    auto_buy_enabled: bool,
    available_cash_eur: Optional[float] = None,
    price_native: Optional[float] = None,
    currency: Optional[str] = None,
    stop_loss_native: Optional[float] = None,
    take_profit_native: Optional[float] = None
) -> Tuple[bool, str]:
    """
    Enforces institutional safety guardrails prior to executing any trade (paper or live).
    Only allows investing money currently available (no virtual budget).
    Factors in broker commission drag and profit erosion.
    """
    # 0. Emergency Kill Switch Check
    if settings.KILL_SWITCH_ACTIVE:
        return False, "Emergency kill switch is ACTIVE. All new trading halted."

    # 1. Market Hours Check (No trades when market is closed)
    if settings.ENFORCE_MARKET_HOURS:
        is_open, open_msg = check_market_hours(ticker)
        if not is_open:
            return False, open_msg

    # 2. Is auto-buy enabled?
    if not auto_buy_enabled:
        return False, "Automated buying is currently paused in settings."

    # 3. Consensus score threshold
    if consensus_score < settings.MIN_CONSENSUS_SCORE:
        return False, f"Consensus score {consensus_score} is below minimum threshold ({settings.MIN_CONSENSUS_SCORE})."

    # 4. Prevent duplicate positions (cannot hold multiple open positions in the same ticker)
    for pos in current_positions:
        if getattr(pos, "ticker", "").upper() == ticker.upper() and getattr(pos, "status", "") == "OPEN":
            return False, f"Position already exists for {ticker}."

    # 5. Maximum open positions limit
    open_count = sum(1 for p in current_positions if getattr(p, "status", "") == "OPEN")
    if open_count >= settings.MAX_POSITIONS:
        return False, f"Max open positions reached ({open_count}/{settings.MAX_POSITIONS})."

    # 6. Only invest money that you actually have (no virtual budget, no negative cash)
    effective_cash = available_cash_eur if available_cash_eur is not None else cash_eur
    if effective_cash <= 0:
        return False, f"No available cash (€{effective_cash:.2f}) to invest."

    if allocation_eur > effective_cash:
        return False, f"Insufficient cash (€{effective_cash:.2f}) for required allocation (€{allocation_eur:.2f})."

    # 7. Maximum single order size hard cap
    if settings.MAX_SINGLE_ORDER_EUR and allocation_eur > (settings.MAX_SINGLE_ORDER_EUR + 0.01):
        return False, f"Order allocation €{allocation_eur:.2f} exceeds single order limit (€{settings.MAX_SINGLE_ORDER_EUR:.2f})."

    # 8. Minimum trade size sanity check
    if allocation_eur < 20.0:
        return False, f"Allocation amount €{allocation_eur:.2f} is too small (< €20)."

    # 9. Broker Fee & Economic Viability Check
    if price_native is not None and price_native > 0:
        from src.trading.fees import evaluate_trade_economic_viability
        viability = evaluate_trade_economic_viability(
            ticker=ticker,
            allocation_eur=allocation_eur,
            price_native=price_native,
            currency=currency or "EUR",
            stop_loss_native=stop_loss_native,
            take_profit_native=take_profit_native,
            broker=settings.LIVE_BROKER,
            pricing_tier=getattr(settings, "IBKR_PRICING_TIER", "tiered"),
            max_fee_drag_pct=getattr(settings, "MAX_FEE_DRAG_PCT", 3.5)
        )
        if not viability["viable"]:
            return False, f"Fee guardrail blocked order: {viability['reason']}"

    return True, "Approved"

def evaluate_trigger_condition(
    trigger_type: str,
    target_price: float,
    curr_price: float,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None
) -> Tuple[bool, Optional[str]]:
    """
    Decides whether a pending price trigger should fire at curr_price.
    Returns (condition_met, invalidation_reason). A non-empty reason means the
    setup no longer holds (price already through the stop or the target) and
    the trigger should be cancelled instead of bought.
    """
    if trigger_type == "LIMIT_PULLBACK":
        if curr_price > target_price:
            return False, None
        if stop_loss and curr_price <= stop_loss:
            return False, f"price {curr_price} already at/below stop-loss {stop_loss}"
        return True, None
    if trigger_type == "BREAKOUT_BUY":
        if curr_price < target_price:
            return False, None
        if take_profit and curr_price >= take_profit:
            return False, f"price {curr_price} already at/above take-profit {take_profit}"
        return True, None
    return False, None

def is_trigger_expired(created_at, now=None, max_age_days: Optional[int] = None) -> bool:
    """True when a pending trigger (naive UTC created_at) is older than TRIGGER_EXPIRY_DAYS."""
    import datetime
    if created_at is None:
        return False
    days = max_age_days if max_age_days is not None else settings.TRIGGER_EXPIRY_DAYS
    if not days or days <= 0:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - created_at) > datetime.timedelta(days=days)

import sys
risk_guardrail = sys.modules[__name__]
