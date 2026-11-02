"""
Broker Fee and Commission Modeling for the Multi-Agent Trading System.
Provides transaction cost calculations for Trading 212 and Alpaca, enforcing economic viability guardrails to prevent fee drag
from destroying portfolio returns on small accounts.
"""

import logging
from typing import Dict, Any, Optional, Tuple
from config.settings import settings
from src.data.currency import currency_service

logger = logging.getLogger(__name__)

# European exchange suffixes
EU_EXCHANGE_SUFFIXES = (
    ".AS",  # Euronext Amsterdam (Netherlands)
    ".PA",  # Euronext Paris (France)
    ".BR",  # Euronext Brussels (Belgium)
    ".DE",  # Deutsche Börse Xetra (Germany)
    ".MC",  # Bolsa de Madrid (Spain)
    ".MI",  # Borsa Italiana (Italy)
    ".LS",  # Euronext Lisbon (Portugal)
    ".L",   # London Stock Exchange (UK)
    ".VI",  # Vienna (Austria)
    ".SW",  # SIX Swiss Exchange (Switzerland)
    ".ST",  # Nasdaq Stockholm (Sweden)
    ".HE",  # Nasdaq Helsinki (Finland)
    ".CO",  # Nasdaq Copenhagen (Denmark)
    ".OL",  # Oslo Bors (Norway)
)

def is_european_ticker(ticker: str, currency: Optional[str] = None) -> bool:
    """Returns True if the ticker is traded on a European exchange or denominated in EUR."""
    upper_tick = ticker.upper()
    if any(upper_tick.endswith(sfx) for sfx in EU_EXCHANGE_SUFFIXES):
        return True
    if currency and currency.upper() in ("EUR", "GBP"):
        return True
    return False

def calculate_broker_fee(
    ticker: str,
    shares: float,
    price_native: float,
    currency: str = "EUR",
    broker: Optional[str] = None,
    order_action: str = "BUY"
) -> Dict[str, Any]:
    """
    Calculates estimated broker commissions and regulatory fees for a single order.
    
    Supported Brokers:
      - Trading 212: no commission, 0.15% FX fee on non-EUR instruments.
      - Alpaca: Commission-free US equities (SEC/FINRA regulatory fees on sell only).
    """
    broker_name = (broker or settings.LIVE_BROKER or "trading212").lower()
    action = order_action.upper()
    curr = currency.upper()

    trade_value_native = round(shares * price_native, 4)
    trade_value_eur = currency_service.to_eur(trade_value_native, curr)
    is_eu = is_european_ticker(ticker, curr)

    if broker_name == "trading212":
        # Trading 212 Invest: no commission; 0.15% FX fee if instrument currency != EUR (buy and sell)
        fx = trade_value_native * 0.0015 if curr != "EUR" else 0.0
        ftt = trade_value_native * 0.004 if (ticker.upper().endswith(".PA") and action == "BUY") else 0.0
        stamp = trade_value_native * 0.005 if ((ticker.upper().endswith(".L") or curr == "GBP") and action == "BUY") else 0.0
        fee_native = round(fx + ftt + stamp, 2)
        fee_eur = round(currency_service.to_eur(fee_native, curr), 2)
        breakdown = "Trading 212 (€0 commission + 0.15% FX)" + (f" + {ftt:.2f} FTT" if ftt else "") + (f" + {stamp:.2f} SDRT" if stamp else "")

    elif broker_name == "alpaca":
        # Alpaca: Zero commission US equities; small regulatory fee on sells
        if action == "SELL":
            sec_fee = trade_value_native * 0.0000278
            finra_taf = min(8.30, shares * 0.000166)
            fee_native = round(max(0.01, sec_fee + finra_taf), 2)
            breakdown = "Alpaca US Regulatory (SEC/FINRA sell fee)"
        else:
            fee_native = 0.0
            breakdown = "Alpaca US Free Execution"
        fee_eur = round(currency_service.to_eur(fee_native, "USD"), 2)

    else:
        # Default fallback flat 0.1% or min €1.00
        fee_eur = round(max(1.0, trade_value_eur * 0.001), 2)
        fee_native = fee_eur
        breakdown = f"Generic Broker flat fee ({broker_name})"

    fee_pct = round((fee_eur / trade_value_eur * 100), 2) if trade_value_eur > 0 else 0.0

    return {
        "broker": broker_name.upper(),
        "is_european": is_eu,
        "currency": curr,
        "trade_value_native": trade_value_native,
        "trade_value_eur": trade_value_eur,
        "fee_native": fee_native,
        "fee_eur": fee_eur,
        "fee_pct": fee_pct,
        "breakdown": breakdown
    }

