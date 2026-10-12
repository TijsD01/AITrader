import pytest
import pytest_asyncio
import tempfile
from pathlib import Path
from httpx import AsyncClient, ASGITransport

from src.memory.models import (
    L0Message, L1Atom, L2Scenario, L3CoreProfile, SkillAsset, WikiPage
)
from src.memory.store import AgentMemoryStore
from src.memory.cold_start import ColdStartEngine
from src.memory.extractor import AssetExtractor
from src.memory.client import AgentMemoryClient
from src.web.app import app

@pytest.fixture
def temp_db_path(tmp_path):
    return tmp_path / "test_memory.db"

@pytest.mark.asyncio
async def test_memory_store_crud(temp_db_path):
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()

    # 1. L0 Raw Messages
    ids = await store.add_l0_messages(
        session_id="test_sess_1",
        messages=[
            L0Message(role="user", content="What is the current deployable cash?"),
            L0Message(role="assistant", content="Deployable cash is €100.00 after €100 reserved for trigger.")
        ]
    )
    assert len(ids) == 2
    l0_rows = await store.query_l0(session_id="test_sess_1")
    assert len(l0_rows) == 2
    assert "€100.00" in l0_rows[0]["content"]

    # 2. L1 Atom CRUD
    atom = L1Atom(
        atom_id="test_atom_1",
        type="instruction",
        subject="BESI.AS",
        predicate="minimum_order",
        object="€100.00",
        content="BESI.AS minimum order is €100.00 to avoid fee drag."
    )
    atom_row_id = await store.add_l1_atom(atom)
    assert atom_row_id is not None
    search_res = await store.search_l1_atoms(query="BESI.AS")
    assert len(search_res) >= 1
    assert search_res[0]["subject"] == "BESI.AS"

    # 3. L2 Scenario
    sc = L2Scenario(
        scenario_key="test-scenario",
        title="Test Scenario",
        summary="A test scenario summary",
        context_content="Details here",
        active_entities=["BESI.AS", "IBKR"]
    )
    await store.update_scenario(sc)
    fetched_sc = await store.get_or_create_scenario("test-scenario")
    assert fetched_sc["title"] == "Test Scenario"
    assert "BESI.AS" in fetched_sc["active_entities"]

    # 4. L3 Core Profile
    l3 = await store.get_core_profile()
    assert "Committee" in l3["persona_name"]
    assert len(l3["risk_rules"]) >= 1

    # 5. Skills
    skills = await store.search_skills()
    assert len(skills) >= 1
    assert any("fee" in s["skill_id"].lower() for s in skills)

    # 6. Wiki
    wiki_pages = await store.list_wiki_pages()
    assert len(wiki_pages) >= 1
    assert any("ibkr" in w["page_id"].lower() for w in wiki_pages)

@pytest.mark.asyncio
async def test_cold_start_engine():
    loadout = await ColdStartEngine.generate_cold_start_loadout()
    assert loadout.status_summary is not None
    assert "Total Equity" in loadout.status_summary
    assert loadout.what_had_happened is not None
    assert "INSTANT MEMORY LOADOUT" in loadout.full_prompt_context
    assert len(loadout.l1_key_facts) >= 1
    assert len(loadout.active_skills) >= 1

@pytest.mark.asyncio
async def test_asset_extractor():
    text = (
        "Deliberated BUY on BESI.AS with consensus score 85/100. "
        "Armed trigger #96 for BESI.AS at €175.88 with €100.00 allocation. "
        "Enforced IBKR tiered commission minimum €1.90 with fee drag limit 3.8%."
    )
    res = await AssetExtractor.extract_from_text(text, session_id="test_extract", sync_to_store=True)
    assert res["extracted_atoms_count"] >= 1
    # Check that atoms have extracted ticker and constraints
    extracted_subs = [a["subject"] for a in res["extracted_atoms"]]
    assert any("BESI.AS" in s or "IBKR" in s or "Capital" in s for s in extracted_subs)

@pytest.mark.asyncio
async def test_agent_memory_client():
    client = AgentMemoryClient()
    # Test cold start
    cs = await client.get_cold_start_loadout()
    assert "INSTANT MEMORY LOADOUT" in cs.full_prompt_context

    # Test trade event logging
    await client.record_trade_event(
        ticker="ASML.AS",
        order_type="BUY",
        shares=1,
        price=850.0,
        currency="EUR",
        fee_eur=1.90,
        rationale="Pullback test"
    )

    # Test trigger event logging
    await client.record_trigger_event(
        trigger_id=101,
        ticker="ASML.AS",
        trigger_type="LIMIT_PULLBACK",
        target_price=820.0,
        allocation_eur=100.0,
        rationale="Dip entry"
    )

    # Hermes recall
    mems = await client.recall(query="ASML")
    assert isinstance(mems, list)

@pytest.mark.asyncio
async def test_memory_api_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Health check
        res = await ac.get("/health")
        assert res.status_code == 200
        health = res.json()
        assert health["status"] == "ok"
        assert health["service"] == "TencentDB-Agent-Memory-Hub"

        # 2. Cold Start API
        res = await ac.get("/v3/memory/cold-start")
        assert res.status_code == 200
        cs_data = res.json()
        assert cs_data["code"] == 0
        assert "what_had_happened" in cs_data["data"]

        # 3. Chat-Memory Context API
        res = await ac.post("/v3/chat-memory/context", json={"query": "BESI.AS"})
        assert res.status_code == 200
        ctx_data = res.json()
        assert ctx_data["code"] == 0

        # 4. Memory Assets catalog
        res = await ac.get("/v3/memory/assets")
        assert res.status_code == 200
        assets = res.json()["data"]
        assert "chat_memory" in assets
        assert "skills" in assets
        assert "llm_wiki" in assets
        assert "code_graph" in assets

        # 5. Extract endpoint
        res = await ac.post("/v3/memory/extract", json={
            "raw_text": "Investigated BESI.AS pullback entry at €175.88. Enforced Euronext minimum €100 order size to limit IBKR tiered commission drag to 3.8%."
        })
        assert res.status_code == 200
        ext_data = res.json()
        assert ext_data["code"] == 0
        assert ext_data["data"]["extracted_atoms_count"] >= 1

        # 6. Proxy endpoint (OpenAI format)
        res = await ac.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [
                {"role": "user", "content": "What is our current cash status?"}
            ]
        })
        assert res.status_code == 200
        comp = res.json()
        assert "choices" in comp
