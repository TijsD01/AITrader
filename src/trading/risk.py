import logging
from typing import Dict, Any, Tuple, Optional, List
from config.settings import settings
from src.data.market_hours import check_market_hours

logger = logging.getLogger(__name__)

def get_effective_deployable_cash(
    cash_eur: float,
    max_capital_usage_pct: Optional[float] = None,
    max_deployable_capital_eur: Optional[float] = None,
    invested_eur: float = 0.0
) -> Tuple[float, float]:
    """
    Calculates the portion of total account cash allowed to be traded versus kept in reserve.
    The EUR cap limits the total in the market: cash already invested (cost basis of open positions) counts against it.
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
        usable = min(usable, max(0.0, cap - invested_eur))
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
    max_positions: Optional[int] = None,
    portfolio_value_eur: float = 0.0
) -> Dict[int, float]:
    """
    Distributes usable cash across pending order triggers, highest consensus score first
    (oldest first on ties). Each funded trigger receives the per-slot allocation until
    cash or free position slots run out; the remaining triggers are left unfunded (€0).
    In whole-share mode a trigger gets at least the cost of its one share when that exceeds the slot (see
    affordable_share_count) and nothing when no share fits the cash still left.
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
            elif settings.WHOLE_SHARES_ONLY and getattr(t, "target_price", None):
                # A share that costs more than the slot gets what it costs; a share that does not fit leaves the trigger unfunded
                alloc, shares = fit_allocation_to_share(
                    alloc, t.target_price, t.currency, getattr(t, "stop_loss", None), remaining, portfolio_value_eur
                )
                if shares < 1:
                    alloc = 0.0
        allocations[t.id] = alloc
        remaining = round(remaining - alloc, 2)
    return allocations

def new_entry_allocation(
    usable_cash: float,
    reserved_eur: float,
    current_positions_count: int,
    ticker: Optional[str] = None,
    currency: Optional[str] = None,
    requested_alloc: Optional[float] = None,
    max_positions: Optional[int] = None
) -> float:
    """
    Budget for a new trigger or buy: the per-slot amount allocate_pending_triggers gives every pending
    trigger (usable cash spread over the free position slots), limited to the cash that pending triggers
    have not reserved. Spreading only the unreserved cash over the free slots would shrink the budget
    for every trigger already pending, while the dynamic re-allocation hands it the full slot again.
    """
    free_cash = round(max(0.0, usable_cash - max(0.0, reserved_eur or 0.0)), 2)
    if requested_alloc is not None and requested_alloc > 0:
        return round(min(free_cash, requested_alloc), 2)
    slot = calculate_position_allocation(
        available_cash=usable_cash,
        current_positions_count=current_positions_count,
        full_budget_utilization=settings.FULL_BUDGET_UTILIZATION,
        max_positions=max_positions,
        ticker=ticker,
        currency=currency
    )
    return round(min(slot, free_cash), 2)

def entry_allocation_from_summary(
    summary: Dict[str, Any],
    ticker: Optional[str] = None,
    currency: Optional[str] = None,
    requested_alloc: Optional[float] = None
) -> float:
    """new_entry_allocation for a broker's get_portfolio_summary() result."""
    reserved = summary.get("reserved_triggers_eur", 0.0) or 0.0
    usable = summary.get("usable_capital_eur")
    if usable is None:
        usable = (summary.get("available_cash_eur", 0.0) or 0.0) + reserved
    return new_entry_allocation(
        usable_cash=usable,
        reserved_eur=reserved,
        current_positions_count=summary.get("open_positions_count", 0) or 0,
        ticker=ticker,
        currency=currency,
        requested_alloc=requested_alloc
    )

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

VALID_TRIGGER_TYPES = ("LIMIT_PULLBACK", "BREAKOUT_BUY")

def derive_trigger_levels(report: Dict[str, Any], current_price: float) -> Tuple[str, float, Optional[float], Optional[float]]:
    """
    Turns a committee report into (trigger_type, trigger_price, stop_loss, take_profit), forcing the
    entry on the correct side of the current price and the stop/target on the correct side of the entry.
    """
    trigger_price = report.get("entry_trigger_price", current_price)
    trigger_type = report.get("trigger_type", "LIMIT_PULLBACK")

    if trigger_type == "LIMIT_PULLBACK" and trigger_price >= current_price:
        trigger_price = round(current_price * 0.985, 2)
    elif trigger_type == "BREAKOUT_BUY" and trigger_price <= current_price:
        trigger_price = round(current_price * 1.01, 2)

    sl = report["risk"].get("stop_loss_price")
    tp = report["risk"].get("take_profit_price")
    if sl and sl >= trigger_price:
        sl = round(trigger_price * 0.96, 2)
    if tp and tp <= trigger_price:
        tp = round(trigger_price * 1.06, 2)
    return trigger_type, trigger_price, sl, tp