def calculate_round_trip_fee(
    ticker: str,
    shares: float,
    entry_price: float,
    exit_price: Optional[float] = None,
    currency: str = "EUR",
    broker: Optional[str] = None,
) -> Dict[str, Any]:
    """Calculates entry + exit round-trip broker commission and fee drag."""
    exit_p = exit_price if (exit_price and exit_price > 0) else entry_price
    entry_fee_info = calculate_broker_fee(
        ticker=ticker,
        shares=shares,
        price_native=entry_price,
        currency=currency,
        broker=broker,
        order_action="BUY"
    )
    exit_fee_info = calculate_broker_fee(
        ticker=ticker,
        shares=shares,
        price_native=exit_p,
        currency=currency,
        broker=broker,
        order_action="SELL"
    )

    round_trip_fee_eur = round(entry_fee_info["fee_eur"] + exit_fee_info["fee_eur"], 2)
    entry_val_eur = entry_fee_info["trade_value_eur"]
    round_trip_drag_pct = round((round_trip_fee_eur / entry_val_eur * 100), 2) if entry_val_eur > 0 else 0.0

    return {
        "entry_fee_eur": entry_fee_info["fee_eur"],
        "exit_fee_eur": exit_fee_info["fee_eur"],
        "round_trip_fee_eur": round_trip_fee_eur,
        "round_trip_drag_pct": round_trip_drag_pct,
        "entry_breakdown": entry_fee_info["breakdown"],
        "exit_breakdown": exit_fee_info["breakdown"],
        "is_european": entry_fee_info["is_european"]
    }

def get_min_viable_order_eur(
    ticker: str,
    currency: str = "EUR",
    broker: Optional[str] = None,
    max_fee_drag_pct: Optional[float] = None
) -> float:
    """
    Computes the minimum trade allocation (in EUR) required to keep round-trip
    broker fees below the maximum allowable fee drag percentage (default 3.5%).
    """
    broker_name = (broker or settings.LIVE_BROKER or "trading212").lower()
    if broker_name == "trading212":
        return 1.0  # fractional, no commission minimum
    elif broker_name == "alpaca":
        return 20.0
    return 50.0

