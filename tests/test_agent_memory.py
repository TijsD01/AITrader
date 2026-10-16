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


# --- Regression tests for memory bug fixes ---

class _FakeBroker:
    async def get_portfolio_summary(self):
        return {
            "total_value_eur": 1234.56,
            "cash_eur": 1000.0,
            "available_cash_eur": 800.0,
            "reserved_triggers_eur": 150.0,
            "open_positions_count": 1,
        }

    async def get_triggers(self, status=None):
        return [{"id": 7, "ticker": "ASML.AS", "trigger_type": "LIMIT_PULLBACK",
                 "target_price": 700.0, "allocation_eur": 150.0, "created_at": "2026-01-01 10:00"}]

    async def get_positions(self):
        return [{"ticker": "NVDA", "shares": 2.0, "entry_price_native": 100.0, "unrealized_pnl_eur": 12.5}]


@pytest.mark.asyncio
async def test_cold_start_reads_real_broker_keys(monkeypatch):
    monkeypatch.setattr(ColdStartEngine, "_portfolio_cache", None)
    monkeypatch.setattr("src.trading.broker.get_active_broker", lambda: _FakeBroker())
    state = await ColdStartEngine._fetch_portfolio_status()
    assert state["available"] is True
    assert state["total_equity_eur"] == 1234.56
    assert state["available_cash_eur"] == 800.0
    assert state["reserved_trigger_cash_eur"] == 150.0
    assert state["open_positions_count"] == 1
    assert state["pending_triggers_count"] == 1
    assert state["active_triggers"][0]["id"] == 7
    assert state["positions"] == [{"ticker": "NVDA", "shares": 2.0, "entry_price": 100.0, "pnl_eur": 12.5}]


@pytest.mark.asyncio
async def test_cold_start_portfolio_status_cached(monkeypatch):
    monkeypatch.setattr(ColdStartEngine, "_portfolio_cache", None)
    calls = {"n": 0}

    def _broker():
        calls["n"] += 1
        return _FakeBroker()

    monkeypatch.setattr("src.trading.broker.get_active_broker", _broker)
    await ColdStartEngine._fetch_portfolio_status()
    await ColdStartEngine._fetch_portfolio_status()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_cold_start_unavailable_state_is_not_fabricated(monkeypatch):
    monkeypatch.setattr(ColdStartEngine, "_portfolio_cache", None)

    def _boom():
        raise RuntimeError("broker down")

    monkeypatch.setattr("src.trading.broker.get_active_broker", _boom)
    state = await ColdStartEngine._fetch_portfolio_status()
    assert state["available"] is False
    assert state["active_triggers"] == []
    assert "total_equity_eur" not in state
    recap = await ColdStartEngine._build_history_recap(state)
    assert "unavailable" in recap
    assert "BESI" not in recap and "#96" not in recap


@pytest.mark.asyncio
async def test_history_recap_uses_lesson_and_handles_null_pnl_pct():
    import datetime
    from src.db.session import AsyncSessionLocal
    from src.db.models import Trade, TradeLesson

    async with AsyncSessionLocal() as session:
        session.add(Trade(
            ticker="RECAP.AS", order_type="SELL", shares=1.0, currency="EUR",
            price_native=10.0, price_eur=10.0, total_eur=10.0, fee_eur=1.9,
            realized_pnl_eur=-3.5, realized_pnl_pct=None,
            executed_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
        ))
        session.add(TradeLesson(
            trade_id=1, ticker="RECAP.AS", outcome="LOSS",
            lesson="Do not chase gaps unique-lesson-marker",
            created_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
        ))
        await session.commit()

    try:
        recap = await ColdStartEngine._build_history_recap({"available": True, "total_equity_eur": 100.0})
        assert "RECAP.AS" in recap
        assert "PnL: €-3.50" in recap
        assert "unique-lesson-marker" in recap
    finally:
        # Remove the future-dated rows so they don't skew other tests (e.g. learner calibration)
        from sqlalchemy import delete
        async with AsyncSessionLocal() as session:
            await session.execute(delete(Trade).where(Trade.ticker == "RECAP.AS"))
            await session.execute(delete(TradeLesson).where(TradeLesson.ticker == "RECAP.AS"))
            await session.commit()