def validate_trigger_levels(
    trigger_type: str,
    target_price: float,
    current_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float]
) -> Optional[str]:
    """Returns an error message when a trigger's type or price levels are inconsistent, else None."""
    if trigger_type not in VALID_TRIGGER_TYPES:
        return f"Unknown trigger type '{trigger_type}' (expected LIMIT_PULLBACK or BREAKOUT_BUY)."
    if not target_price or target_price <= 0:
        return f"Trigger target price ({target_price}) must be positive."
    if trigger_type == "LIMIT_PULLBACK" and target_price >= current_price:
        return f"Trigger overlap error: LIMIT_PULLBACK target price ({target_price}) must be below current price ({current_price})."
    if trigger_type == "BREAKOUT_BUY" and target_price <= current_price:
        return f"Trigger overlap error: BREAKOUT_BUY target price ({target_price}) must be above current price ({current_price})."
    if stop_loss is not None and stop_loss >= target_price:
        return f"Trigger overlap error: Stop Loss ({stop_loss}) must be below target price ({target_price})."
    if take_profit is not None and take_profit <= target_price:
        return f"Trigger overlap error: Take Profit ({take_profit}) must be above target price ({target_price})."
    return None

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

def apply_risk_budget(
    allocation_eur: float,
    price_native: Optional[float],
    stop_loss_native: Optional[float],
    portfolio_value_eur: float,
    risk_pct: Optional[float] = None
) -> float:
    """
    Caps an automated buy so that hitting its stop loses at most risk_pct % of the portfolio
    (equal risk per trade instead of equal money per trade). A wide stop gets a smaller position.
    Leaves the allocation unchanged when there is no usable stop or the cap is switched off.
    """
    pct = settings.RISK_PER_TRADE_PCT if risk_pct is None else risk_pct
    if not pct or pct <= 0 or not price_native or price_native <= 0 or not stop_loss_native or portfolio_value_eur <= 0:
        return allocation_eur
    stop_distance = (price_native - stop_loss_native) / price_native
    if stop_distance <= 0:
        return allocation_eur
    cap = portfolio_value_eur * (pct / 100.0) / stop_distance
    return round(min(allocation_eur, cap), 2)

def risk_capped_allocation(
    allocation_eur: float,
    price_native: Optional[float],
    stop_loss_native: Optional[float],
    portfolio_value_eur: float,
    free_cash_eur: Optional[float] = None
) -> float:
    """
    The main bot's risk cap on an automated buy, measured as RISK_BUDGET_BASE says:
    "slot" keeps the full slot for a stop up to DEFAULT_STOP_LOSS_PCT away and scales a wider stop down,
    so every trade risks the same share of its slot; "portfolio" applies RISK_PER_TRADE_PCT of the
    portfolio (apply_risk_budget); "off" (or RISK_PER_TRADE_PCT = 0) leaves the allocation as is.
    In "slot" mode a buy that already takes all free cash (free_cash_eur, e.g. the last free slot) keeps it while
    the portfolio is below LAST_SLOT_FULL_CASH_MAX_EUR: in a small account the cut-off cash has no other slot to go to,
    in a large one a single uncapped position would risk too much.
    """
    base = (settings.RISK_BUDGET_BASE or "slot").strip().lower()
    if base == "off" or not settings.RISK_PER_TRADE_PCT or settings.RISK_PER_TRADE_PCT <= 0:
        return allocation_eur
    if base == "portfolio":
        return apply_risk_budget(allocation_eur, price_native, stop_loss_native, portfolio_value_eur)
    if free_cash_eur is not None and free_cash_eur > 0 and abs(allocation_eur - free_cash_eur) <= 0.01 \
            and portfolio_value_eur < settings.LAST_SLOT_FULL_CASH_MAX_EUR:
        return allocation_eur
    if not price_native or price_native <= 0 or not stop_loss_native:
        return allocation_eur
    stop_distance = (price_native - stop_loss_native) / price_native
    normal_stop = settings.DEFAULT_STOP_LOSS_PCT
    if stop_distance <= 0 or not normal_stop or normal_stop <= 0 or stop_distance <= normal_stop:
        return allocation_eur
    return round(allocation_eur * normal_stop / stop_distance, 2)

