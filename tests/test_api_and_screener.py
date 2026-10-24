import pytest
from httpx import AsyncClient, ASGITransport
from src.web.app import app
from src.screener.scanner import market_scanner

def test_screener_evaluation():
    # Test screening a small sample
    candidates = market_scanner.scan_universe(market="EU", limit=3)
    assert isinstance(candidates, list)
    if candidates:
        first = candidates[0]
        assert "ticker" in first
        assert "pre_score" in first
        assert 0 <= first["pre_score"] <= 100

@pytest.mark.asyncio
async def test_fastapi_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Test GET /
        resp = await ac.get("/")
        assert resp.status_code == 200

        # Test GET /api/portfolio
        port_resp = await ac.get("/api/portfolio")
        assert port_resp.status_code == 200
        port_data = port_resp.json()
        assert port_data["base_currency"] == "EUR"
        assert "cash_eur" in port_data

        # Test GET /api/positions
        pos_resp = await ac.get("/api/positions")
        assert pos_resp.status_code == 200
        assert isinstance(pos_resp.json(), list)

        # Test GET /api/watchlist
        watch_resp = await ac.get("/api/watchlist")
        assert watch_resp.status_code == 200
        assert len(watch_resp.json()) > 0

        # Test GET /api/settings
        set_resp = await ac.get("/api/settings")
        assert set_resp.status_code == 200
        assert "auto_buy_enabled" in set_resp.json()


def test_scan_universe_includes_watchlist_extras(monkeypatch):
    from src.screener import scanner as sc
    seen = []
    monkeypatch.setattr(sc.market_data_provider, "get_stock_data", lambda t: seen.append(t) and None)
    sc._scan_cache.clear()
    sc.MarketScanner.scan_universe("US", 5, extra_tickers=["zzzz", "ZZZZ", "AAPL", "FOO.AS"])
    assert seen.count("ZZZZ") == 1       # added once, upper-cased
    assert seen.count("AAPL") <= 1        # de-duplicated against fixed universe
    assert "FOO.AS" not in seen           # EU ticker not in US scan
    sc._scan_cache.clear()
    seen.clear()
    sc.MarketScanner.scan_universe("EU", 5, extra_tickers=["FOO.AS", "ZZZZ"])
    assert "FOO.AS" in seen and "ZZZZ" not in seen
    sc._scan_cache.clear()
