import datetime
import pytest
from unittest.mock import patch, AsyncMock
from httpx import AsyncClient, ASGITransport
from sqlalchemy import delete

from src.db import session as db_session
from src.db.models import CashFlow, PortfolioSnapshot, SystemConfig
from src.trading.cash_flows import detect_flow, scan_live_cash_flows, net_cash_flows_eur, SCAN_KEY


def _snap(cash, total):
    return PortfolioSnapshot(cash_eur=cash, equity_eur=total - cash, total_value_eur=total, is_live=True)


def test_detect_flow_tells_deposits_from_trades():
    base = _snap(1000.0, 5000.0)
    # Deposit: cash and total rise together
    assert detect_flow(base, _snap(2000.0, 6000.0), min_eur=100) == 1000.0
    # Withdrawal
    assert detect_flow(base, _snap(500.0, 4500.0), min_eur=100) == -500.0
    # Deposit on a day the shares fell: still a deposit of the cash amount
    assert detect_flow(base, _snap(2000.0, 5700.0), min_eur=100) == 1000.0
    # Buy fill: cash swapped for shares, total flat
    assert detect_flow(base, _snap(500.0, 4998.0), min_eur=100) is None
    # Broker stop sold a position the bot never recorded: cash up, total flat
    assert detect_flow(base, _snap(1800.0, 4990.0), min_eur=100) is None
    # Market move only
    assert detect_flow(base, _snap(1000.0, 5400.0), min_eur=100) is None
    # Small dividend stays gain
    assert detect_flow(base, _snap(1020.0, 5020.0), min_eur=100) is None
    # €1,000 deposit plus a booked €800 buy between two snapshots: cash shows only +200, the deposit is still 1000
    assert detect_flow(base, _snap(1200.0, 6000.0), min_eur=100, traded_cash_eur=800.0) == 1000.0
    # The bot's own buy alone is no flow
    assert detect_flow(base, _snap(200.0, 4995.0), min_eur=100, traded_cash_eur=800.0) is None


async def _fresh_live_history():
    """Isolates this test from live snapshots other tests left behind: starts the scan at a known baseline."""
    async with db_session.AsyncSessionLocal() as session:
        await session.execute(delete(CashFlow))
        baseline = _snap(1000.0, 1000.0)
        session.add(baseline)
        await session.flush()
        max_id = baseline.id
        cfg = await session.get(SystemConfig, SCAN_KEY)
        if cfg:
            cfg.value = str(max_id)
        else:
            session.add(SystemConfig(key=SCAN_KEY, value=str(max_id)))
        await session.commit()
        return max_id


async def _cleanup(from_id):
    async with db_session.AsyncSessionLocal() as session:
        await session.execute(delete(CashFlow))
        await session.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.id >= from_id))
        await session.commit()


@pytest.mark.asyncio
async def test_deposit_is_not_counted_as_live_gain():
    from src.trading.live import live_broker
    first_id = await _fresh_live_history()
    try:
        await _check_deposit_flow(live_broker)
    finally:
        await _cleanup(first_id)


async def _check_deposit_flow(live_broker):
    t0 = datetime.datetime.utcnow() - datetime.timedelta(days=2)
    async with db_session.AsyncSessionLocal() as session:
        for i, (cash, total) in enumerate([(1000.0, 1000.0), (3000.0, 3000.0), (3000.0, 3050.0)]):
            s = _snap(cash, total)
            s.timestamp = t0 + datetime.timedelta(minutes=10 * i)
            session.add(s)
        await session.commit()

    assert await scan_live_cash_flows() == 1
    # A second scan finds nothing new
    assert await scan_live_cash_flows() == 0
    assert await net_cash_flows_eur(is_live=True) == 2000.0
    assert await net_cash_flows_eur(is_live=True, since=t0 + datetime.timedelta(minutes=15)) == 0.0

    with patch.object(live_broker, "refresh_positions_pnl", AsyncMock()), \
         patch.object(live_broker, "adjust_triggers_dynamically", AsyncMock()):
        summary = await live_broker.get_portfolio_summary()
    assert summary["net_deposits_eur"] == 2000.0
    assert summary["invested_capital_eur"] == round(summary["initial_capital_eur"] + 2000.0, 2)
    assert summary["total_gain_eur"] == round(summary["total_value_eur"] - summary["invested_capital_eur"], 2)
    expected_pct = round(summary["total_gain_eur"] / summary["invested_capital_eur"] * 100, 2)
    assert summary["total_return_pct"] == expected_pct


@pytest.mark.asyncio
async def test_cash_flow_api_add_and_remove():
    from src.web.app import app
    first_id = await _fresh_live_history()
    try:
        await _check_api(ASGITransport(app=app))
    finally:
        await _cleanup(first_id)


async def _check_api(transport):
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/api/cash-flows", json={"amount_eur": 250.0, "note": "Missed deposit"})
        assert r.status_code == 200
        flow_id = r.json()["id"]

        r = await ac.get("/api/cash-flows")
        data = r.json()
        assert data["net_deposits_eur"] == 250.0
        assert data["flows"][0]["source"] == "manual"

        assert (await ac.post("/api/cash-flows", json={"amount_eur": 0})).status_code == 400
        assert (await ac.delete(f"/api/cash-flows/{flow_id}")).status_code == 200
        assert (await ac.delete(f"/api/cash-flows/{flow_id}")).status_code == 404
        assert (await ac.get("/api/cash-flows")).json()["net_deposits_eur"] == 0.0