def evaluate_trade_economic_viability(
    ticker: str,
    allocation_eur: float,
    price_native: float,
    currency: str = "EUR",
    stop_loss_native: Optional[float] = None,
    take_profit_native: Optional[float] = None,
    broker: Optional[str] = None,
    max_fee_drag_pct: Optional[float] = None,
    max_profit_erosion_pct: Optional[float] = None
) -> Dict[str, Any]:
    """
    Evaluates whether a trade is economically viable after factoring in broker fees.
    Ensures that broker commissions do not exceed or excessively erode expected gains.
    """
    label = (broker or settings.LIVE_BROKER or "trading212").upper()
    if allocation_eur <= 0 or price_native <= 0:
        return {
            "viable": False,
            "reason": "Invalid allocation or price.",
            "min_viable_allocation_eur": 50.0
        }

    price_eur = currency_service.to_eur(price_native, currency)
    shares = round(allocation_eur / price_eur, 4) if price_eur > 0 else 0.0
    if shares <= 0:
        return {
            "viable": False,
            "reason": "Share count rounds to zero.",
            "min_viable_allocation_eur": 50.0
        }

    rt_info = calculate_round_trip_fee(
        ticker=ticker,
        shares=shares,
        entry_price=price_native,
        exit_price=take_profit_native or price_native,
        currency=currency,
        broker=broker,
    )

    rt_fee_eur = rt_info["round_trip_fee_eur"]
    drag_pct = rt_info["round_trip_drag_pct"]
    max_drag = max_fee_drag_pct if max_fee_drag_pct is not None else getattr(settings, "MAX_FEE_DRAG_PCT", 3.8)
    max_erosion = max_profit_erosion_pct if max_profit_erosion_pct is not None else getattr(settings, "MAX_PROFIT_EROSION_PCT", 48.0)
    min_viable_alloc = get_min_viable_order_eur(
        ticker=ticker,
        currency=currency,
        broker=broker,
        max_fee_drag_pct=max_drag
    )

    # Calculate expected profit at take-profit target
    tp_native = take_profit_native if (take_profit_native and take_profit_native > price_native) else (price_native * (1.0 + settings.DEFAULT_TAKE_PROFIT_PCT))
    sl_native = stop_loss_native if (stop_loss_native and stop_loss_native < price_native) else (price_native * (1.0 - settings.DEFAULT_STOP_LOSS_PCT))

    tp_eur = currency_service.to_eur(tp_native, currency)
    sl_eur = currency_service.to_eur(sl_native, currency)

    gross_profit_eur = round(shares * (tp_eur - price_eur), 2)
    gross_loss_eur = round(shares * (price_eur - sl_eur), 2)

    net_profit_eur = round(gross_profit_eur - rt_fee_eur, 2)
    net_loss_eur = round(gross_loss_eur + rt_fee_eur, 2)
    profit_erosion_pct = round((rt_fee_eur / gross_profit_eur * 100), 1) if gross_profit_eur > 0 else 100.0

    raw_rr = round(gross_profit_eur / gross_loss_eur, 2) if gross_loss_eur > 0 else 2.0
    net_rr = round(net_profit_eur / net_loss_eur, 2) if net_loss_eur > 0 else 0.0

    # Validation Checks:
    # 1. Total round-trip fees exceed gross target profit (guaranteed loss on winner)
    if net_profit_eur <= 0:
        msg = (
            f"Trade economically non-viable: Estimated {label} round-trip fees (€{rt_fee_eur:.2f}) "
            f"exceed total target profit (€{gross_profit_eur:.2f}). Net outcome would be -€{abs(net_profit_eur):.2f}. "
            f"Min viable order for {ticker} is €{min_viable_alloc:.2f}."
        )
        return {
            "viable": False,
            "reason": msg,
            "net_profit_eur": net_profit_eur,
            "gross_profit_eur": gross_profit_eur,
            "round_trip_fee_eur": rt_fee_eur,
            "fee_drag_pct": drag_pct,
            "profit_erosion_pct": profit_erosion_pct,
            "min_viable_allocation_eur": min_viable_alloc,
            "raw_rr": raw_rr,
            "net_rr": net_rr
        }

    # 2. Excessive fee drag (> max_drag %)
    if drag_pct > max_drag:
        msg = (
            f"Excessive fee drag: {label} round-trip commission (€{rt_fee_eur:.2f}, {drag_pct:.1f}% of order) "
            f"exceeds limit ({max_drag}%). Sizing too small for broker fees. "
            f"Min viable order for {ticker} is €{min_viable_alloc:.2f}."
        )
        return {
            "viable": False,
            "reason": msg,
            "net_profit_eur": net_profit_eur,
            "gross_profit_eur": gross_profit_eur,
            "round_trip_fee_eur": rt_fee_eur,
            "fee_drag_pct": drag_pct,
            "profit_erosion_pct": profit_erosion_pct,
            "min_viable_allocation_eur": min_viable_alloc,
            "raw_rr": raw_rr,
            "net_rr": net_rr
        }

    # 3. Excessive profit erosion (> max_erosion %)
    if profit_erosion_pct > max_erosion:
        msg = (
            f"Excessive profit erosion: Broker fees (€{rt_fee_eur:.2f}) consume {profit_erosion_pct:.1f}% "
            f"of target profit (€{gross_profit_eur:.2f}, max allowed {max_erosion}%). "
            f"Min viable order for {ticker} is €{min_viable_alloc:.2f}."
        )
        return {
            "viable": False,
            "reason": msg,
            "net_profit_eur": net_profit_eur,
            "gross_profit_eur": gross_profit_eur,
            "round_trip_fee_eur": rt_fee_eur,
            "fee_drag_pct": drag_pct,
            "profit_erosion_pct": profit_erosion_pct,
            "min_viable_allocation_eur": min_viable_alloc,
            "raw_rr": raw_rr,
            "net_rr": net_rr
        }

    return {
        "viable": True,
        "reason": f"Economically viable (Fee drag {drag_pct:.1f}%, Net profit €{net_profit_eur:.2f}, Net R:R {net_rr}:1)",
        "net_profit_eur": net_profit_eur,
        "gross_profit_eur": gross_profit_eur,
        "round_trip_fee_eur": rt_fee_eur,
        "fee_drag_pct": drag_pct,
        "profit_erosion_pct": profit_erosion_pct,
        "min_viable_allocation_eur": min_viable_alloc,
        "raw_rr": raw_rr,
        "net_rr": net_rr
    }
