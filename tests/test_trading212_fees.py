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


def test_no_min_order_floor():
    assert get_min_viable_order_eur("ASML.AS", "EUR") < 10
    assert get_min_viable_order_eur("AAPL", "USD") < 10


def test_pence_fee_is_not_read_as_pounds():
    # 100 VOD.L at 72p is £72; pence must not be converted as if they were pounds
    pence = _fee("VOD.L", "GBp", price=72.0, shares=100.0)
    pounds = _fee("VOD.L", "GBP", price=0.72, shares=100.0)
    assert pence["trade_value_eur"] == pytest.approx(pounds["trade_value_eur"], rel=1e-6)
    assert pence["fee_eur"] == pytest.approx(pounds["fee_eur"], abs=0.01)
    assert pence["fee_eur"] < 1.0