def test_broker_flow_amount_signs():
    from src.trading.cash_flows import broker_flow_amount
    assert broker_flow_amount({"type": "DEPOSIT", "amount": 500, "currency": "EUR"}) == 500.0
    # Withdrawal sign is taken from the type, whatever sign the amount has
    assert broker_flow_amount({"type": "WITHDRAW", "amount": 200, "currency": "EUR"}) == -200.0
    assert broker_flow_amount({"type": "WITHDRAW", "amount": -200, "currency": "EUR"}) == -200.0
    assert broker_flow_amount({"type": "TRANSFER", "amount": -50, "currency": "EUR"}) == -50.0
    # A fee is a cost, so it stays in the gain
    assert broker_flow_amount({"type": "FEE", "amount": -1.5, "currency": "EUR"}) is None


async def test_t212_history_transactions_pages_until_known_ref():
    import httpx
    from src.trading import trading212
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if "cursor=2" in str(request.url):
            return httpx.Response(200, json={"items": [{"reference": "b", "type": "DEPOSIT", "amount": 1}],
                                             "nextPagePath": "/api/v0/equity/history/transactions?limit=50&cursor=3"})
        return httpx.Response(200, json={"items": [{"reference": "a", "type": "DEPOSIT", "amount": 2}],
                                         "nextPagePath": "/api/v0/equity/history/transactions?limit=50&cursor=2"})

    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    items = await client.history_transactions(known_refs={"b"})
    assert [i["reference"] for i in items] == ["a", "b"]
    assert len(calls) == 2
    assert calls[1].endswith("/api/v0/equity/history/transactions?limit=50&cursor=2")


async def test_t212_deposits_and_withdrawals_are_not_gain():
    from src.trading import cash_flows
    first_id = await _fresh_live_history()
    soon = datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
    items = [
        {"type": "DEPOSIT", "amount": 1000, "currency": "EUR", "reference": "d1", "dateTime": soon.isoformat() + "Z"},
        {"type": "WITHDRAW", "amount": 300, "currency": "EUR", "reference": "w1", "dateTime": soon.isoformat() + "Z"},
        {"type": "FEE", "amount": -2, "currency": "EUR", "reference": "f1", "dateTime": soon.isoformat() + "Z"},
        # Before the first live snapshot: already part of the starting value
        {"type": "DEPOSIT", "amount": 5000, "currency": "EUR", "reference": "old", "dateTime": "2000-01-01T00:00:00Z"},
    ]
    client = AsyncMock()
    client.history_transactions.return_value = items
    try:
        async with db_session.AsyncSessionLocal() as session:
            session.add(CashFlow(amount_eur=999.0, source="auto", is_live=True))  # an earlier guess
            await session.commit()
        cash_flows._broker_synced_at = 0.0
        with patch("src.trading.trading212.get_client", return_value=client), \
             patch.object(cash_flows.settings, "LIVE_BROKER", "trading212"):
            assert await scan_live_cash_flows() == 2
            assert await net_cash_flows_eur(is_live=True) == 700.0
            # Throttled, then a later sync adds nothing twice
            assert await scan_live_cash_flows() == 0
            cash_flows._broker_synced_at = 0.0
            assert await scan_live_cash_flows() == 0
        assert await net_cash_flows_eur(is_live=True) == 700.0
        assert {f["source"] for f in await cash_flows.list_cash_flows(is_live=True)} == {"broker"}
    finally:
        cash_flows._broker_synced_at = 0.0
        await _cleanup(first_id)


async def test_t212_failure_falls_back_to_snapshot_guess():
    from src.trading import cash_flows
    first_id = await _fresh_live_history()
    client = AsyncMock()
    client.history_transactions.side_effect = RuntimeError("HTTP 500")
    try:
        async with db_session.AsyncSessionLocal() as session:
            session.add(_snap(3000.0, 3000.0))
            await session.commit()
        cash_flows._broker_synced_at = 0.0
        with patch("src.trading.trading212.get_client", return_value=client), \
             patch.object(cash_flows.settings, "LIVE_BROKER", "trading212"):
            assert await scan_live_cash_flows() == 1
            # A failed broker read is not retried on the next page load (a 429 would make it wait again)
            assert await scan_live_cash_flows() == 0
            assert client.history_transactions.await_count == 1
        assert await net_cash_flows_eur(is_live=True) == 2000.0
    finally:
        cash_flows._broker_synced_at = 0.0
        await _cleanup(first_id)


async def test_t212_history_transactions_stops_before_since():
    import httpx
    from src.trading import trading212
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"items": [{"reference": str(len(calls)), "dateTime": "2026-01-01T00:00:00Z"}],
                                         "nextPagePath": f"/api/v0/equity/history/transactions?cursor={len(calls)}"})

    client = trading212.Trading212Client("k", "s", transport=httpx.MockTransport(handler))
    await client.history_transactions(since=datetime.datetime(2026, 6, 1))
    assert len(calls) == 1  # the first page already reaches back past since
