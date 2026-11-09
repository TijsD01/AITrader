import logging
import math
import time
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
import yfinance as yf

from src.data.market_hours import check_market_hours, quotes_moving

logger = logging.getLogger(__name__)

# ponytail: in-memory TTL cache, Redis if multi-worker needed
_stock_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_OPEN: float = 60.0
_CACHE_TTL_CLOSED: float = 1800.0
_regime_cache: Dict[str, tuple[float, Optional[Dict[str, Any]]]] = {}
_REGIME_CACHE_TTL: float = 3600.0
# ticker.info (fundamentals) is the slowest Yahoo call and the first to hit 429; it barely moves intraday
_info_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_INFO_CACHE_TTL: float = 24 * 3600.0
# shortcut: never evicted (one entry per ticker+range viewed), add LRU if memory grows
_history_cache: Dict[tuple, tuple[float, list]] = {}

# range key -> yfinance history() kwargs for the ticker detail chart
_HISTORY_RANGES: Dict[str, Dict[str, Any]] = {
    "1D": {"period": "1d", "interval": "5m", "prepost": True},
    "1M": {"period": "1mo", "interval": "1d"},
    "3M": {"period": "3mo", "interval": "1d"},
    "1Y": {"period": "1y", "interval": "1d"},
}

def _ema_last(x: np.ndarray, span: int) -> float:
    """Last value of pandas' ewm(span, adjust=False).mean(), as one dot product."""
    alpha = 2.0 / (span + 1)
    n = len(x) - 1
    w = alpha * (1 - alpha) ** np.arange(n, -1, -1.0)
    w[0] = (1 - alpha) ** n
    return float(w @ x)


