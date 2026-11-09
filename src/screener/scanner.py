import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional
from config.default_universe import EU_STOCKS, US_STOCKS
from config.settings import settings
from src.data.market import market_data_provider
from src.data.market_hours import is_eu_ticker

logger = logging.getLogger(__name__)

# ponytail: in-memory scan cache, 5-min TTL
_scan_cache: Dict[str, tuple[float, List[Dict[str, Any]]]] = {}
_SCAN_TTL: float = 300.0
# Parallel Yahoo fetches; kept small so a ~100-ticker scan stays under Yahoo's rate limit
_SCAN_WORKERS: int = 6
# Neutral score; all bonuses add up to +70, so a perfect setup lands at exactly 100
_BASE_SCORE: int = 30

class MarketScanner:
    @staticmethod
    async def load_watchlist_tickers() -> List[str]:
        """Active watchlist tickers, to pass to scan_universe as extra_tickers."""
        from sqlalchemy import select
        from src.db.session import AsyncSessionLocal
        from src.db.models import WatchlistItem
        async with AsyncSessionLocal() as session:
            res = await session.execute(select(WatchlistItem.ticker).where(WatchlistItem.is_active.is_(True)))
            return [t for t in res.scalars().all() if t]

    @staticmethod
    def scan_universe(market: str = "ALL", limit: int = 10, extra_tickers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Scans liquid universes (EU, US, or ALL) and ranks candidates
        based on momentum, trend alignment, and technical pullback criteria.
        """
        extras = sorted({t.upper().strip() for t in (extra_tickers or []) if t and t.strip()})
        cache_key = f"{market.upper()}_{limit}_{','.join(extras)}"
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
        known = {i["ticker"].upper() for i in pool}
        for t in extras:
            if t in known:
                continue
            if market.upper() == "ALL" or (market.upper() == "EU") == is_eu_ticker(t):
                pool.append({"ticker": t})
                known.add(t)

        ranked_candidates: List[Dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=_SCAN_WORKERS) as ex:
            fetched = list(ex.map(lambda i: market_data_provider.get_stock_data(i["ticker"]), pool))

        for item, data in zip(pool, fetched):
            ticker = item["ticker"]
            if not data:
                continue

            chop = MarketScanner.chop_reason(data["technicals"])
            if chop:
                logger.debug(f"Scan skips {ticker}: {chop}")
                continue

            score, setup_type, notes = MarketScanner._evaluate_technical_setup(data)
            if score >= _BASE_SCORE:  # Only pass setups with no net penalty
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
    def chop_reason(t: Dict[str, Any]) -> Optional[str]:
        """Why this ticker is not worth a committee debate (ranging or dead flat), or None. Unknown values pass."""
        if not settings.CHOP_FILTER_ENABLED:
            return None
        adx, atr_pct = t.get("adx14"), t.get("atr_pct")
        if atr_pct is not None and atr_pct < settings.CHOP_MIN_ATR_PCT:
            return f"flat: ATR is {atr_pct}% of price (minimum {settings.CHOP_MIN_ATR_PCT}%)"
        if adx is not None and adx < settings.CHOP_ADX_MIN:
            return f"no trend: ADX {adx} (minimum {settings.CHOP_ADX_MIN})"
        return None

    @staticmethod
    def _evaluate_technical_setup(data: Dict[str, Any]) -> tuple[int, str, str]:
        t = data["technicals"]
        f = data["fundamentals"]

        score = _BASE_SCORE
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