@pytest.mark.asyncio
async def test_search_tokenises_sentence_queries_and_keeps_instructions(temp_db_path):
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()
    await store.add_l1_atom(L1Atom(
        atom_id="a_asml", type="episodic", subject="ASML.AS", predicate="note",
        object="strong order book", content="ASML.AS reported a strong order book."
    ))
    await store.add_l1_atom(L1Atom(
        atom_id="a_other", type="episodic", subject="ZZZ", predicate="note",
        object="nothing", content="Unrelated episodic fact."
    ))
    res = await store.search_l1_atoms(query="What do we know about ASML.AS today?")
    ids = [a["atom_id"] for a in res]
    assert "a_asml" in ids
    assert "a_other" not in ids
    # Instruction atoms (e.g. the seeded fee rule) are always returned, even if unrelated
    assert "atom_init_fee_rule" in ids
    # Stale point-in-time seed facts are gone
    all_ids = [a["atom_id"] for a in await store.search_l1_atoms(limit=100)]
    assert "atom_init_besi_trigger" not in all_ids
    assert "atom_init_account_size" not in all_ids

    # Wildcards in the query are literal, not LIKE wildcards
    assert "a_other" not in [a["atom_id"] for a in await store.search_l1_atoms(query="%%%")]
    assert "a_other" not in [a["atom_id"] for a in await store.search_l1_atoms(query="Unre_ated")]

    # Empty query keeps returning everything
    assert {"a_asml", "a_other"} <= {a["atom_id"] for a in await store.search_l1_atoms()}

    # Skills: sentence query matches via individual words
    skills = await store.search_skills(query="how do I avoid commission erosion on small orders?")
    assert any(s["skill_id"] == "ibkr-tiered-fee-mitigation" for s in skills)
    assert await store.search_skills(query="%%%") == []


@pytest.mark.asyncio
async def test_extractor_metadata_dedup_and_fixed_skill_id(temp_db_path, monkeypatch):
    import src.memory.extractor as ext
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()
    monkeypatch.setattr(ext, "memory_store", store)

    text = ("User: should I SELL AAPL?\nAssistant: Deliberation: pullback limit entry looks fine; "
            "veto if fee drag too high.")
    r1 = await AssetExtractor.extract_from_text(
        text, metadata={"ticker": "ASML.AS", "action": "BUY", "consensus_score": 82}
    )
    verdicts = [a for a in r1["extracted_atoms"] if a["predicate"] == "committee_verdict"]
    assert len(verdicts) == 1
    assert verdicts[0]["subject"] == "ASML.AS"
    assert "BUY (Score: 82)" in verdicts[0]["object"]

    # No metadata: a verdict must not be taken from "User:" lines
    r2 = await AssetExtractor.extract_from_text("User: SELL AAPL now\nAssistant: Acknowledged.", sync_to_store=False)
    assert not [a for a in r2["extracted_atoms"] if a["predicate"] == "committee_verdict"]

    await AssetExtractor.extract_from_text(text)
    skills = [s for s in await store.search_skills(query="pullback", top_k=50) if s["skill_id"].startswith("skill-pullback")]
    assert len(skills) == 1

    wisdom = (await store.get_core_profile())["cumulative_wisdom"]
    bullet = "Enforce strict Risk Officer veto"
    assert wisdom.count(bullet) == 1


