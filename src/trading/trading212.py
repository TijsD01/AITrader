"""
Minimal async client for the Trading 212 Public API v0 (equity endpoints only).
Auth is HTTP Basic (API key, API secret). Order POSTs are NOT idempotent, so they are never retried here.
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

DEMO_URL = "https://demo.trading212.com/api/v0"
LIVE_URL = "https://live.trading212.com/api/v0"
INSTRUMENTS_TTL = 6 * 3600.0   # instrument list changes rarely; the endpoint allows 1 req/50s
SUMMARY_TTL = 5.0              # account summary allows 1 req/5s
MAX_RATE_WAIT = 10.0

# Yahoo suffix -> substrings of the T212 exchange name (lower case). "" is a US listing.
EXCHANGE_HINTS = {
    "": ("nyse", "nasdaq"),
    "AS": ("amsterdam",),
    "DE": ("xetra", "deutsche"),
    "F": ("frankfurt",),
    "PA": ("paris",),
    "BR": ("brussels",),
    "LS": ("lisbon",),
    "MI": ("milan", "borsa italiana"),
    "MC": ("madrid", "bme"),
    "L": ("london", "lse"),
    "SW": ("six", "swiss"),
}


class Trading212Error(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"Trading 212 HTTP {status}: {text}")
        self.status = status


def _ccy(code: Optional[str]) -> str:
    """Pence codes ("GBp" from Yahoo, "GBX" from T212) compare equal; others case-insensitively."""
    code = (code or "").strip()
    return "GBX" if code in ("GBp", "GBX") else code.upper()


def _suffix_for(exchange: str) -> Optional[str]:
    name = (exchange or "").lower()
    return next((s for s, hints in EXCHANGE_HINTS.items() if any(h in name for h in hints)), None)


class Trading212Client:
    def __init__(self, key: str, secret: str, demo: bool = True, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._http = httpx.AsyncClient(
            base_url=DEMO_URL if demo else LIVE_URL, auth=(key, secret), timeout=10, transport=transport
        )
        self._instruments: Optional[List[Dict[str, Any]]] = None
        self._instruments_at = 0.0
        self._instruments_lock = asyncio.Lock()
        self._summary: Optional[Dict[str, Any]] = None
        self._summary_at = 0.0

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(3):
            r = await self._http.request(method, path, **kwargs)
            # Only reads are retried: a repeated order POST would place a second order
            if r.status_code == 429 and method == "GET" and attempt < 2:
                reset = float(r.headers.get("x-ratelimit-reset") or 0)
                await asyncio.sleep(min(max(reset - time.time(), 1.0), MAX_RATE_WAIT))
                continue
            break
        if r.status_code >= 400:
            raise Trading212Error(r.status_code, r.text[:200])
        return r.json() if r.content else None

    async def account_summary(self) -> Dict[str, Any]:
        if self._summary is None or time.monotonic() - self._summary_at > SUMMARY_TTL:
            self._summary = await self._request("GET", "/equity/account/summary")
            self._summary_at = time.monotonic()
        return self._summary

    async def positions(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        return await self._request("GET", "/equity/positions", params={"ticker": ticker} if ticker else None) or []

    async def instruments(self) -> List[Dict[str, Any]]:
        """Tradable instruments, each with an added "exchange" name, cached for INSTRUMENTS_TTL."""
        if self._instruments is not None and time.monotonic() - self._instruments_at <= INSTRUMENTS_TTL:
            return self._instruments
        async with self._instruments_lock:
            # Another caller may have refreshed while this one waited (the endpoint allows 1 req/50s)
            if self._instruments is not None and time.monotonic() - self._instruments_at <= INSTRUMENTS_TTL:
                return self._instruments
            try:
                instruments = await self._request("GET", "/equity/metadata/instruments") or []
                exchanges = await self._request("GET", "/equity/metadata/exchanges") or []
            except Exception as e:
                if self._instruments is None:
                    raise
                logger.warning(f"Trading 212 instrument refresh failed, using the cached list: {e}")
                self._instruments_at = time.monotonic() - INSTRUMENTS_TTL + 300  # try again in 5 minutes
                return self._instruments
            schedule_names = {ws["id"]: ex.get("name", "") for ex in exchanges for ws in ex.get("workingSchedules", [])}
            for i in instruments:
                i["exchange"] = schedule_names.get(i.get("workingScheduleId"), "")
            self._instruments, self._instruments_at = instruments, time.monotonic()
        return self._instruments

    async def resolve_ticker(self, yahoo_ticker: str, currency: str) -> Optional[str]:
        """Maps a Yahoo ticker (ASML.AS, AAPL) to one T212 ticker, or None when nothing or several instruments fit."""
        base, _, suffix = yahoo_ticker.upper().partition(".")
        found = [
            i for i in await self.instruments()
            if i.get("type") in ("STOCK", "ETF") and (i.get("shortName") or "").upper() == base
            and _ccy(i.get("currencyCode")) == _ccy(currency)
        ]
        # Always require the exchange to fit the Yahoo suffix: a lone symbol match on another venue may be another company
        hints = EXCHANGE_HINTS.get(suffix, ())
        found = [i for i in found if any(h in i["exchange"].lower() for h in hints)]
        if len(found) != 1:
            logger.error(f"Trading 212: {len(found)} instruments match {yahoo_ticker} ({currency}); no unique ticker.")
            return None
        return found[0]["ticker"]

    async def yahoo_ticker(self, t212_ticker: str) -> str:
        """Reverse map for adopting broker holdings: shortName plus the Yahoo suffix of its exchange."""
        inst = next((i for i in await self.instruments() if i.get("ticker") == t212_ticker), None)
        suffix = _suffix_for(inst["exchange"]) if inst else None
        if not inst or suffix is None:
            logger.warning(f"Trading 212: no Yahoo ticker known for {t212_ticker}; keeping the T212 ticker.")
            return t212_ticker
        return inst["shortName"].upper() + (f".{suffix}" if suffix else "")

    async def place_limit(self, ticker: str, quantity: float, limit_price: float, validity: str = "DAY") -> Dict[str, Any]:
        return await self._request("POST", "/equity/orders/limit", json={
            "ticker": ticker, "quantity": quantity, "limitPrice": limit_price, "timeValidity": validity})

    async def place_market(self, ticker: str, quantity: float) -> Dict[str, Any]:
        return await self._request("POST", "/equity/orders/market", json={
            "ticker": ticker, "quantity": quantity, "extendedHours": False})

    async def place_stop(self, ticker: str, quantity: float, stop_price: float, validity: str = "GOOD_TILL_CANCEL") -> Dict[str, Any]:
        return await self._request("POST", "/equity/orders/stop", json={
            "ticker": ticker, "quantity": quantity, "stopPrice": stop_price, "timeValidity": validity})

    async def get_order(self, order_id: int) -> Optional[Dict[str, Any]]:
        """A pending order, or None once it is no longer pending (filled, cancelled, rejected)."""
        try:
            return await self._request("GET", f"/equity/orders/{order_id}")
        except Trading212Error as e:
            if e.status == 404:
                return None
            raise

    async def pending_orders(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/equity/orders") or []

    async def cancel_order(self, order_id: int) -> None:
        try:
            await self._request("DELETE", f"/equity/orders/{order_id}")
        except Trading212Error as e:
            if e.status != 404:  # 404: no longer pending, nothing to cancel
                raise

    async def history_orders(self, ticker: str, limit: int = 50) -> List[Dict[str, Any]]:
        res = await self._request("GET", "/equity/history/orders", params={"ticker": ticker, "limit": limit})
        return (res or {}).get("items", [])


_client: Optional[Trading212Client] = None
_client_key: Optional[tuple] = None


def get_client() -> Optional[Trading212Client]:
    """Shared client built from settings; None when the API key or secret is missing."""
    global _client, _client_key
    if not (settings.TRADING212_API_KEY and settings.TRADING212_API_SECRET):
        return None
    key = (settings.TRADING212_API_KEY, settings.TRADING212_API_SECRET, bool(settings.TRADING212_DEMO))
    if _client is None or _client_key != key:
        _client, _client_key = Trading212Client(*key), key
    return _client
