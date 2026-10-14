import time
import logging
from typing import Dict, Optional
import yfinance as yf

logger = logging.getLogger(__name__)

# Currencies whose EUR rate is fetched via yfinance "EUR<CCY>=X" (units of CCY per 1 EUR).
FETCHABLE_CURRENCIES = ("USD", "GBP", "CHF", "SEK", "NOK", "DKK")
# Quote codes that denote minor units (pence) of the main currency, matched case-sensitively.
PENCE_CODES = ("GBp", "GBX")

class CurrencyService:
    def __init__(self):
        self._rates_cache: Dict[str, float] = {
            "EUR": 1.0,
            "USD": 1.08,  # 1 EUR = 1.08 USD
            "GBP": 0.85,  # 1 EUR = 0.85 GBP
            "CHF": 0.95,  # rough fallbacks, replaced on first successful fetch
            "SEK": 11.5,
            "NOK": 11.7,
            "DKK": 7.46,
        }
        self._last_update: float = 0.0
        self._cache_ttl: float = 900.0  # 15 minutes
        self._fail_backoff: float = 300.0  # wait before retrying after a failed fetch
        # Per-currency time before which no new network fetch is attempted
        self._next_attempt: Dict[str, float] = {}
        self._warned_unknown = set()

    def _refresh_rate(self, code: str, now: float):
        if now < self._next_attempt.get(code, 0.0):
            return
        try:
            # yfinance EUR<CCY>=X gives the rate for 1 EUR in <CCY>
            info = yf.Ticker(f"EUR{code}=X").fast_info
            rate = info.get("lastPrice") or info.get("previousClose")
            if rate and rate > 0:
                self._rates_cache[code] = float(rate)
                self._next_attempt[code] = now + self._cache_ttl
                if code == "USD":
                    self._last_update = now
                logger.info(f"Updated EUR/{code} rate: 1 EUR = {float(rate):.4f} {code}")
                return
            raise ValueError("no rate returned")
        except Exception as e:
            # Back off so a failing network call is not retried on every to_eur call
            self._next_attempt[code] = now + self._fail_backoff
            if code == "USD":
                self._last_update = now
            logger.warning(f"Could not refresh FX rate EUR{code}=X: {e}. Using cached: {self._rates_cache.get(code)}")

    def update_rates(self, currency: Optional[str] = None):
        now = time.time()
        codes = ["USD"]
        if currency and currency in FETCHABLE_CURRENCIES and currency not in codes:
            codes.append(currency)
        for code in codes:
            self._refresh_rate(code, now)

    def to_eur(self, amount: float, currency: str) -> float:
        raw = (currency or "").strip()
        # Case matters: "GBp" is pence while "GBP" is pounds, so detect pence before uppercasing.
        is_pence = raw in PENCE_CODES
        curr = "GBP" if is_pence else raw.upper()
        if curr == "US$":
            curr = "USD"

        if curr == "EUR":
            return amount
        self.update_rates(curr)
        if curr in FETCHABLE_CURRENCIES:
            adj_amount = amount / 100.0 if is_pence else amount
            return adj_amount / self._rates_cache[curr]
        # Default fallback (unknown currency): treat as 1:1
        if curr not in self._warned_unknown:
            self._warned_unknown.add(curr)
            logger.warning(f"No FX rate for currency '{currency}'; assuming 1:1 with EUR.")
        return amount

currency_service = CurrencyService()
