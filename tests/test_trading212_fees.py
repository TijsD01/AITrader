import pytest
from unittest.mock import patch
from config.settings import settings
from src.trading.fees import calculate_broker_fee, get_min_viable_order_eur
from src.data.currency import currency_service


@pytest.fixture(autouse=True)
def _t212(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_BROKER", "trading212")


def _fee(ticker, cur, action="BUY", price=100.0, shares=10.0):
    return calculate_broker_fee(ticker, shares, price, cur, order_action=action)


def test_eur_stock_free():
    r = _fee("ASML.AS", "EUR")
    assert r["fee_eur"] == 0.0
    assert "Trading 212" in r["breakdown"]


@pytest.mark.parametrize("action", ["BUY", "SELL"])
def test_usd_stock_fx_fee(action):
    r = _fee("AAPL", "USD", action)
    assert r["fee_eur"] == pytest.approx(r["trade_value_eur"] * 0.0015, abs=0.01)
    assert r["fee_eur"] > 0


def test_ftt_on_pa_buy_only():
    buy, sell = _fee("AIR.PA", "EUR"), _fee("AIR.PA", "EUR", "SELL")
    assert buy["fee_eur"] == pytest.approx(1000 * 0.004)
    assert sell["fee_eur"] == 0.0


def test_uk_stamp_duty_buy_only():
    assert _fee("SHEL.L", "GBP")["fee_native"] > _fee("SHEL.L", "GBP", "SELL")["fee_native"]


def test_min_order_floor_only_for_ibkr(monkeypatch):
    monkeypatch.setattr(settings, "MIN_ORDER_EUR_IBKR_EU", 100.0)
    monkeypatch.setattr(settings, "MIN_ORDER_EUR_IBKR_US", 35.0)
    assert get_min_viable_order_eur("ASML.AS", "EUR") < 10
    assert get_min_viable_order_eur("AAPL", "USD") < 10
    monkeypatch.setattr(settings, "LIVE_BROKER", "ibkr")
    assert get_min_viable_order_eur("ASML.AS", "EUR") >= 100.0
    assert get_min_viable_order_eur("AAPL", "USD") >= 35.0