@pytest.mark.asyncio
async def test_extractor_filters_stop_words_and_upserts_deterministically(temp_db_path, monkeypatch):
    import src.memory.extractor as ext
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()
    monkeypatch.setattr(ext, "memory_store", store)

    # Trigger extraction must extract BESI.AS (not "for" or "entry" or "LIMIT_PULLBACK")
    r1 = await AssetExtractor.extract_from_text(
        "Armed order trigger #96 for BESI.AS (LIMIT_PULLBACK) at target price €175.88. "
        "Est. IBKR round-trip commission: €1.79 (0.7% drag). "
        "Enforced IBKR fee drag limit 3.8%.",
        metadata={"ticker": "BESI.AS", "action": "BUY", "consensus_score": 85}
    )
    subjects = [a["subject"] for a in r1["extracted_atoms"]]
    assert "BESI.AS" in subjects
    for bad in ("for", "entry", "LIMIT", "LIMIT_PULLBACK", "Hit", "trigger"):
        assert bad not in subjects

    # Routine per-trade commission (€1.79 / 0.7%) is skipped; the 3.8% fee rule is extracted once
    fee_atoms = [a for a in r1["extracted_atoms"] if a["subject"] == "IBKR_Broker_Fee"]
    assert len(fee_atoms) == 1
    assert fee_atoms[0]["object"] == "3.8%"

    # Repeated extraction upserts the same deterministic atom_ids instead of multiplying rows
    await AssetExtractor.extract_from_text(
        "Armed order trigger #96 for BESI.AS (LIMIT_PULLBACK) at target price €174.50. "
        "Enforced IBKR fee drag limit 3.8%.",
        metadata={"ticker": "BESI.AS", "action": "HOLD", "consensus_score": 60}
    )
    besi_verdicts = [
        a for a in await store.search_l1_atoms(query="BESI.AS", limit=50)
        if a["subject"] == "BESI.AS" and a["predicate"] == "committee_verdict"
    ]
    assert len(besi_verdicts) == 1
    assert "HOLD (Score: 60)" in besi_verdicts[0]["object"]


@pytest.mark.asyncio
async def test_l1_ranking_and_prune_and_compact(temp_db_path):
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()

    # Insert an older query-matching episodic atom and a newer generic instruction atom
    await store.add_l1_atom(L1Atom(
        atom_id="ep_asml", type="episodic", subject="ASML.AS",
        predicate="committee_verdict", object="BUY (Score: 88)",
        content="Committee BUY on ASML.AS.", created_at=100.0
    ))
    await store.add_l1_atom(L1Atom(
        atom_id="inst_generic", type="instruction", subject="GeneralRule",
        predicate="rule", object="always check risk",
        content="Always check risk limits.", created_at=999999.0
    ))
    # Direct token match (ASML.AS) must rank above the newer unrelated instruction atom
    ranked = await store.search_l1_atoms(query="ASML.AS", limit=5)
    assert ranked[0]["atom_id"] == "ep_asml"

    # Insert stop-word/test pollution and verify prune_and_compact removes it
    await store.add_l1_atom(L1Atom(
        atom_id="bad_for", type="episodic", subject="for",
        predicate="trigger_price_level", object="€100", content="Trigger for for"
    ))
    await store.add_l1_atom(L1Atom(
        atom_id="bad_test", type="episodic", subject="NVDA_FEE_TEST",
        predicate="committee_verdict", object="BUY", content="Test atom"
    ))
    stats = await store.prune_and_compact(max_l0_rows=10)
    assert stats["l1_deleted"] >= 2
    remaining_subjects = {a["subject"] for a in await store.search_l1_atoms(limit=100)}
    assert "for" not in remaining_subjects
    assert "NVDA_FEE_TEST" not in remaining_subjects

    # Verify CodeGraph search finds seeded symbols
    cg = await store.search_codegraph(query="LiveBroker")
    assert any(n["symbol_name"] == "LiveBroker" for n in cg)


