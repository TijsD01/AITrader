import datetime

import pandas as pd
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from src.data import market as market_module
from src.data.market import market_data_provider
from src.db import session as db_session
from src.db.models import AnalysisReport, Position, ShadowPosition, ShadowTrigger, Trade
from src.trading import trading212
from src.trading.broker import get_active_broker

T = "ZZTD"


def _position(is_live, entry, stop):
    return Position(ticker=T, name="Zz Test", currency="USD", shares=2, entry_price_native=entry,
                    current_price_native=entry, stop_loss_native=stop, take_profit_native=120.0,
                    highest_price_native=105.0, entry_price_eur=entry, current_price_eur=entry,
                    cost_basis_eur=2 * entry, status="OPEN", is_live=is_live)


async def _cleanup():
    async with db_session.AsyncSessionLocal() as s:
        for model in (Position, Trade, AnalysisReport, ShadowPosition, ShadowTrigger):
            await s.execute(delete(model).where(model.ticker == T))
        await s.commit()


@pytest.fixture
async def seeded(monkeypatch):
    await _cleanup()
    now = datetime.datetime.utcnow()
    async with db_session.AsyncSessionLocal() as s:
        s.add(_position(False, 100.0, 95.0))
        s.add(_position(True, 200.0, 190.0))  # live row, hidden in paper mode
        s.add(Trade(ticker=T, order_type="BUY", shares=2, price_native=100.0, price_eur=100.0, total_eur=200.0,
                    is_live=False, executed_at=now - datetime.timedelta(days=3)))
        s.add(AnalysisReport(ticker=T, action="BUY", consensus_score=77, target_price=130.0, stop_loss=92.0,
                             raw_signals={"entry_trigger_price": 98.5, "trigger_type": "LIMIT_PULLBACK"}))
        s.add(ShadowTrigger(strategy="aggressive", ticker=T, trigger_type="BREAKOUT_BUY", target_price=110.0,
                            current_price_at_creation=104.0, stop_loss=101.0, take_profit=125.0, status="PENDING"))
        s.add(ShadowPosition(strategy="baseline", ticker=T, shares=1, entry_price_native=99.0,
                             current_price_native=104.0, highest_price_native=106.0, stop_loss_native=94.0,
                             take_profit_native=118.0, entry_price_eur=99.0, current_price_eur=104.0,
                             cost_basis_eur=99.0, status="OPEN", opened_at=now - datetime.timedelta(days=2)))
        await s.commit()

    # Seeding a real allocation plan is awkward, so the broker's trigger list is faked
    async def fake_triggers(status=None):
        base = {"ticker": T, "name": "Zz Test", "currency": "USD", "trigger_type": "LIMIT_PULLBACK",
                "target_price": 97.0, "stop_loss": 93.0, "take_profit": 121.0, "planned_shares": None,
                "consensus_score": 70, "ai_rationale": "r", "created_at": "2026-11-01 10:00"}
        return [
            {**base, "id": 901, "status": "PENDING", "allocation_eur": 450.0, "planned_shares": 4},
            {**base, "id": 902, "status": "PENDING", "allocation_eur": 0.0},
            {**base, "id": 903, "status": "CANCELLED", "allocation_eur": 300.0},
            {**base, "id": 904, "ticker": "OTHER", "status": "PENDING", "allocation_eur": 100.0},
        ]
    monkeypatch.setattr(get_active_broker(), "get_triggers", fake_triggers)
    monkeypatch.setattr(market_data_provider, "get_stock_data",
                        lambda sym: {"name": "Zz Test Inc", "currency": "USD", "price": 104.2})
    monkeypatch.setattr(market_data_provider, "get_price_history",
                        lambda sym, rk: [["2026-11-01T00:00", 101.0], ["2026-11-02T00:00", 104.0]])
    yield
    await _cleanup()


async def _get(path):
    from src.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        return await ac.get(path)


async def test_detail_collects_every_level_set(seeded):
    r = await _get(f"/api/ticker/{T.lower()}/detail?range=3M")
    assert r.status_code == 200
    data = r.json()
    assert data["ticker"] == T and data["range"] == "3M" and data["current_price"] == 104.2
    assert data["name"] == "Zz Test Inc" and data["currency"] == "USD"
    assert len(data["history"]) == 2

    positions = [s for s in data["level_sets"] if s["kind"] == "position"]
    assert len(positions) == 1  # the is_live=True row is excluded in paper mode
    pos = positions[0]
    assert pos["state"] == "live"
    assert pos["levels"] == {"entry": 100.0, "limit": None, "stop": 95.0, "take_profit": 120.0,
                             "high": 105.0, "breakeven_at": 102.5}

    by_id = {s["id"]: s for s in data["level_sets"]}
    assert by_id["trigger:901"]["state"] == "live"
    assert by_id["trigger:901"]["info"]["allocation_eur"] == 450.0
    assert by_id["trigger:902"]["state"] == "unfunded"
    assert by_id["trigger:903"]["state"] == "past"
    assert "trigger:904" not in by_id
    assert by_id["trigger:901"]["levels"]["limit"] == 97.0

    report = by_id[f"report:{T}"]
    assert report["state"] == "would_be" and report["trigger_type"] == "LIMIT_PULLBACK"
    assert report["levels"]["limit"] == 98.5
    assert report["levels"]["stop"] == 92.0 and report["levels"]["take_profit"] == 130.0

    st = [s for s in data["level_sets"] if s["kind"] == "shadow_trigger"]
    assert len(st) == 1 and st[0]["state"] == "would_be" and st[0]["levels"]["limit"] == 110.0
    sp = [s for s in data["level_sets"] if s["kind"] == "shadow_position"]
    assert len(sp) == 1 and sp[0]["state"] == "simulated" and sp[0]["label"].startswith("Shadow · ")
    assert sp[0]["levels"]["high"] == 106.0

    sources = {(t["side"], t["source"]) for t in data["trades"]}
    assert ("BUY", "main") in sources and ("BUY", "shadow:baseline") in sources


