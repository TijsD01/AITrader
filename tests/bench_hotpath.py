"""
Hot-path latency benchmark. Offline: Yahoo is replaced by a synthetic 252-bar history.

    python tests/bench_hotpath.py            # print results
    python tests/bench_hotpath.py out.json   # also save them

Not collected by pytest (no test_ prefix). Numbers are medians; compare runs on the same machine only.
"""
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_tmp = tempfile.mkdtemp(prefix="bench_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/bench.db"

import numpy as np
import pandas as pd

from config.settings import settings, Settings

for _name, _field in Settings.model_fields.items():  # code defaults, not the owner's .env
    if not _field.is_required():
        setattr(settings, _name, _field.get_default(call_default_factory=True))
settings.DATABASE_URL = os.environ["DATABASE_URL"]
settings.ENFORCE_MARKET_HOURS = False  # the result must not depend on the wall clock
settings.REGIME_FILTER_ENABLED = False

import src.data.market as market
from src.screener.scanner import MarketScanner
from src.trading import risk


def _history(n: int = 252) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    high = close * (1 + rng.uniform(0, 0.01, n))
    low = close * (1 - rng.uniform(0, 0.01, n))
    return pd.DataFrame(
        {"Open": close, "High": high, "Low": low, "Close": close, "Volume": rng.integers(1e5, 1e6, n).astype(float)},
        index=pd.date_range("2025-01-01", periods=n, freq="B"),
    )


class _FakeTicker:
    hist = _history()
    fast_info = {"lastPrice": float(hist["Close"].iloc[-1]), "currency": "EUR"}
    info = {"shortName": "Bench NV", "sector": "Tech", "quoteType": "EQUITY"}

    def __init__(self, symbol):
        pass

    def history(self, **kwargs):
        return self.hist


def _median_us(fn, n: int) -> float:
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    return round(statistics.median(samples), 1)


def bench_signal() -> dict:
    """(a) 252 daily bars -> indicators -> scanner pre-score."""
    market.yf.Ticker = _FakeTicker
    market.quotes_moving = lambda sym: False

    def run():
        market._stock_cache.clear()
        data = market.MarketDataProvider.get_stock_data("BENCH.AS")
        MarketScanner._evaluate_technical_setup(data)

    run()
    us = _median_us(run, 300)
    tracemalloc.start()
    run()
    peak_kb = round(tracemalloc.get_traced_memory()[1] / 1024, 1)
    tracemalloc.stop()
    return {"bars_to_signal_us": us, "bars_to_signal_peak_kb": peak_kb}


def bench_risk() -> dict:
    """(b) buy guardrails + sizing plan + broker order payload."""
    def run():
        ok, _ = risk.validate_buy_order(
            ticker="BENCH.AS", current_positions=[], cash_eur=5000.0, portfolio_total_eur=10000.0,
            allocation_eur=1250.0, consensus_score=85, auto_buy_enabled=True,
            price_native=101.37, currency="EUR", stop_loss_native=97.3, take_profit_native=109.5,
        )
        plan = risk.plan_buy(1250.0, 101.37, "EUR", 97.3, 10000.0, max_eur=5000.0)
        json.dumps({"ticker": "BENCH_EQ", "quantity": plan["shares"], "limitPrice": 101.37, "timeValidity": "DAY"})
        assert ok

    run()
    payload = {"ticker": "BENCH_EQ", "quantity": 12.3312, "limitPrice": 101.37, "timeValidity": "DAY"}
    return {
        "risk_check_plus_payload_us": _median_us(run, 2000),
        "order_payload_json_dumps_us": _median_us(lambda: json.dumps(payload), 20000),
    }


def bench_db() -> dict:
    """(c) one state write (commit) and one state read on a fresh SQLite file."""
    from sqlalchemy import select
    import src.db.session as db
    from src.db.models import Base, SystemConfig

    async def run():
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        writes, reads = [], []
        for i in range(200):
            t0 = time.perf_counter()
            async with db.AsyncSessionLocal() as s:
                await s.merge(SystemConfig(key="BENCH", value=str(i)))
                await s.commit()
            t1 = time.perf_counter()
            async with db.AsyncSessionLocal() as s:
                (await s.execute(select(SystemConfig).where(SystemConfig.key == "BENCH"))).scalars().first()
            t2 = time.perf_counter()
            writes.append((t1 - t0) * 1e6)
            reads.append((t2 - t1) * 1e6)
        await db.engine.dispose()
        return {"db_write_us": round(statistics.median(writes), 1), "db_read_us": round(statistics.median(reads), 1)}

    return asyncio.run(run())


if __name__ == "__main__":
    results = {**bench_signal(), **bench_risk(), **bench_db()}
    print(json.dumps(results, indent=2))
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps(results, indent=2) + "\n")