@pytest.mark.asyncio
async def test_cold_start_scenario_matching_and_token_budget(temp_db_path, monkeypatch):
    import src.memory.cold_start as cs_mod
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()
    monkeypatch.setattr(cs_mod, "memory_store", store)
    monkeypatch.setattr(ColdStartEngine, "_portfolio_cache", None)
    monkeypatch.setattr("src.trading.broker.get_active_broker", lambda: _FakeBroker())

    await store.update_scenario(L2Scenario(
        scenario_key="evaluation-asml.as", title="Committee Deliberation on ASML.AS",
        summary="Deliberated BUY on ASML.AS (Score 85/100).", context_content="Details",
        active_entities=["ASML.AS", "BUY"]
    ))
    await store.update_scenario(L2Scenario(
        scenario_key="evaluation-crm", title="Committee Deliberation on CRM",
        summary="Deliberated HOLD on CRM (Score 65/100).", context_content="CRM details",
        active_entities=["CRM", "HOLD"]
    ))

    # Querying ASML.AS selects ASML.AS scenario even though CRM was updated more recently
    loadout_asml = await ColdStartEngine.generate_cold_start_loadout(query="ASML.AS", include_lessons=False)
    assert "ASML.AS" in loadout_asml.l2_active_scenario
    assert "CRM" not in loadout_asml.l2_active_scenario
    # Skills fall back to core playbooks even when query is a ticker symbol
    assert len(loadout_asml.active_skills) >= 1
    # Token-optimized prompt stays compact (< 2400 chars)
    assert len(loadout_asml.full_prompt_context) < 2400

    # Querying a ticker with no scenario does not leak CRM's scenario into the prompt
    loadout_nvda = await ColdStartEngine.generate_cold_start_loadout(query="NVDA", include_lessons=False)
    assert "CRM" not in loadout_nvda.l2_active_scenario

    # Short 1-char ticker "V" must match evaluation-v, never "evaluation-asml.as" (which has 'v' in 'evaluation')
    await store.update_scenario(L2Scenario(
        scenario_key="evaluation-v", title="Committee Deliberation on V",
        summary="Deliberated HOLD on V (Score 69/100).", context_content="V details",
        active_entities=["V", "HOLD"]
    ))
    await store.update_scenario(L2Scenario(
        scenario_key="evaluation-asml.as", title="Committee Deliberation on ASML.AS",
        summary="Deliberated BUY on ASML.AS (Score 85/100).", context_content="Details",
        active_entities=["ASML.AS", "BUY"]
    ))
    loadout_v = await ColdStartEngine.generate_cold_start_loadout(query="V", include_lessons=False)
    assert "Deliberation on V" in loadout_v.l2_active_scenario
    assert "ASML.AS" not in loadout_v.l2_active_scenario

    # Conversational sentence query matches CRM scenario
    loadout_sentence = await ColdStartEngine.generate_cold_start_loadout(
        query="What should we do with CRM today?", include_lessons=False
    )
    assert "Deliberation on CRM" in loadout_sentence.l2_active_scenario


@pytest.mark.asyncio
async def test_concurrent_init_and_ast_codegraph_and_short_ticker_search(temp_db_path):
    import asyncio
    import ast
    from src.memory.store import REPO_ROOT

    # 1. Concurrent calls on an uninitialized store must not raise 'database is locked'
    store = AgentMemoryStore(db_path=temp_db_path)
    l3, scenarios, atoms, skills, wiki, cg = await asyncio.gather(
        store.get_core_profile(),
        store.list_scenarios(),
        store.search_l1_atoms(),
        store.search_skills(),
        store.list_wiki_pages(),
        store.list_codegraph(limit=30),
    )
    assert "Committee" in l3["persona_name"]
    assert len(cg) >= 15

    # 2. Every seeded CodeGraphNode must exist in the AST with exact (lineno, end_lineno)
    for node in cg:
        fp = REPO_ROOT / node["file_path"]
        assert fp.exists(), f"Missing file {node['file_path']}"
        tree = ast.parse(fp.read_text(encoding="utf-8"))
        matched = [
            n for n in ast.walk(tree)
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == node["symbol_name"]
        ]
        assert matched, f"Symbol {node['symbol_name']} not found in {node['file_path']}"
        assert (node["line_start"], node["line_end"]) == (matched[0].lineno, matched[0].end_lineno)

    # 3. Short ticker 'V' and conversational query with 'with' must not match unrelated verdict prose
    await store.add_l1_atom(L1Atom(
        atom_id="v_atom", type="episodic", subject="V", predicate="committee_verdict",
        object="HOLD (Score: 69)",
        content="Investment Committee rendered HOLD verdict on V with consensus conviction of 69/100.",
        created_at=100.0
    ))
    await store.add_l1_atom(L1Atom(
        atom_id="jpm_atom", type="episodic", subject="JPM", predicate="committee_verdict",
        object="HOLD (Score: 69)",
        content="Investment Committee rendered HOLD verdict on JPM with consensus conviction of 69/100.",
        created_at=500.0
    ))
    v_res = await store.search_l1_atoms(query="V", limit=5)
    assert v_res[0]["subject"] == "V"
    assert "JPM" not in [a["subject"] for a in v_res]

    jpm_sentence_res = await store.search_l1_atoms(query="What should we do with V today?", limit=5)
    assert jpm_sentence_res[0]["subject"] == "V"
    assert "JPM" not in [a["subject"] for a in jpm_sentence_res]


