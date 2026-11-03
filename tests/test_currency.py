import pytest
from src.data.currency import CurrencyService


def _svc(monkeypatch, fetch=None):
    svc = CurrencyService()
    calls = []

    class FakeTicker:
        def __init__(self, sym):
            self.sym = sym
            calls.append(sym)

        @property
        def fast_info(self):
            if fetch is None:
                raise RuntimeError("network down")
            return {"lastPrice": fetch[self.sym]}

    monkeypatch.setattr("src.data.currency.yf.Ticker", FakeTicker)
    return svc, calls


def test_pence_divided_by_100(monkeypatch):
    svc, _ = _svc(monkeypatch)
    svc._rates_cache["GBP"] = 0.85
    assert svc.to_eur(1000, "GBp") == pytest.approx(10.0 / 0.85)
    assert svc.to_eur(1000, "GBP") == pytest.approx(1000 / 0.85)
    assert svc.to_eur(1000, "GBX") == pytest.approx(10.0 / 0.85)


def test_eur_and_usd(monkeypatch):
    svc, _ = _svc(monkeypatch)
    assert svc.to_eur(5, "EUR") == 5
    assert svc.to_eur(108, "USD") == pytest.approx(100.0)


def test_other_currencies_fetched(monkeypatch):
    svc, calls = _svc(monkeypatch, fetch={"EURUSD=X": 1.1, "EURCHF=X": 0.9, "EURSEK=X": 10.0})
    assert svc.to_eur(90, "CHF") == pytest.approx(100.0)
    assert svc.to_eur(100, "SEK") == pytest.approx(10.0)
    assert "EURCHF=X" in calls and "EURSEK=X" in calls


def test_failed_fetch_backs_off(monkeypatch):
    svc, calls = _svc(monkeypatch, fetch=None)
    svc.to_eur(100, "CHF")
    n = len(calls)
    assert n >= 1
    svc.to_eur(100, "CHF")
    svc.to_eur(100, "USD")
    assert len(calls) == n
    assert svc._last_update > 0


def test_unknown_currency_falls_back(monkeypatch):
    svc, _ = _svc(monkeypatch)
    assert svc.to_eur(42, "XYZ") == 42