def plan_buy(
    allocation_eur: float,
    price_native: float,
    currency: str,
    stop_loss_native: Optional[float],
    portfolio_value_eur: float,
    max_eur: Optional[float] = None,
    automated: bool = True
) -> Dict[str, Any]:
    """
    What a buy of allocation_eur turns into at price_native: the risk cap (automated buys only),
    then whole shares when WHOLE_SHARES_ONLY is on. Execution and the trigger alerts and dashboard
    use this same sequence, so they show the same budget and share count.
    """
    from src.data.currency import currency_service
    budget = risk_capped_allocation(allocation_eur, price_native, stop_loss_native, portfolio_value_eur, max_eur) if automated else allocation_eur
    price_eur = currency_service.to_eur(price_native, currency) if price_native else 0.0
    if not budget or budget <= 0 or price_eur <= 0:
        shares = 0.0
    elif settings.WHOLE_SHARES_ONLY:
        shares = float(affordable_share_count(budget, price_eur, max_eur if max_eur is not None else allocation_eur, portfolio_value_eur))
    else:
        shares = round(budget / price_eur, 4)
    return {
        "budget_eur": round(budget, 2),
        "risk_capped": budget < allocation_eur - 0.005,
        "shares": shares,
        "price_eur": round(price_eur, 4),
        "cost_eur": round(shares * price_eur, 2),
    }

def plan_pending_triggers(
    triggers: List[Any],
    usable_cash: float,
    portfolio_value_eur: float,
    allocations: Optional[Dict[int, float]] = None
) -> Dict[int, Dict[str, Any]]:
    """
    plan_buy for every pending trigger at its limit price, using allocations (from allocate_pending_triggers)
    or else each trigger's stored allocation. Like execution, each buy may round up into cash that the other
    pending triggers have not reserved. Returns {trigger_id: plan with its allocation_eur}.
    """
    allocs = {t.id: max(0.0, (allocations or {}).get(t.id, t.allocation_eur) or 0.0) for t in triggers}
    reserved = sum(allocs.values())
    plans: Dict[int, Dict[str, Any]] = {}
    for t in triggers:
        alloc = allocs[t.id]
        available = max(0.0, usable_cash - (reserved - alloc))
        plan = plan_buy(alloc, t.target_price, t.currency, t.stop_loss, portfolio_value_eur, max_eur=available)
        plan["allocation_eur"] = round(alloc, 2)
        plans[t.id] = plan
    return plans

def compute_trailing_stop(
    entry_price: Optional[float],
    current_stop: Optional[float],
    highest_price: Optional[float],
    atr: Optional[float],
    breakeven_trigger_pct: float,
    atr_mult: Optional[float] = None
) -> Optional[float]:
    """
    Returns the ratcheted stop for a position; never lower than current_stop.
    Once the high-water mark is breakeven_trigger_pct above entry, the stop moves to at least entry + 1%.
    With atr_mult > 0 it then trails at highest_price - atr_mult x ATR, so winners can run
    while a reversal still locks in most of the gain.
    """
    if not entry_price or entry_price <= 0 or not highest_price:
        return current_stop
    if (highest_price / entry_price) - 1.0 < breakeven_trigger_pct:
        return current_stop
    candidate = entry_price * 1.01
    mult = settings.TRAILING_STOP_ATR_MULT if atr_mult is None else atr_mult
    if mult and mult > 0 and atr and atr > 0:
        candidate = max(candidate, highest_price - mult * atr)
    candidate = round(candidate, 2)
    if current_stop is not None and current_stop >= candidate:
        return current_stop
    return candidate

def whole_share_count(
    allocation_eur: float,
    price_eur: float,
    max_eur: Optional[float] = None,
    round_up_pct: Optional[float] = None
) -> int:
    """
    Whole number of shares closest to allocation_eur. Rounds up by one share only when that is
    closer to the allocation, costs at most round_up_pct % above it and stays within max_eur
    (the cash that is actually there). Returns 0 when even one share does not fit.
    """
    import math
    if not allocation_eur or allocation_eur <= 0 or not price_eur or price_eur <= 0:
        return 0
    pct = settings.WHOLE_SHARE_ROUND_UP_PCT if round_up_pct is None else round_up_pct
    ceiling = allocation_eur * (1.0 + max(0.0, pct) / 100.0)
    if max_eur is not None:
        ceiling = min(ceiling, max_eur)
    down = int(math.floor(min(allocation_eur, ceiling) / price_eur + 1e-9))
    up = down + 1
    if up * price_eur <= ceiling + 1e-9 and (up * price_eur - allocation_eur) < (allocation_eur - down * price_eur):
        return up
    return down

