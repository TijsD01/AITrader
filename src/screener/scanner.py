import logging
import time
from typing import List, Dict, Any, Optional
from config.default_universe import EU_STOCKS, US_STOCKS
from src.data.market import market_data_provider

logger = logging.getLogger(__name__)

# ponytail: in-memory scan cache, 5-min TTL
_scan_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
_SCAN_TTL: float = 300.0

class MarketScanner:
    @staticmethod
    def scan_universe(market: str = "ALL", limit: int = 10) -> List[Dict[str, Any]]:
        """
        Scans liquid universes (EU, US, or ALL) and ranks candidates
        based on momentum, trend alignment, and technical pullback criteria.
        """
        cache_key = f"{market.upper()}_{limit}"
        now = time.time()
        if cache_key in _scan_cache:
            cached_time, cached_data = _scan_cache[cache_key]
            if (now - cached_time) < _SCAN_TTL:
                return cached_data

        pool: List[Dict[str, str]] = []
        if market.upper() in ("EU", "ALL"):
            pool.extend(EU_STOCKS)
        if market.upper() in ("US", "ALL"):
            pool.extend(US_STOCKS)

        ranked_candidates: List[Dict[str, Any]] = []

        for item in pool:
            ticker = item["ticker"]
            data = market_data_provider.get_stock_data(ticker)
            if not data:
                continue

            score, setup_type, notes = MarketScanner._evaluate_technical_setup(data)
            if score >= 50:  # Only pass promising setups
                candidate = {
                    "ticker": ticker,
                    "name": data["name"],
                    "country": item.get("country", ""),
                    "sector": data.get("sector", item.get("sector", "")),
                    "price": data["price"],
                    "currency": data["currency"],
                    "pre_score": score,
                    "setup_type": setup_type,
                    "notes": notes,
                    "rsi": data["technicals"]["rsi14"],
                    "return_5d_pct": data["technicals"]["return_5d_pct"],
                    "volume_surge": data["technicals"]["volume_surge_ratio"],
                }
                ranked_candidates.append(candidate)

        # Sort descending by algorithmic pre_score
        ranked_candidates.sort(key=lambda x: x["pre_score"], reverse=True)
        result = ranked_candidates[:limit]
        _scan_cache[cache_key] = (now, result)
        return result

    @staticmethod
    def _evaluate_technical_setup(data: Dict[str, Any]) -> tuple[int, str, str]:
        t = data["technicals"]
        f = data["fundamentals"]

        score = 50
        setups = []
        notes = []

        # 1. Trend Alignment (Above 50 EMA and 200 SMA)
        if t["above_ema50"] and t["above_sma200"]:
            score += 15
            notes.append("Strong macro uptrend (>EMA50 & >SMA200)")
        elif t["above_ema20"] and t["above_ema50"]:
            score += 10
            notes.append("Short-term momentum (>EMA20 & >EMA50)")

        # 2. RSI Evaluation
        rsi = t["rsi14"]
        if 48 <= rsi <= 68:
            score += 15
            setups.append("Bullish Momentum")
            notes.append(f"Healthy RSI ({rsi}) in expansion zone")
        elif 35 <= rsi < 48 and t["above_sma200"]:
            score += 15
            setups.append("Oversold Pullback in Uptrend")
            notes.append(f"Dip-buying opportunity (RSI {rsi})")
        elif rsi > 75:
            score -= 10
            notes.append("Overextended RSI (>75)")

        # 3. MACD Momentum
        if t["macd"] > t["macd_signal"] and t["macd_hist"] > 0:
            score += 10
            notes.append("MACD Bullish Crossover")

        # 4. Volume Surge
        if t["volume_surge_ratio"] >= 1.2:
            score += 10
            notes.append(f"Volume Surge ({t['volume_surge_ratio']}x avg)")

        # 5. Near the 52-week high (George & Hwang 2004: a stronger momentum signal than past return)
        pct_below_high = t.get("pct_below_52w_high")
        if pct_below_high is not None and pct_below_high <= 5.0:
            score += 10
            notes.append(f"Within {pct_below_high}% of 52-week high")

        # 6. Fundamental bonus / penalty
        rev_growth = f.get("revenue_growth")
        if rev_growth and rev_growth > 0.08:
            score += 5
            notes.append(f"Revenue growth above 8% ({round(rev_growth * 100)}% YoY)")
        
        target_price = f.get("target_mean_price")
        if target_price and target_price > t["price"] * 1.10:
            score += 5
            notes.append(f"Wall St target upside ({round((target_price/t['price']-1)*100)}%)")

        setup_name = " & ".join(setups) if setups else "Consolidation / Range"
        notes_str = "; ".join(notes)
        return min(100, max(0, score)), setup_name, notes_str

market_scanner = MarketScanner()
