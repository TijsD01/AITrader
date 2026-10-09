import time
import logging
from typing import Dict
import yfinance as yf

logger = logging.getLogger(__name__)

class CurrencyService:
    def __init__(self):
        self._rates_cache: Dict[str, float] = {
            "EUR": 1.0,
            "USD": 1.08, # 1 EUR = 1.08 USD
            "GBP": 0.85, # 1 EUR = 0.85 GBP
        }
        self._last_update: float = 0.0
        self._cache_ttl: float = 900.0  # 15 minutes

    def update_rates(self):
        now = time.time()
        if now - self._last_update < self._cache_ttl:
            return

        try:
            # yfinance EURUSD=X gives rate for 1 EUR in USD
            ticker = yf.Ticker("EURUSD=X")
            info = ticker.fast_info
            rate = info.get("lastPrice") or info.get("previousClose")
            if rate and rate > 0:
                self._rates_cache["USD"] = float(rate)
                self._last_update = now
                logger.info(f"Updated EUR/USD rate: 1 EUR = {rate:.4f} USD")
        except Exception as e:
            logger.warning(f"Could not refresh FX rate EURUSD=X: {e}. Using cached: {self._rates_cache['USD']}")

    def to_eur(self, amount: float, currency: str) -> float:
        self.update_rates()
        curr = currency.upper()
        if curr == "EUR":
            return amount
        if curr in ("USD", "US$"):
            rate = self._rates_cache.get("USD", 1.08)
            return amount / rate
        if curr in ("GBP", "GBp"):
            # GBp is pence
            adj_amount = amount / 100.0 if curr == "GBp" else amount
            rate = self._rates_cache.get("GBP", 0.85)
            return adj_amount / rate
        # Default fallback
        return amount

currency_service = CurrencyService()