async def test_breakeven_hidden_once_stop_reaches_entry(seeded):
    async with db_session.AsyncSessionLocal() as s:
        await s.execute(delete(Position).where(Position.ticker == T))
        s.add(_position(False, 100.0, 101.0))
        await s.commit()
    data = (await _get(f"/api/ticker/{T}/detail")).json()
    pos = [s for s in data["level_sets"] if s["kind"] == "position"][0]
    assert pos["levels"]["breakeven_at"] is None


async def test_history_failure_still_returns_200(seeded, monkeypatch):
    monkeypatch.setattr(market_data_provider, "get_price_history", lambda sym, rk: None)
    monkeypatch.setattr(market_data_provider, "get_stock_data", lambda sym: None)
    r = await _get(f"/api/ticker/{T}/detail?range=1D")
    assert r.status_code == 200
    data = r.json()
    assert data["history"] == [] and data["range"] == "1D"
    assert data["name"] == "Zz Test"  # falls back to the position row
    assert any(s["kind"] == "position" for s in data["level_sets"])


async def test_invalid_ticker_is_400():
    r = await _get("/api/ticker/not a ticker!!/detail")
    assert r.status_code == 400


def test_price_history_range_mapping(monkeypatch):
    calls = []

    class FakeTicker:
        def __init__(self, sym):
            pass

        def history(self, **kwargs):
            calls.append(kwargs)
            idx = pd.DatetimeIndex(["2026-11-02 09:30", "2026-11-02 09:35"], tz="America/New_York")
            return pd.DataFrame({"Close": [10.123, 10.456]}, index=idx)

    monkeypatch.setattr(market_module.yf, "Ticker", FakeTicker)
    market_module._history_cache.clear()
    pts = market_data_provider.get_price_history("ZZTD", "1D")
    assert calls[-1] == {"period": "1d", "interval": "5m", "prepost": True}
    assert pts == [["2026-11-02T14:30", 10.12], ["2026-11-02T14:35", 10.46]]
    market_data_provider.get_price_history("ZZTD", "bogus")
    assert calls[-1] == {"period": "3mo", "interval": "1d"}
    for key, period in (("1M", "1mo"), ("1Y", "1y")):
        market_data_provider.get_price_history("ZZTD", key)
        assert calls[-1] == {"period": period, "interval": "1d"}
    n = len(calls)
    market_data_provider.get_price_history("ZZTD", "1Y")  # served from cache
    assert len(calls) == n
    market_module._history_cache.clear()


class _FakeT212:
    def __init__(self, positions=None, boom=False):
        self._positions, self.boom, self.calls = positions or [], boom, 0

    async def positions(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("T212 down")
        return self._positions

    async def resolve_ticker(self, sym, currency):
        return "ZZTD_US_EQ" if sym == T else None


def _t212(monkeypatch, client, broker="trading212"):
    from config.settings import settings
    monkeypatch.setattr(settings, "LIVE_BROKER", broker)
    monkeypatch.setattr(trading212, "get_client", lambda: client)


def _held(price):
    return [{"instrument": {"ticker": "ZZTD_US_EQ"}, "currentPrice": price}]


async def test_t212_price_preferred_and_history_extended(seeded, monkeypatch):
    _t212(monkeypatch, _FakeT212(_held(105.5)))
    data = (await _get(f"/api/ticker/{T}/detail")).json()
    assert data["current_price"] == 105.5 and data["price_source"] == "trading212"
    assert len(data["history"]) == 3 and data["history"][-1][1] == 105.5


async def test_no_t212_client_falls_back_to_yahoo(seeded, monkeypatch):
    _t212(monkeypatch, None)
    data = (await _get(f"/api/ticker/{T}/detail")).json()
    assert data["current_price"] == 104.2 and data["price_source"] == "yahoo" and len(data["history"]) == 2


async def test_t212_failure_falls_back_to_yahoo(seeded, monkeypatch):
    _t212(monkeypatch, _FakeT212(boom=True))
    r = await _get(f"/api/ticker/{T}/detail")
    assert r.status_code == 200
    assert r.json()["current_price"] == 104.2 and r.json()["price_source"] == "yahoo"


async def test_cached_history_not_mutated(seeded, monkeypatch):
    cached = [["2026-11-01T00:00", 101.0], ["2026-11-02T00:00", 104.0]]
    monkeypatch.setattr(market_data_provider, "get_price_history", lambda sym, rk: cached)
    _t212(monkeypatch, _FakeT212(_held(105.5)))
    for _ in range(2):
        data = (await _get(f"/api/ticker/{T}/detail")).json()
        assert len(data["history"]) == 3
    assert len(cached) == 2


async def test_other_broker_never_calls_t212(seeded, monkeypatch):
    client = _FakeT212(_held(105.5))
    _t212(monkeypatch, client, broker="alpaca")
    data = (await _get(f"/api/ticker/{T}/detail")).json()
    assert client.calls == 0 and data["price_source"] == "yahoo"