def affordable_share_count(
    budget_eur: float,
    price_eur: float,
    free_cash_eur: float,
    portfolio_value_eur: float = 0.0
) -> int:
    """
    The whole-share count every sizing decision uses: whole_share_count for the budget, but at least one share
    when the budget buys none and one share fits both the cash that is free for this buy and
    MAX_POSITION_PCT % of the portfolio (and MAX_SINGLE_ORDER_EUR). Returns 0 when no budget was given.
    """
    if not budget_eur or budget_eur <= 0 or not price_eur or price_eur <= 0:
        return 0
    free = max(0.0, free_cash_eur or 0.0)
    shares = whole_share_count(budget_eur, price_eur, max_eur=max(free, budget_eur))
    if shares >= 1:
        return shares
    if price_eur > free + 1e-9:
        return 0
    if portfolio_value_eur and portfolio_value_eur > 0 and price_eur > portfolio_value_eur * settings.MAX_POSITION_PCT / 100.0 + 1e-9:
        return 0
    if settings.MAX_SINGLE_ORDER_EUR and price_eur > settings.MAX_SINGLE_ORDER_EUR + 0.01:
        return 0
    return 1

def fit_allocation_to_share(
    allocation_eur: float,
    price_native: Optional[float],
    currency: str,
    stop_loss_native: Optional[float],
    free_cash_eur: float,
    portfolio_value_eur: float = 0.0,
    automated: bool = True
) -> Tuple[float, float]:
    """
    (allocation, shares) for a whole-share buy. When the planned shares cost more than the allocation
    (the one-share minimum, not the usual round-up), the allocation is raised to their cost, rounded up to the cent,
    so that reserving the allocation reserves the cash the buy will really spend. Returns the allocation
    unchanged, with 0 shares, when nothing fits. Whole-share mode only.
    """
    import math
    from src.data.currency import currency_service
    plan = plan_buy(allocation_eur, price_native, currency, stop_loss_native, portfolio_value_eur, max_eur=free_cash_eur, automated=automated)
    shares = plan["shares"]
    if shares < 1:
        return allocation_eur, 0.0
    price_eur = currency_service.to_eur(price_native, currency)
    if whole_share_count(plan["budget_eur"], price_eur, max_eur=max(free_cash_eur, plan["budget_eur"])) >= 1:
        return allocation_eur, shares  # the usual rounding: keep the allocation as it was
    return max(allocation_eur, math.ceil(shares * price_eur * 100 - 1e-6) / 100.0), shares

def regime_index_for_ticker(ticker: str, currency: Optional[str] = None) -> str:
    """The market index whose trend decides whether new entries in this ticker are allowed."""
    from src.trading.fees import is_european_ticker
    return settings.REGIME_INDEX_EU if is_european_ticker(ticker, currency) else settings.REGIME_INDEX_US

def check_market_regime(ticker: str, currency: Optional[str] = None) -> Tuple[bool, str]:
    """
    Market regime filter: blocks new automated entries while the ticker's market index trades
    below its REGIME_SMA_DAYS moving average. Returns (allowed, reason). Fails open when the
    index data cannot be fetched. Blocking (fetches index data); call it via asyncio.to_thread.
    """
    if not settings.REGIME_FILTER_ENABLED:
        return True, ""
    from src.data.market import market_data_provider
    index_symbol = regime_index_for_ticker(ticker, currency)
    regime = market_data_provider.get_index_regime(index_symbol, settings.REGIME_SMA_DAYS)
    if not regime:
        logger.warning(f"Regime filter: no data for {index_symbol}, allowing entry in {ticker}.")
        return True, ""
    if regime["above_sma"]:
        return True, ""
    return False, (
        f"Market regime filter: {index_symbol} ({regime['price']}) is below its "
        f"{regime['sma_days']}-day average ({regime['sma']}); no new entries in a falling market."
    )

import sys
risk_guardrail = sys.modules[__name__]