@pytest.mark.asyncio
async def test_extractor_skips_hold_triggers_and_trade_fill_verdicts(temp_db_path, monkeypatch):
    import src.memory.extractor as ext
    store = AgentMemoryStore(db_path=temp_db_path)
    await store.init_db()
    monkeypatch.setattr(ext, "memory_store", store)

    # 1. HOLD deliberation with technical breakout commentary and 'Portfolio Manager:' speaker label
    hold_delib = (
        "Deliberation on OR.PA (HOLD, Score 75/100):\n"
        "**[Technical Analyst]**: \"OR.PA presents a setup following a breakout above its pivot at 392.15 EUR.\"\n"
        "**[Portfolio Manager]**: \"Consensus is HOLD. AI Trigger: LIMIT_PULLBACK @ 392.15 EUR.\""
    )
    res_hold = await AssetExtractor.extract_from_text(
        hold_delib, metadata={"ticker": "OR.PA", "action": "HOLD", "consensus_score": 75}, sync_to_store=False
    )
    preds = [a["predicate"] for a in res_hold["extracted_atoms"]]
    assert "committee_verdict" in preds
    assert "trigger_price_level" not in preds
    assert "account_balance_state" not in preds

    # 2. Trade fill execution log must record trade_executed, not fabricate a committee_verdict or €1.90 fee rule
    fill_text = "Executed BUY on 3.0x TTE.PA at 80.76 EUR with fee of €1.90 . Rationale: Live Trigger #109 Hit"
    res_fill = await AssetExtractor.extract_from_text(
        fill_text, metadata={"event": "trade_execution", "ticker": "TTE.PA"}, sync_to_store=False
    )
    fill_preds = [a["predicate"] for a in res_fill["extracted_atoms"]]
    assert "trade_executed" in fill_preds
    assert "committee_verdict" not in fill_preds
    assert "fee_constraint_observed" not in fill_preds


