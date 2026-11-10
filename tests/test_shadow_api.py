import pytest
from httpx import AsyncClient, ASGITransport

from tests.test_shadow_store import _fresh_store  # noqa: F401  (autouse: clean tables, seeded strategies)


@pytest.fixture
async def client():
    from src.web.app import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_schema_lists_all_params(client):
    names = {r["name"] for r in (await client.get("/api/shadow/schema")).json()}
    assert "stop_scale" in names and "ignore_risk_veto" in names


async def test_create_edit_copy_delete_roundtrip(client):
    r = await client.post("/api/shadow", json={"label": "Api One", "base": "aggressive", "params": {"stop_scale": 1.5}})
    assert r.status_code == 200 and r.json()["key"] == "api_one"
    assert (await client.patch("/api/shadow/api_one", json={"params": {"stop_scale": 1.1}, "learning_strength": 2})).status_code == 200
    hist = (await client.get("/api/shadow/api_one/history")).json()
    assert hist[0]["changes"]["stop_scale"] == {"old": 1.5, "new": 1.1}
    assert (await client.post(f"/api/shadow/api_one/history/{hist[0]['id']}/undo")).status_code == 200
    assert (await client.post("/api/shadow/api_one/copy", json={"label": "Api Two"})).json()["key"] == "api_two"
    assert (await client.delete("/api/shadow/api_one")).status_code == 200
    assert (await client.delete("/api/shadow/api_one")).status_code == 404


async def test_invalid_input_is_422_with_field_name(client):
    r = await client.patch("/api/shadow/baseline", json={"params": {"stop_scale": 99}})
    assert r.status_code == 422 and "stop_scale" in r.json()["detail"]
    assert (await client.patch("/api/shadow/baseline", json={"learning_strength": 7})).status_code == 422


async def test_malformed_and_huge_inputs_are_422(client):
    assert (await client.post("/api/shadow", json={"label": ""})).status_code == 422
    assert (await client.post("/api/shadow", json={"params": {}})).status_code == 422
    assert (await client.post("/api/shadow", json={"label": "X", "params": [1]})).status_code == 422
    assert (await client.patch("/api/shadow/baseline", json={"params": "x"})).status_code == 422
    r = await client.patch("/api/shadow/baseline", content='{"params": {"max_positions": 1%s}}' % ("0" * 400),
                           headers={"content-type": "application/json"})
    assert r.status_code == 422 and "max_positions" in r.json()["detail"]


async def test_undo_of_other_strategys_change_is_404(client):
    await client.patch("/api/shadow/baseline", json={"params": {"stop_scale": 1.2}})
    hist = (await client.get("/api/shadow/baseline/history")).json()
    assert (await client.post(f"/api/shadow/aggressive/history/{hist[0]['id']}/undo")).status_code == 404
    assert (await client.post("/api/shadow/baseline/history/999999/undo")).status_code == 404


async def test_comparison_exposes_learning_fields(client):
    s = (await client.get("/api/shadow")).json()["strategies"][0]
    assert {"learning_strength", "copied_from", "is_preset"} <= set(s)
