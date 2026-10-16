import time
import logging
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, Query, HTTPException
from fastapi.responses import JSONResponse

from src.memory.models import (
    ApiResponse,
    AddConversationRequest, QueryConversationRequest,
    AddAtomRequest, SearchAtomsRequest, L1Atom,
    UpdateScenarioRequest, L2Scenario,
    UpdateCoreProfileRequest, L3CoreProfile,
    CreateSkillRequest, SearchSkillsRequest, SkillAsset,
    CreateWikiRequest, WikiPage,
    ChatMemoryContextRequest, ColdStartLoadout,
    ExtractAssetsRequest
)
from src.memory.store import memory_store
from src.memory.cold_start import cold_start_engine
from src.memory.extractor import asset_extractor

logger = logging.getLogger(__name__)

memory_router = APIRouter(tags=["TencentDB Agent Memory"])

START_TIME = time.time()

# --- Health Check (Standard & TencentDB compatible) ---

@memory_router.get("/health")
async def health_check():
    import asyncio
    await memory_store.init_db()
    atoms, skills, wiki, scenarios = await asyncio.gather(
        memory_store.search_l1_atoms(limit=1),
        memory_store.search_skills(top_k=50),
        memory_store.list_wiki_pages(),
        memory_store.list_scenarios(),
    )
    return {
        "status": "ok",
        "version": "2.0.0-tencentdb-agent-memory",
        "service": "TencentDB-Agent-Memory-Hub",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "stores": {
            "backend": "sqlite-wal",
            "db_path": str(memory_store.db_path)
        },
        "assets_summary": {
            "l1_atoms_active": len(atoms) > 0,
            "skills_count": len(skills),
            "wiki_pages_count": len(wiki),
            "scenarios_count": len(scenarios)
        }
    }

# --- Cold Start & Composite Context ---

@memory_router.post("/v3/chat-memory/context", response_model=ApiResponse)
async def get_chat_memory_context(req: ChatMemoryContextRequest):
    """
    TencentDB Agent Memory v3 Chat-Memory Context Assembly.
    Returns the composite memory context block (L1+L2+L3+Skills+Wiki)
    to inject into the agent's context window.
    """
    loadout = await cold_start_engine.generate_cold_start_loadout(
        team_id=req.team_id,
        agent_id=req.agent_id,
        query=req.query,
        char_budget=req.char_budget
    )
    return ApiResponse(data=loadout.model_dump())

@memory_router.get("/v3/memory/cold-start", response_model=ApiResponse)
async def get_cold_start(team_id: str = "trading-team", agent_id: str = "committee"):
    """
    Instant Cold-Start Situation Report:
    Wakes up the AI Agent with an instant recap of what happened,
    the live portfolio status, and active memory loadouts.
    """
    loadout = await cold_start_engine.generate_cold_start_loadout(
        team_id=team_id,
        agent_id=agent_id
    )
    return ApiResponse(data=loadout.model_dump())

# --- L0: Raw Conversation ---

@memory_router.post("/v3/conversation/add", response_model=ApiResponse)
async def add_conversation(req: AddConversationRequest):
    """
    Appends raw conversation messages to L0, and automatically triggers
    the asynchronous asset extraction pipeline.
    """
    ids = await memory_store.add_l0_messages(
        session_id=req.session_id,
        messages=req.messages,
        team_id=req.team_id,
        agent_id=req.agent_id,
        user_id=req.user_id,
        task_id=req.task_id
    )

    # Asynchronously trigger asset extraction
    combined_text = "\n".join(f"{m.role}: {m.content}" for m in req.messages)
    import asyncio
    asyncio.create_task(
        asset_extractor.extract_from_text(
            text=combined_text,
            session_id=req.session_id,
            team_id=req.team_id,
            agent_id=req.agent_id,
            sync_to_store=True
        )
    )

    return ApiResponse(data={
        "accepted_ids": ids,
        "total_count": len(ids),
        "session_id": req.session_id
    })

@memory_router.post("/v3/conversation/query", response_model=ApiResponse)
@memory_router.post("/v3/conversation/search", response_model=ApiResponse)
async def query_conversation(req: QueryConversationRequest):
    rows = await memory_store.query_l0(
        session_id=req.session_id,
        team_id=req.team_id,
        agent_id=req.agent_id,
        limit=req.limit,
        offset=req.offset
    )
    return ApiResponse(data={"items": rows, "total": len(rows)})

# --- L1: Atomic Memory ---