@pytest.mark.asyncio
async def test_agents_md_and_production_codegraph_ast_sync(temp_db_path):
    import ast
    import re
    import sqlite3
    import aiosqlite
    from src.memory.store import REPO_ROOT, DB_PATH, AgentMemoryStore, memory_store

    agents_md_path = REPO_ROOT / "AGENTS.md"
    assert agents_md_path.exists(), "AGENTS.md must exist at repository root"
    text = agents_md_path.read_text(encoding="utf-8")

    # Parse every file link and its optional (`symbol:start-end`, ...) block
    link_pattern = re.compile(
        r"\[[^\]]+\]\(file:///home/tdapper/TradingBotNew/([^)]+)\)\s*(?:\(([^)]+)\))?"
    )
    sym_pattern = re.compile(r"^([A-Za-z0-9_]+):(\d+)-(\d+)$")

    ast_cache = {}

    def _get_ast_ranges(rel_path: str):
        if rel_path not in ast_cache:
            fp = REPO_ROOT / rel_path
            tree = ast.parse(fp.read_text(encoding="utf-8"))
            by_name = {}
            for n in ast.walk(tree):
                if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    by_name.setdefault(n.name, []).append((n.lineno, n.end_lineno))
            ast_cache[rel_path] = by_name
        return ast_cache[rel_path]

    checked_symbols = 0
    for m in link_pattern.finditer(text):
        rel_path, sym_block = m.group(1), m.group(2)
        fp = REPO_ROOT / rel_path
        assert fp.exists(), f"AGENTS.md references non-existent file: {rel_path}"
        if not sym_block or fp.suffix != ".py":
            continue
        nodes = _get_ast_ranges(rel_path)
        tokens = re.findall(r"`([^`]+)`", sym_block)
        assert tokens, f"Empty parenthetical block for {rel_path} in AGENTS.md"
        for tok in tokens:
            if "/" in tok:
                assert (REPO_ROOT / tok).exists(), f"Referenced path {tok} does not exist"
                continue
            sym_m = sym_pattern.match(tok)
            assert sym_m is not None, (
                f"Symbol `{tok}` for {rel_path} in AGENTS.md is missing an explicit `:start-end` line range"
            )
            sym, start_line, end_line = sym_m.group(1), int(sym_m.group(2)), int(sym_m.group(3))
            assert sym in nodes, f"Symbol {sym} not found in {rel_path}"
            assert (start_line, end_line) in nodes[sym], (
                f"AGENTS.md drift for {rel_path}::{sym}: expected {nodes[sym]}, got {(start_line, end_line)}"
            )
            checked_symbols += 1

    assert checked_symbols >= 45, f"Expected at least 45 symbol ranges in AGENTS.md, found {checked_symbols}"

    # Verify the persisted production SQLite database (data/memory.db, not conftest's test_memory.db)
    assert DB_PATH.exists(), f"Production memory DB missing at {DB_PATH}"
    with sqlite3.connect(str(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT symbol_name, file_path, line_start, line_end FROM memory_codegraph"
        ).fetchall()
    assert len(rows) >= 15, f"Expected >= 15 codegraph rows in {DB_PATH}, found {len(rows)}"
    for sym_name, rel_path, l_start, l_end in rows:
        expected_ranges = _get_ast_ranges(rel_path).get(sym_name, [])
        assert (l_start, l_end) in expected_ranges, (
            f"data/memory.db memory_codegraph drift for {rel_path}::{sym_name}: "
            f"expected {expected_ranges}, got {(l_start, l_end)}"
        )

    # Also verify active memory_store.list_codegraph() without calling refresh_codegraph() beforehand
    cg = await memory_store.list_codegraph(limit=50)
    assert len(cg) >= 15
    for node in cg:
        rel_path = node["file_path"]
        expected_ranges = _get_ast_ranges(rel_path).get(node["symbol_name"], [])
        assert (node["line_start"], node["line_end"]) in expected_ranges, (
            f"memory_codegraph drift for {rel_path}::{node['symbol_name']}: "
            f"expected {expected_ranges}, got {(node['line_start'], node['line_end'])}"
        )

    # Verify that if a store has drifted line numbers in memory_codegraph, init_db() auto-repairs it
    drift_store = AgentMemoryStore(db_path=temp_db_path)
    await drift_store.init_db()
    async with aiosqlite.connect(str(temp_db_path)) as db:
        await db.execute("UPDATE memory_codegraph SET line_end = 999999 WHERE symbol_name = 'AgentMemoryStore'")
        await db.commit()
    reopened_store = AgentMemoryStore(db_path=temp_db_path)
    repaired_cg = await reopened_store.list_codegraph(limit=50)
    ams_node = next(n for n in repaired_cg if n["symbol_name"] == "AgentMemoryStore")
    assert (ams_node["line_start"], ams_node["line_end"]) in _get_ast_ranges("src/memory/store.py")["AgentMemoryStore"]

