import time
from typing import Dict, Any, List, Optional, Literal
from pydantic import BaseModel, Field

# --- TencentDB Agent Memory v3 Response Envelope ---

class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    request_id: str = Field(default_factory=lambda: f"req-{int(time.time()*1000)}")
    data: Optional[Any] = None

# --- L0: Raw Conversation ---

class L0Message(BaseModel):
    role: Literal["user", "assistant", "system", "agent"]
    content: str
    timestamp: Optional[float] = Field(default_factory=time.time)
    metadata: Optional[Dict[str, Any]] = None

class AddConversationRequest(BaseModel):
    session_id: str
    messages: List[L0Message]
    team_id: str = "trading-team"
    agent_id: str = "committee"
    user_id: str = "default"
    task_id: Optional[str] = None

class QueryConversationRequest(BaseModel):
    session_id: Optional[str] = None
    query: Optional[str] = None
    limit: int = 20
    offset: int = 0
    team_id: str = "trading-team"
    agent_id: Optional[str] = None

# --- L1: Atomic Memory (Facts, Events, Instructions) ---

class L1Atom(BaseModel):
    id: Optional[int] = None
    atom_id: Optional[str] = None
    team_id: str = "trading-team"
    agent_id: str = "committee"
    type: Literal["episodic", "persona", "instruction"] = "episodic"
    subject: str
    predicate: str
    object: str
    content: str
    confidence: float = 1.0
    source_session_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

class AddAtomRequest(BaseModel):
    team_id: str = "trading-team"
    agent_id: str = "committee"
    type: Literal["episodic", "persona", "instruction"] = "episodic"
    subject: str
    predicate: str
    object: str
    content: str
    confidence: float = 1.0
    source_session_id: Optional[str] = None

class SearchAtomsRequest(BaseModel):
    query: Optional[str] = None
    type: Optional[str] = None
    team_id: str = "trading-team"
    agent_id: Optional[str] = None
    limit: int = 20

# --- L2: Scenario Memory (Context Blocks) ---

class L2Scenario(BaseModel):
    scenario_key: str
    title: str
    team_id: str = "trading-team"
    agent_id: str = "committee"
    summary: str
    context_content: str
    active_entities: List[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)

class UpdateScenarioRequest(BaseModel):
    scenario_key: str
    title: str
    team_id: str = "trading-team"
    agent_id: str = "committee"
    summary: str
    context_content: str
    active_entities: Optional[List[str]] = None

# --- L3: Core Profile / Persona ---

class L3CoreProfile(BaseModel):
    team_id: str = "trading-team"
    agent_id: str = "committee"
    persona_name: str = "Hedge Fund PM & Committee"
    core_profile: str
    cumulative_wisdom: str
    risk_rules: List[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)

class UpdateCoreProfileRequest(BaseModel):
    team_id: str = "trading-team"
    agent_id: str = "committee"
    persona_name: Optional[str] = None
    core_profile: Optional[str] = None
    cumulative_wisdom: Optional[str] = None
    risk_rules: Optional[List[str]] = None

# --- Skill Asset ---

class SkillAsset(BaseModel):
    skill_id: str
    name: str
    team_id: str = "trading-team"
    category: str = "trading"
    description: str
    trigger_conditions: List[str] = Field(default_factory=list)
    execution_steps: List[str] = Field(default_factory=list)
    code_template: Optional[str] = None
    version: str = "1.0.0"
    created_at: float = Field(default_factory=time.time)

class CreateSkillRequest(BaseModel):
    skill_id: str
    name: str
    team_id: str = "trading-team"
    category: str = "trading"
    description: str
    trigger_conditions: List[str]
    execution_steps: List[str]
    code_template: Optional[str] = None

class SearchSkillsRequest(BaseModel):
    query: Optional[str] = None
    category: Optional[str] = None
    team_id: str = "trading-team"
    top_k: int = 10

# --- Knowledge: Wiki Asset ---

class WikiPage(BaseModel):
    page_id: str
    team_id: str = "trading-team"
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    links: List[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)

class CreateWikiRequest(BaseModel):
    page_id: str
    team_id: str = "trading-team"
    title: str
    content_md: str
    tags: Optional[List[str]] = None
    links: Optional[List[str]] = None

# --- Knowledge: CodeGraph Asset ---

class CodeGraphNode(BaseModel):
    symbol_name: str
    symbol_type: str  # function, class, module
    file_path: str
    line_start: int
    line_end: int
    docstring: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    callers: List[str] = Field(default_factory=list)

# --- Composite Context / Cold-Start ---

class ChatMemoryContextRequest(BaseModel):
    query: Optional[str] = ""
    team_id: str = "trading-team"
    agent_id: str = "committee"
    user_id: str = "default"
    session_id: Optional[str] = None
    char_budget: int = 3200
    include_skills: bool = True
    include_wiki: bool = True
    include_portfolio: bool = True

class ColdStartLoadout(BaseModel):
    status_summary: str
    what_had_happened: str
    l3_core_profile: str
    l2_active_scenario: str
    l1_key_facts: List[str]
    active_skills: List[str]
    wiki_highlights: List[str]
    portfolio_state: Dict[str, Any]
    full_prompt_context: str
    timestamp: float = Field(default_factory=time.time)

# --- Asset Extraction Request ---

class ExtractAssetsRequest(BaseModel):
    session_id: Optional[str] = None
    raw_text: Optional[str] = None
    team_id: str = "trading-team"
    agent_id: str = "committee"
    sync_to_store: bool = True