def compute_technicals(hist: pd.DataFrame, current_price: float) -> Dict[str, Any]:
    """
    Technical profile from daily bars (oldest first, 20 or more). Only the last value of each indicator
    is needed, so most of them read the tail of the NumPy columns instead of building a full series.
    """
    c = hist["Close"].to_numpy(dtype=float)
    h = hist["High"].to_numpy(dtype=float)
    l = hist["Low"].to_numpy(dtype=float)
    v = np.nan_to_num(hist["Volume"].to_numpy(dtype=float))
    ok = np.isfinite(c) & np.isfinite(h) & np.isfinite(l)
    if not ok.all():  # Yahoo sometimes returns an empty row for a day without trades
        c, h, l, v = c[ok], h[ok], l[ok], v[ok]
    if len(c) < 20:
        raise ValueError(f"only {len(c)} usable bars")

    ema20, ema50 = _ema_last(c, 20), _ema_last(c, 50)
    sma200 = float(c[-200:].mean())

    # RSI 14 (simple average of the last 14 changes)
    delta = np.diff(c[-15:])
    gain, loss = float(np.clip(delta, 0, None).mean()), float(np.clip(-delta, 0, None).mean())
    if gain == 0 and loss == 0:
        rsi14 = 50.0
    elif loss == 0:
        rsi14 = 100.0  # no down days in the window
    else:
        rsi14 = 100 - (100 / (1 + (gain / loss)))

    # MACD (12, 26, 9): the signal line needs the whole MACD series
    closes = pd.Series(c)
    macd_line = (closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()).to_numpy()
    macd_val, signal_val = float(macd_line[-1]), _ema_last(macd_line, 9)

    # True range, +DM and -DM per bar (from the second bar on)
    prev_c = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)))
    atr14 = float(tr[-14:].mean())
    if not atr14 > 0:
        atr14 = current_price * 0.025  # 2.5% default estimate

    # ADX 14 (Wilder): trend strength whatever the direction; below ~20 the price is ranging
    up, down = h[1:] - h[:-1], l[:-1] - l[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    sm = pd.DataFrame({"tr": tr, "p": plus_dm, "m": minus_dm}).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    di_sum, di_diff = sm[:, 1] + sm[:, 2], np.abs(sm[:, 1] - sm[:, 2])
    dx = np.divide(100.0 * di_diff, di_sum, out=np.zeros_like(di_sum), where=di_sum > 0)
    adx14 = float(pd.Series(dx).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])

    # 52-week high (George & Hwang 2004: nearness to it predicts returns better than past return)
    high_52w = float(h.max())
    pct_below_52w_high = ((high_52w - current_price) / high_52w) * 100 if high_52w > 0 else 0.0

    avg_vol_20 = float(v[-20:].mean())

    def ret(bars: int) -> float:
        return float((c[-1] / c[-1 - bars] - 1) * 100) if len(c) > bars + 1 else 0.0

    return {
        "price": round(current_price, 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "sma200": round(sma200, 2),
        "rsi14": round(rsi14, 2),
        "macd": round(macd_val, 2),
        "macd_signal": round(signal_val, 2),
        "macd_hist": round(macd_val - signal_val, 2),
        "atr14": round(atr14, 2),
        "atr_pct": round(atr14 / current_price * 100, 2),
        "adx14": round(adx14, 2),
        # Support & Resistance (20-day swing low/high)
        "support_20d": round(float(l[-20:].min()), 2),
        "resistance_20d": round(float(h[-20:].max()), 2),
        "high_52w": round(high_52w, 2),
        "pct_below_52w_high": round(max(0.0, pct_below_52w_high), 2),
        "volume_surge_ratio": round(float(v[-1]) / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0,
        "return_1d_pct": round(float((c[-1] / c[-2] - 1) * 100), 2),
        "return_5d_pct": round(ret(5), 2),
        "return_1m_pct": round(ret(21), 2),
        "above_ema20": bool(current_price > ema20),
        "above_ema50": bool(current_price > ema50),
        "above_sma200": bool(current_price > sma200),
    }


class MarketDataProvider:
    @staticmethod
    def get_stock_data(ticker_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Fetches historical price series and fundamental metrics from yfinance.
        Calculates key technical indicators (EMA, RSI, MACD, ATR).
        """
        sym = ticker_symbol.upper().strip()
        now = time.time()
        if sym in _stock_cache:
            cached_time, cached_data = _stock_cache[sym]
            # Judged now, not when cached: a quote cached while closed goes stale the moment the market opens
            if (now - cached_time) < (_CACHE_TTL_OPEN if quotes_moving(sym) else _CACHE_TTL_CLOSED):
                return cached_data

        try:
            ticker = yf.Ticker(ticker_symbol)
            # Fetch 1 year of daily data (~250 bars) so the 200-day SMA is a real 200-day SMA
            hist = ticker.history(period="1y")
            if hist.empty or len(hist) < 20:
                logger.warning(f"Insufficient historical data for {ticker_symbol}")
                return None

            # Extract info/fast_info
            fast_info = ticker.fast_info
            current_price = float(fast_info.get("lastPrice") or hist["Close"].iloc[-1])
            # lastPrice is the regular-session close; Trading 212 trades on in pre/after-market, so quote that
            if quotes_moving(sym) and not check_market_hours(sym)[0]:
                try:
                    ext = ticker.history(period="1d", interval="1m", prepost=True)["Close"].dropna()
                    if not ext.empty and ext.iloc[-1] > 0:
                        current_price = float(ext.iloc[-1])
                except Exception as e:
                    logger.debug(f"No extended-hours quote for {ticker_symbol}: {e}")
            if not math.isfinite(current_price) or current_price <= 0:
                # NaN compares False with every stop and target, so it must not reach the brokers as a price
                logger.warning(f"Unusable quote for {ticker_symbol}: {current_price}")
                return None
            currency = fast_info.get("currency", "USD")

            # Try to get comprehensive fundamental info
            cached_info = _info_cache.get(sym)
            if cached_info and (now - cached_info[0]) < _INFO_CACHE_TTL:
                info = cached_info[1]
            else:
                info = {}
                try:
                    info = ticker.info or {}
                except Exception as e:
                    logger.debug(f"Could not load full ticker.info for {ticker_symbol}: {e}")
                if info:
                    _info_cache[sym] = (now, info)

            technicals = compute_technicals(hist, current_price)

            result = {
                "ticker": ticker_symbol,
                "name": info.get("shortName") or info.get("longName") or ticker_symbol,
                "currency": currency,
                "price": round(current_price, 2),
                "sector": info.get("sector", "General"),
                "industry": info.get("industry", "General"),
                "country": info.get("country", ""),
                "quote_type": info.get("quoteType"),  # EQUITY, ETF, INDEX, CURRENCY, ...

                # Technical Profile
                "technicals": technicals,

                # Fundamental Profile
                "fundamentals": {
                    "market_cap": info.get("marketCap"),
                    "trailing_pe": info.get("trailingPE"),
                    "forward_pe": info.get("forwardPE"),
                    "peg_ratio": info.get("pegRatio"),
                    "price_to_book": info.get("priceToBook"),
                    "profit_margins": info.get("profitMargins"),
                    "operating_margins": info.get("operatingMargins"),
                    "return_on_equity": info.get("returnOnEquity"),
                    "revenue_growth": info.get("revenueGrowth"),
                    "earnings_growth": info.get("earningsGrowth"),
                    "debt_to_equity": info.get("debtToEquity"),
                    "dividend_yield": info.get("dividendYield"),
                    "beta": info.get("beta", 1.0),
                    "target_mean_price": info.get("targetMeanPrice"),
                    "recommendation_key": info.get("recommendationKey"),
                }
            }
            _stock_cache[sym] = (time.time(), result)
            return result
        except Exception as e:
            logger.error(f"Error compiling market data for {ticker_symbol}: {e}")
            return None

    @staticmethod
    def get_index_regime(index_symbol: str, sma_days: int = 200) -> Optional[Dict[str, Any]]:
        """
        Returns the index's last close and its sma_days moving average, cached for an hour.
        None when the history is unavailable or too short.
        """
        key = f"{index_symbol.upper()}:{sma_days}"
        now = time.time()
        cached = _regime_cache.get(key)
        if cached and (now - cached[0]) < _REGIME_CACHE_TTL:
            return cached[1]
        result = None
        try:
            period = "2y" if sma_days > 200 else "1y"
            closes = yf.Ticker(index_symbol).history(period=period)["Close"].dropna()
            if len(closes) >= sma_days:
                last = float(closes.iloc[-1])
                sma = float(closes.tail(sma_days).mean())
                result = {
                    "index": index_symbol,
                    "price": round(last, 2),
                    "sma": round(sma, 2),
                    "sma_days": sma_days,
                    "above_sma": last >= sma,
                }
            else:
                logger.warning(f"Not enough history for {index_symbol} to compute a {sma_days}-day average.")
        except Exception as e:
            logger.error(f"Error fetching regime data for {index_symbol}: {e}")
        # A failed fetch is retried after 10 minutes instead of on every trigger check
        _regime_cache[key] = (now if result is not None else now - _REGIME_CACHE_TTL + 600.0, result)
        return result

    @staticmethod
    def get_price_history(ticker_symbol: str, range_key: str = "3M") -> Optional[list]:
        """Close prices for the chart as [[iso_time, close], ...], or None on failure. Unknown range means 3M."""
        sym = ticker_symbol.upper().strip()
        range_key = range_key if range_key in _HISTORY_RANGES else "3M"
        key = (sym, range_key)
        now = time.time()
        ttl = _CACHE_TTL_OPEN if quotes_moving(sym) else _CACHE_TTL_CLOSED
        cached = _history_cache.get(key)
        if cached and (now - cached[0]) < ttl:
            return cached[1]
        try:
            closes = yf.Ticker(sym).history(**_HISTORY_RANGES[range_key])["Close"].dropna()
            if closes.index.tz is not None:  # naive UTC, like the timestamps stored in the DB
                closes.index = closes.index.tz_convert("UTC").tz_localize(None)
            points = [[ts.isoformat(timespec="minutes"), round(float(v), 2)] for ts, v in closes.items()]
        except Exception as e:
            logger.error(f"Error fetching price history for {sym} ({range_key}): {e}")
            return None
        if not points:
            return None
        _history_cache[key] = (now, points)
        return points

    @staticmethod
    def invalidate(ticker_symbols) -> None:
        """Drops cached quotes so the next get_stock_data call fetches fresh data."""
        for ticker_symbol in ticker_symbols:
            _stock_cache.pop(ticker_symbol.upper().strip(), None)

market_data_provider = MarketDataProvider()
