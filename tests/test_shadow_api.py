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


async def _snapshot(client, key="baseline"):
    s = next(x for x in (await client.get("/api/shadow")).json()["strategies"] if x["key"] == key)
    return s["label"], s["params"]["stop_scale"], s["learning_strength"], await client.get(f"/api/shadow/{key}/history")


@pytest.mark.parametrize("body", [
    {"label": "New", "params": {"stop_scale": 99}},
    {"label": "New", "learning_strength": 7},
    {"label": "   "},
])
async def test_patch_is_all_or_nothing(client, body):
    before = await _snapshot(client)
    assert (await client.patch("/api/shadow/baseline", json=body)).status_code == 422
    after = await _snapshot(client)
    assert before[:3] == after[:3] and before[3].json() == after[3].json()


async def test_patch_label_and_params_saved_with_one_history_row(client):
    n = len((await _snapshot(client))[3].json())  # history rows persist across tests
    r = await client.patch("/api/shadow/baseline", json={"label": "  New  ", "params": {"stop_scale": 1.3}})
    assert r.status_code == 200
    label, stop, _, hist = await _snapshot(client)
    assert (label, stop) == ("New", 1.3) and len(hist.json()) == n + 1


async def test_patch_label_truncated_to_60(client):
    await client.patch("/api/shadow/baseline", json={"label": "x" * 100})
    assert len((await _snapshot(client))[0]) == 60


async def test_patch_deleted_key_is_404_and_writes_nothing(client):
    assert (await client.delete("/api/shadow/aggressive")).status_code == 200
    assert (await client.patch("/api/shadow/aggressive", json={"label": "Z", "params": {"stop_scale": 1.2}})).status_code == 404
    assert (await client.get("/api/shadow/aggressive/history")).json() == []


async def test_undo_id_out_of_range_is_not_500(client):
    r = await client.post(f"/api/shadow/baseline/history/{10**30}/undo")
    assert r.status_code in (404, 422)


async def test_learn_route_returns_learner_result(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.trading.shadow_learner import shadow_learner
    result = {"success": True, "skipped": True, "message": "Needs 5 closed trades.", "changes": {}}
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(shadow_learner, "learn", mock)
    r = await client.post("/api/shadow/baseline/learn")
    assert r.status_code == 200 and r.json() == result
    mock.assert_awaited_once_with("baseline", manual=True)


async def test_learn_route_unknown_key_is_404(client, monkeypatch):
    from unittest.mock import AsyncMock
    from src.trading.shadow_learner import shadow_learner
    mock = AsyncMock()
    monkeypatch.setattr(shadow_learner, "learn", mock)
    assert (await client.post("/api/shadow/nope/learn")).status_code == 404
    mock.assert_not_awaited()