@memory_router.post("/v3/atomic/add", response_model=ApiResponse)
async def add_atomic_memory(req: AddAtomRequest):
    atom = L1Atom(
        team_id=req.team_id,
        agent_id=req.agent_id,
        type=req.type,
        subject=req.subject,
        predicate=req.predicate,
        object=req.object,
        content=req.content,
        confidence=req.confidence,
        source_session_id=req.source_session_id
    )
    atom_id = await memory_store.add_l1_atom(atom)
    return ApiResponse(data={"atom_id": atom_id, "atom": atom.model_dump()})

@memory_router.post("/v3/atomic/list", response_model=ApiResponse)
@memory_router.post("/v3/atomic/search", response_model=ApiResponse)
async def search_atomic_memory(req: SearchAtomsRequest):
    atoms = await memory_store.search_l1_atoms(
        query=req.query,
        atom_type=req.type,
        team_id=req.team_id,
        limit=req.limit
    )
    return ApiResponse(data={"items": atoms, "total": len(atoms)})

# --- L2: Scenario Memory ---

@memory_router.post("/v3/scenario/get", response_model=ApiResponse)
async def get_scenario(req: Dict[str, Any]):
    scenario_key = req.get("scenario_key", "default-session")
    sc = await memory_store.get_or_create_scenario(scenario_key=scenario_key)
    return ApiResponse(data=sc)

@memory_router.post("/v3/scenario/list", response_model=ApiResponse)
async def list_scenarios(req: Optional[Dict[str, Any]] = None):
    team_id = req.get("team_id", "trading-team") if req else "trading-team"
    scs = await memory_store.list_scenarios(team_id=team_id)
    return ApiResponse(data={"items": scs, "total": len(scs)})

@memory_router.post("/v3/scenario/update", response_model=ApiResponse)
async def update_scenario(req: UpdateScenarioRequest):
    sc = L2Scenario(
        scenario_key=req.scenario_key,
        title=req.title,
        team_id=req.team_id,
        agent_id=req.agent_id,
        summary=req.summary,
        context_content=req.context_content,
        active_entities=req.active_entities or []
    )
    await memory_store.update_scenario(sc)
    return ApiResponse(data={"scenario_key": sc.scenario_key, "updated": True})

# --- L3: Core Profile / Persona ---

@memory_router.post("/v3/core/get", response_model=ApiResponse)
async def get_core_profile(req: Optional[Dict[str, Any]] = None):
    team_id = req.get("team_id", "trading-team") if req else "trading-team"
    agent_id = req.get("agent_id", "committee") if req else "committee"
    prof = await memory_store.get_core_profile(team_id=team_id, agent_id=agent_id)
    return ApiResponse(data=prof)

@memory_router.post("/v3/core/update", response_model=ApiResponse)
async def update_core_profile(req: UpdateCoreProfileRequest):
    current = await memory_store.get_core_profile(team_id=req.team_id, agent_id=req.agent_id)
    updated = L3CoreProfile(
        team_id=req.team_id,
        agent_id=req.agent_id,
        persona_name=req.persona_name or current["persona_name"],
        core_profile=req.core_profile or current["core_profile"],
        cumulative_wisdom=req.cumulative_wisdom or current["cumulative_wisdom"],
        risk_rules=req.risk_rules if req.risk_rules is not None else current["risk_rules"]
    )
    await memory_store.update_core_profile(updated)
    return ApiResponse(data={"updated": True, "agent_id": req.agent_id})

# --- Skills ---

@memory_router.post("/v3/skill/list", response_model=ApiResponse)
@memory_router.post("/v3/skill/search", response_model=ApiResponse)
async def search_skills(req: Optional[SearchSkillsRequest] = None):
    query = req.query if req else None
    cat = req.category if req else None
    team_id = req.team_id if req else "trading-team"
    top_k = req.top_k if req else 20
    skills = await memory_store.search_skills(query=query, category=cat, team_id=team_id, top_k=top_k)
    return ApiResponse(data={"items": skills, "total": len(skills)})

@memory_router.post("/v3/skill/create", response_model=ApiResponse)
@memory_router.post("/v3/skill/add", response_model=ApiResponse)
async def create_skill(req: CreateSkillRequest):
    sk = SkillAsset(
        skill_id=req.skill_id,
        name=req.name,
        team_id=req.team_id,
        category=req.category,
        description=req.description,
        trigger_conditions=req.trigger_conditions,
        execution_steps=req.execution_steps,
        code_template=req.code_template
    )
    await memory_store.add_skill(sk)
    return ApiResponse(data={"skill_id": sk.skill_id, "created": True})

