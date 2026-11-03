"""
Checks that every ticker in the default universe maps to exactly one Trading 212 instrument.

Read-only: it only calls the instrument metadata endpoints, never places orders.
Uses TRADING212_API_KEY / TRADING212_API_SECRET / TRADING212_DEMO from settings (.env).

    python scripts/check_t212_universe.py

Exit code 1 when any ticker does not resolve, so a failing ticker can be removed before it reaches a buy.
"""
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.default_universe import EU_STOCKS, US_STOCKS  # noqa: E402

# Listing currency per Yahoo suffix; "" is a US listing
SUFFIX_CURRENCY = {"": "USD", "AS": "EUR", "DE": "EUR", "F": "EUR", "PA": "EUR", "BR": "EUR", "LS": "EUR",
                   "MI": "EUR", "MC": "EUR", "L": "GBp", "SW": "CHF"}


async def check_universe(client, tickers: List[str]) -> List[Dict[str, Any]]:
    """One row per ticker: the T212 ticker (None when no unique match) and its minimum trade quantity."""
    by_ticker = {i.get("ticker"): i for i in await client.instruments()}
    rows = []
    for yahoo in tickers:
        suffix = yahoo.upper().partition(".")[2]
        currency = SUFFIX_CURRENCY.get(suffix)
        t212: Optional[str] = await client.resolve_ticker(yahoo, currency) if currency else None
        inst = by_ticker.get(t212, {})
        rows.append({"ticker": yahoo, "t212": t212, "min_qty": inst.get("minTradeQuantity")})
    return rows


async def main() -> int:
    from src.trading.trading212 import get_client
    client = get_client()
    if client is None:
        print("TRADING212_API_KEY / TRADING212_API_SECRET missing in .env")
        return 2
    rows = await check_universe(client, [s["ticker"] for s in EU_STOCKS + US_STOCKS])
    for r in rows:
        status = "OK  " if r["t212"] else "MISS"
        print(f"{status} {r['ticker']:<10} {r['t212'] or '-':<14} min_qty={r['min_qty']}")
    missing = [r["ticker"] for r in rows if not r["t212"]]
    print(f"\n{len(rows) - len(missing)}/{len(rows)} resolved." + (f" Missing: {', '.join(missing)}" if missing else ""))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
