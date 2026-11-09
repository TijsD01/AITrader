"""Indicator maths (offline): the NumPy version must equal the pandas formulas it replaced."""
import time

import numpy as np
import pandas as pd
import pytest

import src.data.market as market
from src.data.market import compute_technicals


def _history(n=252, drift=0.0005, vol=0.015, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    return pd.DataFrame({
        "High": close * (1 + rng.uniform(0, 0.01, n)),
        "Low": close * (1 - rng.uniform(0, 0.01, n)),
        "Close": close,
        "Volume": rng.integers(1e5, 1e6, n).astype(float),
    })


def _pandas_reference(hist, price):
    """The formulas get_stock_data used before the rewrite."""
    closes, highs, lows = hist["Close"], hist["High"], hist["Low"]
    delta = closes.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean().iloc[-1]
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean().iloc[-1]
    macd_line = closes.ewm(span=12, adjust=False).mean() - closes.ewm(span=26, adjust=False).mean()
    tr = pd.concat([highs - lows, (highs - closes.shift()).abs(), (lows - closes.shift()).abs()], axis=1).max(axis=1)
    return {
        "ema20": round(float(closes.ewm(span=20, adjust=False).mean().iloc[-1]), 2),
        "ema50": round(float(closes.ewm(span=50, adjust=False).mean().iloc[-1]), 2),
        "sma200": round(float(closes.rolling(min(len(closes), 200)).mean().iloc[-1]), 2),
        "rsi14": round(float(100 - 100 / (1 + gain / loss)), 2),
        "macd": round(float(macd_line.iloc[-1]), 2),
        "macd_signal": round(float(macd_line.ewm(span=9, adjust=False).mean().iloc[-1]), 2),
        "atr14": round(float(tr.rolling(14).mean().iloc[-1]), 2),
        "support_20d": round(float(lows.tail(20).min()), 2),
        "resistance_20d": round(float(highs.tail(20).max()), 2),
        "high_52w": round(float(highs.max()), 2),
        "volume_surge_ratio": round(float(hist["Volume"].iloc[-1] / hist["Volume"].tail(20).mean()), 2),
        "return_5d_pct": round(float((closes.iloc[-1] / closes.iloc[-6] - 1) * 100), 2),
        "return_1m_pct": round(float((closes.iloc[-1] / closes.iloc[-22] - 1) * 100), 2),
    }


@pytest.mark.parametrize("n,seed", [(252, 7), (252, 11), (120, 3), (30, 5)])
def test_numpy_indicators_equal_the_pandas_formulas(n, seed):
    hist = _history(n=n, seed=seed)
    price = float(hist["Close"].iloc[-1])
    got = compute_technicals(hist, price)
    for key, want in _pandas_reference(hist, price).items():
        assert got[key] == pytest.approx(want, abs=0.011), key


def test_adx_separates_a_trend_from_chop():
    trend = compute_technicals(_history(drift=0.01, vol=0.004), 1.0)
    rng = np.random.default_rng(3)
    close = 100 + rng.normal(0, 0.5, 252)  # noise around a flat level: no memory, so no trend
    chop = compute_technicals(pd.DataFrame({"High": close + 0.3, "Low": close - 0.3, "Close": close, "Volume": 1e5}), 100.0)
    assert 0 <= chop["adx14"] < 20 < trend["adx14"] <= 100


def test_atr_pct_is_atr_over_price():
    hist = _history()
    t = compute_technicals(hist, 50.0)
    assert t["atr_pct"] == pytest.approx(t["atr14"] / 50.0 * 100, abs=0.01)


def test_flat_series_has_neutral_rsi_and_zero_adx():
    flat = pd.DataFrame({"High": [10.0] * 60, "Low": [10.0] * 60, "Close": [10.0] * 60, "Volume": [0.0] * 60})
    t = compute_technicals(flat, 10.0)
    assert t["rsi14"] == 50.0 and t["adx14"] == 0.0 and t["volume_surge_ratio"] == 1.0
    assert t["atr14"] == 0.25  # no range: the 2.5% default estimate, never zero


class _Ticker:
    last_price = 100.0
    calls = 0

    def __init__(self, symbol):
        pass

    @property
    def fast_info(self):
        return {"lastPrice": type(self).last_price, "currency": "EUR"}

    info = {"shortName": "Fake"}

    def history(self, **kwargs):
        type(self).calls += 1
        return _history().assign(Open=1.0)


@pytest.fixture
def fake_yahoo(monkeypatch):
    monkeypatch.setattr(market.yf, "Ticker", _Ticker)
    monkeypatch.setattr(_Ticker, "calls", 0)
    monkeypatch.setattr(_Ticker, "last_price", 100.0)
    monkeypatch.setattr(market, "check_market_hours", lambda sym: (True, ""))
    market._stock_cache.pop("FAKE.AS", None)
    yield _Ticker
    market._stock_cache.pop("FAKE.AS", None)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_quote_gives_no_data(fake_yahoo, bad):
    # A NaN price would sit above no stop and below no target, so the position monitor would do nothing, silently
    fake_yahoo.last_price = bad
    assert market.market_data_provider.get_stock_data("FAKE.AS") is None


def test_quote_cache_lives_longer_while_quotes_stand_still(fake_yahoo, monkeypatch):
    moving = {"now": False}
    monkeypatch.setattr(market, "quotes_moving", lambda sym: moving["now"])
    assert market.market_data_provider.get_stock_data("FAKE.AS")["price"] == 100.0
    cached_at, data = market._stock_cache["FAKE.AS"]
    market._stock_cache["FAKE.AS"] = (time.time() - 600, data)  # 10 minutes old
    market.market_data_provider.get_stock_data("FAKE.AS")
    assert fake_yahoo.calls == 1, "market closed: no refetch within 30 minutes"
    moving["now"] = True  # the market opens: the same entry is now stale
    market.market_data_provider.get_stock_data("FAKE.AS")
    assert fake_yahoo.calls == 2


def _scan_with(monkeypatch, technicals_by_ticker):
    from src.screener import scanner as sc
    base = {"above_ema50": True, "above_sma200": True, "above_ema20": True, "rsi14": 55, "macd": 1, "macd_signal": 0,
            "macd_hist": 1, "volume_surge_ratio": 1.0, "return_5d_pct": 0.0, "price": 10.0, "pct_below_52w_high": 20.0}

    def fake(ticker):
        if ticker not in technicals_by_ticker:
            return None
        return {"name": ticker, "price": 10.0, "currency": "USD", "sector": "", "fundamentals": {},
                "technicals": {**base, **technicals_by_ticker[ticker]}}
    monkeypatch.setattr(sc.market_data_provider, "get_stock_data", fake)
    sc._scan_cache.clear()
    res = sc.MarketScanner.scan_universe("US", 50)
    sc._scan_cache.clear()
    return [c["ticker"] for c in res]


def test_scanner_drops_chop_before_the_committee_sees_it(monkeypatch):
    # The scan decides which tickers get an LLM debate: a ranging or dead-flat stock must not cost tokens
    found = _scan_with(monkeypatch, {
        "AAPL": {"adx14": 31.0, "atr_pct": 2.1},   # trending, moving
        "MSFT": {"adx14": 12.0, "atr_pct": 2.1},   # no trend
        "NVDA": {"adx14": 31.0, "atr_pct": 0.3},   # trend, but too quiet to clear the fees
        "AMZN": {},                                # no ADX known: fail open
    })
    assert found == ["AAPL", "AMZN"]


def test_chop_filter_can_be_switched_off(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "CHOP_FILTER_ENABLED", False)
    assert _scan_with(monkeypatch, {"MSFT": {"adx14": 12.0, "atr_pct": 0.3}}) == ["MSFT"]
