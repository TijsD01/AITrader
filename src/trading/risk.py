import logging
from typing import Dict, Any, Tuple, Optional, List
from config.settings import settings
from src.data.market_hours import check_market_hours

logger = logging.getLogger(__name__)

def calculate_position_allocation(
    available_cash: float,
    current_positions_count: int = 0,
    requested_alloc: Optional[float] = None,
    recommended_size_pct: Optional[float] = None,
    full_budget_utilization: Optional[bool] = None,
    max_single_order_eur: Optional[float] = None,
    max_positions: Optional[int] = None
) -> float:
    """
    Calculates the allocation for a new position or order trigger.
    
    When full_budget_utilization is True (or default settings.FULL_BUDGET_UTILIZATION):
      The bot dynamically divides the remaining available budget across the remaining open slots:
      allocation = available_cash / max(1, max_positions - current_positions_count)
      Capped at max_single_order_eur and available_cash.
      If only 1 slot is remaining, the entire available budget is deployed.
      This allows users who keep their reserves externally on the side to fully utilize
      the dedicated trading bot account capital.
    
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
        # Slices available cash evenly across remaining slots
        alloc = available_cash / float(remaining_slots)
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

def validate_buy_order(
    ticker: str,
    current_positions: list,
    cash_eur: float,
    portfolio_total_eur: float,
    allocation_eur: float,
    consensus_score: int,
    auto_buy_enabled: bool,
    available_cash_eur: Optional[float] = None
) -> Tuple[bool, str]:
    """
    Enforces institutional safety guardrails prior to executing any trade (paper or live).
    Only allows investing money currently available (no virtual budget).
    """
    # 0. Emergency Kill Switch Check
    if settings.KILL_SWITCH_ACTIVE:
        return False, "Emergency kill switch is ACTIVE. All new trading halted."

    # 1. Market Hours Check (No trades when market is closed)
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

    return True, "Approved"

import sys
risk_guardrail = sys.modules[__name__]