# --- Knowledge: Wiki & CodeGraph ---

@memory_router.post("/v3/knowledge/list", response_model=ApiResponse)
async def list_knowledge(req: Optional[Dict[str, Any]] = None):
    team_id = req.get("team_id", "trading-team") if req else "trading-team"
    wiki_pages = await memory_store.list_wiki_pages(team_id=team_id)
    codegraph = await memory_store.list_codegraph(limit=20)
    return ApiResponse(data={
        "wiki_pages": wiki_pages,
        "codegraph_nodes": codegraph,
        "total_wiki": len(wiki_pages),
        "total_codegraph": len(codegraph)
    })

@memory_router.post("/v3/knowledge/create", response_model=ApiResponse)
@memory_router.post("/v3/knowledge/add", response_model=ApiResponse)
async def create_wiki_page(req: CreateWikiRequest):
    page = WikiPage(
        page_id=req.page_id,
        team_id=req.team_id,
        title=req.title,
        content_md=req.content_md,
        tags=req.tags or [],
        links=req.links or []
    )
    await memory_store.add_wiki_page(page)
    return ApiResponse(data={"page_id": page.page_id, "created": True})

@memory_router.post("/v3/codegraph/search", response_model=ApiResponse)
@memory_router.get("/v3/codegraph/search", response_model=ApiResponse)
async def search_codegraph(query: Optional[str] = None, limit: int = 20):
    nodes = await memory_store.search_codegraph(query=query, limit=limit)
    return ApiResponse(data={"items": nodes, "total": len(nodes)})

@memory_router.post("/v3/memory/compact", response_model=ApiResponse)
async def compact_memory(max_l0_rows: int = 150):
    stats = await memory_store.prune_and_compact(max_l0_rows=max_l0_rows)
    return ApiResponse(data=stats)

# --- Unified Assets Overview ---

@memory_router.get("/v3/memory/assets", response_model=ApiResponse)
async def get_all_assets(team_id: str = "trading-team", agent_id: str = "committee"):
    """Returns the unified 4-asset catalog (Chat Memory L0-L3, Skills, Wiki, CodeGraph)."""
    import asyncio
    await memory_store.init_db()
    l3, scenarios, atoms, skills, wiki, codegraph = await asyncio.gather(
        memory_store.get_core_profile(team_id=team_id, agent_id=agent_id),
        memory_store.list_scenarios(team_id=team_id),
        memory_store.search_l1_atoms(team_id=team_id, limit=30),
        memory_store.search_skills(team_id=team_id, top_k=20),
        memory_store.list_wiki_pages(team_id=team_id),
        memory_store.list_codegraph(limit=30),
    )

    return ApiResponse(data={
        "chat_memory": {
            "l3_core": l3,
            "l2_scenarios": scenarios,
            "l1_atoms": atoms
        },
        "skills": skills,
        "llm_wiki": wiki,
        "code_graph": codegraph
    })

# --- Explicit Asset Extraction ---

@memory_router.post("/v3/memory/extract", response_model=ApiResponse)
async def extract_assets_endpoint(req: ExtractAssetsRequest):
    """
    Explicitly runs the asset extraction pipeline on given text or session.
    """
    text = req.raw_text or ""
    if not text and req.session_id:
        msgs = await memory_store.query_l0(session_id=req.session_id, team_id=req.team_id, limit=20)
        # query_l0 returns newest first; the transcript must read chronologically
        text = "\n".join(f"{m['role']}: {m['content']}" for m in reversed(msgs))

    result = await asset_extractor.extract_from_text(
        text=text,
        session_id=req.session_id,
        team_id=req.team_id,
        agent_id=req.agent_id,
        sync_to_store=req.sync_to_store
    )
    return ApiResponse(data=result)

# --- Hermes / OpenClaw Compatibility Endpoints ---

@memory_router.post("/recall")
async def hermes_recall(req: Dict[str, Any]):
    query = req.get("query", "")
    limit = req.get("limit", 5)
    atoms = await memory_store.search_l1_atoms(query=query, limit=limit)
    return {"status": "ok", "memories": atoms}

@memory_router.post("/capture")
async def hermes_capture(req: Dict[str, Any]):
    text = req.get("text", "") or req.get("content", "")
    session_id = req.get("session_id")
    res = await asset_extractor.extract_from_text(
        text=text,
        session_id=session_id,
        sync_to_store=True
    )
    return {"status": "ok", "extracted": res}
