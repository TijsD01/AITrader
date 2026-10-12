import logging
import time
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
import yfinance as yf

logger = logging.getLogger(__name__)

# ponytail: in-memory TTL cache, Redis if multi-worker needed
_stock_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL_OPEN: float = 60.0
_CACHE_TTL_CLOSED: float = 1800.0

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
            if (now - cached_time) < _CACHE_TTL_OPEN:
                return cached_data

        try:
            ticker = yf.Ticker(ticker_symbol)
            # Fetch 6 months of daily data to calculate 200 SMA and indicators
            hist = ticker.history(period="6mo")
            if hist.empty or len(hist) < 20:
                logger.warning(f"Insufficient historical data for {ticker_symbol}")
                return None

            # Extract info/fast_info
            fast_info = ticker.fast_info
            current_price = float(fast_info.get("lastPrice") or hist["Close"].iloc[-1])
            currency = fast_info.get("currency", "USD")

            # Try to get comprehensive fundamental info
            info = {}
            try:
                info = ticker.info or {}
            except Exception as e:
                logger.debug(f"Could not load full ticker.info for {ticker_symbol}: {e}")

            # Calculate Technical Indicators
            closes = hist["Close"]
            highs = hist["High"]
            lows = hist["Low"]
            volumes = hist["Volume"]

            # Moving Averages
            ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
            ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
            sma200 = closes.rolling(window=min(len(closes), 200)).mean().iloc[-1]

            # RSI 14
            delta = closes.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi_series = 100 - (100 / (1 + rs))
            rsi14 = float(rsi_series.iloc[-1]) if not np.isnan(rsi_series.iloc[-1]) else 50.0

            # MACD (12, 26, 9)
            exp12 = closes.ewm(span=12, adjust=False).mean()
            exp26 = closes.ewm(span=26, adjust=False).mean()
            macd_line = exp12 - exp26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            macd_val = float(macd_line.iloc[-1])
            signal_val = float(signal_line.iloc[-1])
            macd_hist = macd_val - signal_val

            # ATR 14
            tr1 = highs - lows
            tr2 = (highs - closes.shift()).abs()
            tr3 = (lows - closes.shift()).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr14 = float(tr.rolling(14).mean().iloc[-1])
            if np.isnan(atr14) or atr14 <= 0:
                atr14 = current_price * 0.025  # 2.5% default estimate

            # Support & Resistance (20-day swing low/high)
            recent_20 = hist.tail(20)
            support_20d = float(recent_20["Low"].min())
            resistance_20d = float(recent_20["High"].max())

            # Volume comparison
            avg_vol_20 = float(volumes.tail(20).mean())
            last_vol = float(volumes.iloc[-1])
            volume_surge_ratio = round(last_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 1.0

            # Performance returns
            ret_1d = float(((closes.iloc[-1] / closes.iloc[-2]) - 1) * 100) if len(closes) > 1 else 0.0
            ret_5d = float(((closes.iloc[-1] / closes.iloc[-5]) - 1) * 100) if len(closes) > 5 else 0.0
            ret_1m = float(((closes.iloc[-1] / closes.iloc[-21]) - 1) * 100) if len(closes) > 21 else 0.0

            result = {
                "ticker": ticker_symbol,
                "name": info.get("shortName") or info.get("longName") or ticker_symbol,
                "currency": currency,
                "price": round(current_price, 2),
                "sector": info.get("sector", "General"),
                "industry": info.get("industry", "General"),
                "country": info.get("country", ""),
                
                # Technical Profile
                "technicals": {
                    "price": round(current_price, 2),
                    "ema20": round(float(ema20), 2),
                    "ema50": round(float(ema50), 2),
                    "sma200": round(float(sma200), 2),
                    "rsi14": round(rsi14, 2),
                    "macd": round(macd_val, 2),
                    "macd_signal": round(signal_val, 2),
                    "macd_hist": round(macd_hist, 2),
                    "atr14": round(atr14, 2),
                    "support_20d": round(support_20d, 2),
                    "resistance_20d": round(resistance_20d, 2),
                    "volume_surge_ratio": volume_surge_ratio,
                    "return_1d_pct": round(ret_1d, 2),
                    "return_5d_pct": round(ret_5d, 2),
                    "return_1m_pct": round(ret_1m, 2),
                    "above_ema20": bool(current_price > ema20),
                    "above_ema50": bool(current_price > ema50),
                    "above_sma200": bool(current_price > sma200),
                },

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
    def invalidate(ticker_symbols) -> None:
        """Drops cached quotes so the next get_stock_data call fetches fresh data."""
        for ticker_symbol in ticker_symbols:
            _stock_cache.pop(ticker_symbol.upper().strip(), None)

market_data_provider = MarketDataProvider()
