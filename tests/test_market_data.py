import pytest
from src.data.market import market_data_provider
from src.data.currency import currency_service

def test_currency_conversion():
    # 100 EUR should equal 100 EUR
    assert currency_service.to_eur(100.0, "EUR") == 100.0
    # $108 USD should convert to ~€100 EUR assuming ~1.08 rate
    eur_val = currency_service.to_eur(108.0, "USD")
    assert 90.0 < eur_val < 115.0

def test_market_data_us_stock():
    # Test AAPL
    data = market_data_provider.get_stock_data("AAPL")
    assert data is not None
    assert data["ticker"] == "AAPL"
    assert data["price"] > 0
    assert "technicals" in data
    t = data["technicals"]
    assert 0 <= t["rsi14"] <= 100
    assert t["ema20"] > 0
    assert t["support_20d"] <= t["resistance_20d"]

def test_market_data_eu_stock():
    # Test ASML.AS (Euronext Amsterdam)
    data = market_data_provider.get_stock_data("ASML.AS")
    assert data is not None
    assert data["ticker"] == "ASML.AS"
    assert data["price"] > 0
    assert data["currency"] in ("EUR", "EUR")
    assert "technicals" in data
