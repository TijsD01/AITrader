import threading

import httpx

from config.default_universe import EU_STOCKS, US_STOCKS
from scripts.check_t212_universe import SUFFIX_CURRENCY, check_universe
from src.data import market as market_module
from src.trading import trading212
from src.trading.trading212 import EXCHANGE_HINTS


def test_universe_has_no_duplicates():
    tickers = [s["ticker"] for s in EU_STOCKS + US_STOCKS]
    assert len(tickers) == len(set(tickers))


def test_universe_suffixes_are_tradable_on_trading212():
    # A suffix Trading 212 can't map (.ST, .CO, ...) would make every buy of that ticker fail
    for s in EU_STOCKS + US_STOCKS:
        suffix = s["ticker"].partition(".")[2]
        assert suffix in EXCHANGE_HINTS and suffix in SUFFIX_CURRENCY, s["ticker"]
        assert "-" not in s["ticker"], s["ticker"]  # Yahoo BRK-B vs T212 BRK.B does not resolve


def test_scan_runs_in_parallel_and_keeps_ranking(monkeypatch):
    from src.screener import scanner as sc
    threads = set()

    def fake(ticker):
        threads.add(threading.get_ident())
        rsi = 50 if ticker in ("AAPL", "MSFT") else 90  # only AAPL/MSFT score >= 50 with a bonus
        return {"name": ticker, "price": 10.0, "currency": "USD", "sector": "",
                "technicals": {"above_ema50": True, "above_sma200": True, "above_ema20": True, "rsi14": rsi,
                               "macd": 1, "macd_signal": 0, "macd_hist": 1, "volume_surge_ratio": 1.0,
                               "return_5d_pct": 0.0, "price": 10.0, "pct_below_52w_high": 20.0},
                "fundamentals": {}}
    monkeypatch.setattr(sc.market_data_provider, "get_stock_data", fake)
    sc._scan_cache.clear()
    res = sc.MarketScanner.scan_universe("US", 50)
    sc._scan_cache.clear()
    assert len(threads) > 1
    # Equal scores keep universe order (stable sort over results zipped back in pool order)
    top = [c["ticker"] for c in res if c["pre_score"] == res[0]["pre_score"]]
    assert top[:2] == ["AAPL", "MSFT"]


def test_ticker_info_is_cached_between_quote_refreshes(monkeypatch):
    import pandas as pd
    calls = []

    class FakeTicker:
        def __init__(self, sym):
            self.fast_info = {"lastPrice": 10.0, "currency": "USD"}

        def history(self, period):
            n = 60
            return pd.DataFrame({"Close": [10.0] * n, "High": [11.0] * n, "Low": [9.0] * n, "Volume": [100] * n,
                                 "Open": [10.0] * n})

        @property
        def info(self):
            calls.append(1)
            return {"shortName": "Foo", "sector": "Tech"}

    monkeypatch.setattr(market_module.yf, "Ticker", FakeTicker)
    market_module._info_cache.pop("FOO", None)
    market_module._stock_cache.pop("FOO", None)
    assert market_module.MarketDataProvider.get_stock_data("FOO")
    market_module._stock_cache.pop("FOO", None)  # quote expired / refreshed
    assert market_module.MarketDataProvider.get_stock_data("FOO")
    market_module._info_cache.pop("FOO", None)
    market_module._stock_cache.pop("FOO", None)
    assert len(calls) == 1


async def test_check_universe_flags_unmapped_tickers():
    def handler(request):
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json=[
                {"ticker": "ASMLa_EQ", "shortName": "ASML", "type": "STOCK", "currencyCode": "EUR",
                 "workingScheduleId": 1, "minTradeQuantity": 0.01},
                {"ticker": "AAPL_US_EQ", "shortName": "AAPL", "type": "STOCK", "currencyCode": "USD",
                 "workingScheduleId": 2, "minTradeQuantity": 0.01},
            ])
        return httpx.Response(200, json=[{"name": "Euronext Amsterdam", "workingSchedules": [{"id": 1}]},
                                         {"name": "NASDAQ", "workingSchedules": [{"id": 2}]}])
    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    rows = await check_universe(client, ["ASML.AS", "AAPL", "NOVO-B.CO", "MSFT"])
    got = {r["ticker"]: (r["t212"], r["min_qty"]) for r in rows}
    assert got == {"ASML.AS": ("ASMLa_EQ", 0.01), "AAPL": ("AAPL_US_EQ", 0.01),
                   "NOVO-B.CO": (None, None), "MSFT": (None, None)}
