import os
import re
import json
import time
import logging
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
import aiosqlite
from pathlib import Path

from src.memory.models import (
    L0Message, L1Atom, L2Scenario, L3CoreProfile, SkillAsset, WikiPage, CodeGraphNode
)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "memory.db"

def _query_tokens(query: str, max_tokens: int = 12) -> List[str]:
    """Splits a free-text query into unique search words (len >= 3). Falls back to the whole query."""
    tokens: List[str] = []
    for raw in re.findall(r"[A-Za-z0-9\u20ac][A-Za-z0-9\u20ac._-]*", query or ""):
        tok = raw.strip("._-")
        if len(tok) >= 3 and tok.lower() not in (t.lower() for t in tokens):
            tokens.append(tok)
    if not tokens and query and query.strip():
        tokens = [query.strip()]
    return tokens[:max_tokens]


def _like_pattern(token: str) -> str:
    """Builds a LIKE pattern for a token, escaping %, _ and the escape char itself (use with ESCAPE '\\')."""
    esc = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class AgentMemoryStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    @asynccontextmanager
    async def _get_conn(self):
        async with aiosqlite.connect(str(self.db_path)) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            yield conn

    async def init_db(self):
        if self._initialized:
            return
        async with self._get_conn() as db:
            # 1. L0 Raw Conversations
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_l0_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    task_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at REAL NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_l0_session ON memory_l0_conversations(session_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_l0_team_agent ON memory_l0_conversations(team_id, agent_id)")

            # 2. L1 Atoms
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_l1_atoms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    atom_id TEXT UNIQUE,
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    type TEXT NOT NULL, -- episodic, persona, instruction
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    content TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_session_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_l1_type ON memory_l1_atoms(type)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_l1_team ON memory_l1_atoms(team_id, agent_id)")

            # 3. L2 Scenarios
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_l2_scenarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_key TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    context_content TEXT NOT NULL,
                    active_entities_json TEXT,
                    updated_at REAL NOT NULL
                )
            """)

            # 4. L3 Core Profile / Persona
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_l3_core (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    persona_name TEXT NOT NULL,
                    core_profile TEXT NOT NULL,
                    cumulative_wisdom TEXT NOT NULL,
                    risk_rules_json TEXT,
                    updated_at REAL NOT NULL,
                    UNIQUE(team_id, agent_id)
                )
            """)

            # 5. Skills
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    trigger_conditions_json TEXT NOT NULL,
                    execution_steps_json TEXT NOT NULL,
                    code_template TEXT,
                    version TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

            # 6. Wiki Pages
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_wiki (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_id TEXT UNIQUE NOT NULL,
                    team_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content_md TEXT NOT NULL,
                    tags_json TEXT,
                    links_json TEXT,
                    updated_at REAL NOT NULL
                )
            """)

            # 7. CodeGraph
            await db.execute("""
                CREATE TABLE IF NOT EXISTS memory_codegraph (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_name TEXT UNIQUE NOT NULL,
                    symbol_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_start INTEGER,
                    line_end INTEGER,
                    docstring TEXT,
                    dependencies_json TEXT,
                    callers_json TEXT
                )
            """)

            await db.commit()

        self._initialized = True
        logger.info("AgentMemoryStore SQLite initialized at %s", self.db_path)
        await self._seed_default_assets_if_empty()

    # --- L0 Conversations ---

    async def add_l0_messages(self, session_id: str, messages: List[L0Message],
                             team_id: str = "trading-team", agent_id: str = "committee",
                             user_id: str = "default", task_id: Optional[str] = None) -> List[int]:
        await self.init_db()
        inserted_ids = []
        now = time.time()
        async with self._get_conn() as db:
            for msg in messages:
                meta = json.dumps(msg.metadata or {})
                cursor = await db.execute("""
                    INSERT INTO memory_l0_conversations 
                    (session_id, team_id, agent_id, user_id, task_id, role, content, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (session_id, team_id, agent_id, user_id, task_id, msg.role, msg.content, meta, msg.timestamp or now))
                inserted_ids.append(cursor.lastrowid)
            await db.commit()
        return inserted_ids

    async def query_l0(self, session_id: Optional[str] = None, team_id: str = "trading-team",
                       agent_id: Optional[str] = None, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        await self.init_db()
        query = "SELECT * FROM memory_l0_conversations WHERE team_id = ?"
        params: List[Any] = [team_id]
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        if agent_id:
            query += " AND agent_id = ?"
            params.append(agent_id)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with self._get_conn() as db:
            async with await db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    results.append({
                        "id": r["id"],
                        "session_id": r["session_id"],
                        "team_id": r["team_id"],
                        "agent_id": r["agent_id"],
                        "role": r["role"],
                        "content": r["content"],
                        "metadata": json.loads(r["metadata_json"] or "{}"),
                        "created_at": r["created_at"],
                    })
                return results

    # --- L1 Atoms ---

    async def add_l1_atom(self, atom: L1Atom) -> int:
        await self.init_db()
        now = time.time()
        atom_id = atom.atom_id or f"atom_{int(now*1000)}_{os.urandom(3).hex()}"
        async with self._get_conn() as db:
            cursor = await db.execute("""
                INSERT OR REPLACE INTO memory_l1_atoms
                (atom_id, team_id, agent_id, type, subject, predicate, object, content, confidence, source_session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (atom_id, atom.team_id, atom.agent_id, atom.type, atom.subject, atom.predicate, atom.object,
                  atom.content, atom.confidence, atom.source_session_id, atom.created_at or now, now))
            await db.commit()
            return cursor.lastrowid

    async def search_l1_atoms(self, query: Optional[str] = None, atom_type: Optional[str] = None,
                              team_id: str = "trading-team", limit: int = 20) -> List[Dict[str, Any]]:
        await self.init_db()
        sql = "SELECT * FROM memory_l1_atoms WHERE team_id = ?"
        params: List[Any] = [team_id]
        if atom_type:
            sql += " AND type = ?"
            params.append(atom_type)
        if query:
            clauses = []
            for tok in _query_tokens(query):
                clauses.append("content LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\' OR object LIKE ? ESCAPE '\\'")
                pat = _like_pattern(tok)
                params.extend([pat, pat, pat])
            # Instruction atoms (rules/constraints) are always relevant
            clauses.append("type = 'instruction'")
            sql += " AND (" + " OR ".join(clauses) + ")"
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        async with self._get_conn() as db:
            async with await db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]

    # --- L2 Scenarios ---

    async def get_or_create_scenario(self, scenario_key: str, default_title: str = "",
                                     team_id: str = "trading-team", agent_id: str = "committee") -> Dict[str, Any]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute("SELECT * FROM memory_l2_scenarios WHERE scenario_key = ?", (scenario_key,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    res = dict(row)
                    res["active_entities"] = json.loads(res.get("active_entities_json") or "[]")
                    return res

        # Create default scenario
        now = time.time()
        async with self._get_conn() as db:
            await db.execute("""
                INSERT OR REPLACE INTO memory_l2_scenarios
                (scenario_key, title, team_id, agent_id, summary, context_content, active_entities_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (scenario_key, default_title or scenario_key, team_id, agent_id, "Active session context", "", "[]", now))
            await db.commit()
        return {
            "scenario_key": scenario_key,
            "title": default_title or scenario_key,
            "team_id": team_id,
            "agent_id": agent_id,
            "summary": "Active session context",
            "context_content": "",
            "active_entities": [],
            "updated_at": now
        }

    async def update_scenario(self, scenario: L2Scenario) -> None:
        await self.init_db()
        now = time.time()
        entities = json.dumps(scenario.active_entities)
        async with self._get_conn() as db:
            await db.execute("""
                INSERT INTO memory_l2_scenarios (scenario_key, title, team_id, agent_id, summary, context_content, active_entities_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scenario_key) DO UPDATE SET
                    title=excluded.title,
                    summary=excluded.summary,
                    context_content=excluded.context_content,
                    active_entities_json=excluded.active_entities_json,
                    updated_at=excluded.updated_at
            """, (scenario.scenario_key, scenario.title, scenario.team_id, scenario.agent_id,
                  scenario.summary, scenario.context_content, entities, now))
            await db.commit()

    async def list_scenarios(self, team_id: str = "trading-team") -> List[Dict[str, Any]]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute("SELECT * FROM memory_l2_scenarios WHERE team_id = ? ORDER BY updated_at DESC", (team_id,)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    item["active_entities"] = json.loads(item.get("active_entities_json") or "[]")
                    results.append(item)
                return results

    # --- L3 Core Profile / Persona ---

    async def get_core_profile(self, team_id: str = "trading-team", agent_id: str = "committee") -> Dict[str, Any]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute(
                "SELECT * FROM memory_l3_core WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    res = dict(row)
                    res["risk_rules"] = json.loads(res.get("risk_rules_json") or "[]")
                    return res

        # Seed default profile
        now = time.time()
        default_profile = (
            "Institutional Hedge Fund Quantitative & Discretionary Committee. "
            "Governs capital allocation across European and US equities with rigorous risk management, "
            "strict IBKR fee drag mitigation, and dynamic slot consolidation."
        )
        default_wisdom = (
            "1. Avoid micro-allocations: Never trade below €100 on Euronext/XETRA to prevent fee erosion (>3.8% drag).\n"
            "2. Limit Pullbacks: Enter oversold pullbacks on fundamentally resilient balance sheets with limit triggers.\n"
            "3. Absolute Risk Veto: If fee drag exceeds 3.8% or expected net profit is negative, Risk Officer vetoes BUY verdicts."
        )
        default_rules = [
            "Round-trip commission drag must not exceed 3.8% of order size",
            "Minimum order size on Euronext/Xetra is €100.00",
            "Minimum order size on US equities is €35.00",
            "Risk Officer holds unilateral veto over BUY signals"
        ]
        async with self._get_conn() as db:
            await db.execute("""
                INSERT OR IGNORE INTO memory_l3_core
                (team_id, agent_id, persona_name, core_profile, cumulative_wisdom, risk_rules_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (team_id, agent_id, "Hedge Fund PM & Committee", default_profile, default_wisdom, json.dumps(default_rules), now))
            await db.commit()
            # A concurrent writer may have saved a profile first (seed is IGNOREd): prefer what is stored
            async with await db.execute(
                "SELECT * FROM memory_l3_core WHERE team_id = ? AND agent_id = ?",
                (team_id, agent_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    res = dict(row)
                    res["risk_rules"] = json.loads(res.get("risk_rules_json") or "[]")
                    return res

        return {
            "team_id": team_id,
            "agent_id": agent_id,
            "persona_name": "Hedge Fund PM & Committee",
            "core_profile": default_profile,
            "cumulative_wisdom": default_wisdom,
            "risk_rules": default_rules,
            "updated_at": now
        }

    async def update_core_profile(self, profile: L3CoreProfile) -> None:
        await self.init_db()
        now = time.time()
        rules_json = json.dumps(profile.risk_rules)
        async with self._get_conn() as db:
            await db.execute("""
                INSERT INTO memory_l3_core (team_id, agent_id, persona_name, core_profile, cumulative_wisdom, risk_rules_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id, agent_id) DO UPDATE SET
                    persona_name=excluded.persona_name,
                    core_profile=excluded.core_profile,
                    cumulative_wisdom=excluded.cumulative_wisdom,
                    risk_rules_json=excluded.risk_rules_json,
                    updated_at=excluded.updated_at
            """, (profile.team_id, profile.agent_id, profile.persona_name, profile.core_profile, profile.cumulative_wisdom, rules_json, now))
            await db.commit()

    # --- Skills ---

    async def add_skill(self, skill: SkillAsset) -> None:
        await self.init_db()
        now = time.time()
        async with self._get_conn() as db:
            await db.execute("""
                INSERT OR REPLACE INTO memory_skills
                (skill_id, name, team_id, category, description, trigger_conditions_json, execution_steps_json, code_template, version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (skill.skill_id, skill.name, skill.team_id, skill.category, skill.description,
                  json.dumps(skill.trigger_conditions), json.dumps(skill.execution_steps), skill.code_template, skill.version, skill.created_at or now))
            await db.commit()

    async def search_skills(self, query: Optional[str] = None, category: Optional[str] = None,
                            team_id: str = "trading-team", top_k: int = 10) -> List[Dict[str, Any]]:
        await self.init_db()
        sql = "SELECT * FROM memory_skills WHERE team_id = ?"
        params: List[Any] = [team_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        if query:
            clauses = []
            for tok in _query_tokens(query):
                clauses.append("name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\'")
                pat = _like_pattern(tok)
                params.extend([pat, pat])
            sql += " AND (" + " OR ".join(clauses) + ")"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(top_k)

        async with self._get_conn() as db:
            async with await db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    item["trigger_conditions"] = json.loads(item.get("trigger_conditions_json") or "[]")
                    item["execution_steps"] = json.loads(item.get("execution_steps_json") or "[]")
                    results.append(item)
                return results

    # --- Wiki ---

    async def add_wiki_page(self, page: WikiPage) -> None:
        await self.init_db()
        now = time.time()
        async with self._get_conn() as db:
            await db.execute("""
                INSERT OR REPLACE INTO memory_wiki
                (page_id, team_id, title, content_md, tags_json, links_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (page.page_id, page.team_id, page.title, page.content_md, json.dumps(page.tags), json.dumps(page.links), now))
            await db.commit()

    async def get_wiki_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute("SELECT * FROM memory_wiki WHERE page_id = ?", (page_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    item = dict(row)
                    item["tags"] = json.loads(item.get("tags_json") or "[]")
                    item["links"] = json.loads(item.get("links_json") or "[]")
                    return item
                return None

    async def list_wiki_pages(self, team_id: str = "trading-team") -> List[Dict[str, Any]]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute("SELECT * FROM memory_wiki WHERE team_id = ? ORDER BY updated_at DESC", (team_id,)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    item["tags"] = json.loads(item.get("tags_json") or "[]")
                    item["links"] = json.loads(item.get("links_json") or "[]")
                    results.append(item)
                return results

    # --- CodeGraph ---

    async def add_codegraph_node(self, node: CodeGraphNode) -> None:
        await self.init_db()
        async with self._get_conn() as db:
            await db.execute("""
                INSERT OR REPLACE INTO memory_codegraph
                (symbol_name, symbol_type, file_path, line_start, line_end, docstring, dependencies_json, callers_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (node.symbol_name, node.symbol_type, node.file_path, node.line_start, node.line_end,
                  node.docstring, json.dumps(node.dependencies), json.dumps(node.callers)))
            await db.commit()

    async def list_codegraph(self, limit: int = 50) -> List[Dict[str, Any]]:
        await self.init_db()
        async with self._get_conn() as db:
            async with await db.execute("SELECT * FROM memory_codegraph LIMIT ?", (limit,)) as cursor:
                rows = await cursor.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    item["dependencies"] = json.loads(item.get("dependencies_json") or "[]")
                    item["callers"] = json.loads(item.get("callers_json") or "[]")
                    results.append(item)
                return results

    # --- Seed Default Assets from Current System & DB ---

    async def _seed_default_assets_if_empty(self):
        async with self._get_conn() as db:
            # Drop stale point-in-time facts seeded by earlier versions (trigger #96, EUR 200 account)
            await db.execute("DELETE FROM memory_l1_atoms WHERE atom_id IN ('atom_init_besi_trigger', 'atom_init_account_size')")
            await db.commit()
            async with await db.execute("SELECT COUNT(*) as cnt FROM memory_skills") as c:
                row = await c.fetchone()
                if row["cnt"] > 0:
                    return

        logger.info("Seeding initial TencentDB Agent Memory assets...")
        now = time.time()

        # Seed Skills
        skill_fee = SkillAsset(
            skill_id="ibkr-tiered-fee-mitigation",
            name="IBKR Tiered Fee Mitigation Playbook",
            category="risk-execution",
            description="Prevent micro-trade commission erosion on Interactive Brokers for EU and US equities.",
            trigger_conditions=[
                "Account capital is small (e.g. €200 - €1,000)",
                "Placing orders on Euronext (.AS, .PA), XETRA (.DE), or London (.L)",
                "Round-trip commission drag would exceed 3.8% of trade capital"
            ],
            execution_steps=[
                "Consolidate position slots: calculate available cash / min_viable (€100 for EU, €35 for US)",
                "Rule 9 check: evaluate_trade_economic_viability() before placing order or arming trigger",
                "If fee drag > 3.8% or net expected gain <= 0, Risk Officer triggers immediate veto",
                "Record estimated fees in trade audit log and track cumulative commissions in database"
            ],
            version="1.0.0"
        )
        await self.add_skill(skill_fee)

        skill_pullback = SkillAsset(
            skill_id="pullback-limit-entry",
            name="Limit Pullback Dip Entry Playbook",
            category="strategy-execution",
            description="Identify quality pullback entries on institutional growth stocks using limit triggers.",
            trigger_conditions=[
                "Technical analyst detects oversold pullback (RSI < 45 or near 50-day EMA support)",
                "Fundamental balance sheet health is STRONG or EXCELLENT",
                "Consensus score >= 70 from committee"
            ],
            execution_steps=[
                "Calculate pullback price level (1.5% - 3.0% below current spot)",
                "Arm OrderTrigger with dynamic consolidated allocation (e.g. €100)",
                "Set stop-loss at 1.8x ATR below entry and take-profit at upper resistance (min 2:1 R:R)",
                "Monitor real-time ticker quotes via scheduler; fire buy order on price breach"
            ],
            version="1.0.0"
        )
        await self.add_skill(skill_pullback)

        # Seed Wiki Pages
        wiki_ibkr = WikiPage(
            page_id="wiki-ibkr-pricing",
            title="Interactive Brokers Pricing & Fee Architecture",
            content_md="""# Interactive Brokers Pricing Architecture

## Commission Structure
- **European Equities (Euronext Amsterdam / Paris / Brussels / Lisbon, XETRA)**:
  - Tiered: 0.05% of trade value, with a €1.25 broker minimum + ~€0.65 exchange/clearing fee (€1.90/order min).
  - Round-trip cost: ~€3.80.
  - On a €33.33 order, fee drag is **11.4%**, guaranteeing a net loss.
  - Safe minimum order size: **€100.00** (drag ≤ 3.8%).
- **US Equities**:
  - Tiered: $0.0035/share ($0.35 minimum) + FINRA/SEC fees (~$0.38/order).
  - Safe minimum order size: **€35.00** (~$38.00).
- **UK Equities (.L)**:
  - 0.5% SDRT Stamp Duty on purchases + £1.00 minimum base fee.

## Guardrail Rules
1. `MAX_FEE_DRAG_PCT = 3.8%`
2. `MIN_ORDER_EUR_IBKR_EU = 100.0`
3. `MIN_ORDER_EUR_IBKR_US = 35.0`
4. `FULL_BUDGET_UTILIZATION`: Dynamically consolidates small budgets into viable €100 slots instead of splitting into toxic micro-orders.
""",
            tags=["ibkr", "fees", "broker", "pricing", "risk"],
            links=["wiki-portfolio-architecture", "skill-ibkr-tiered-fee-mitigation"]
        )
        await self.add_wiki_page(wiki_ibkr)

        wiki_portfolio = WikiPage(
            page_id="wiki-portfolio-architecture",
            title="Portfolio Capital & Order Trigger System",
            content_md="""# Portfolio Capital & Order Trigger Architecture

## Capital Segregation
- **Portfolio Value (Total Equity)**: Cash balance + sum of open market value of positions.
- **Reserved Trigger Capital**: Ring-fenced cash earmarked for pending limit triggers (`SUM(allocation_eur)`).
- **Deployable Available Cash**: `max(0.0, Usable_Cash - Reserved_Triggers)`.
- *Example*: In a €200.00 account with 0 positions and 1 active pending trigger for BESI.AS with €100.00 allocation, deployable cash is exactly €100.00.

## Dynamic Slot Consolidation
When total capital is small (€200), rather than dividing into `MAX_POSITIONS = 6` micro-slots (€33.33 each), the system concentrates funds into viable order slots (2 slots of €100.00 for EU equities).
""",
            tags=["portfolio", "cash", "triggers", "architecture"],
            links=["wiki-ibkr-pricing"]
        )
        await self.add_wiki_page(wiki_portfolio)

        # Seed Initial L1 Atoms
        await self.add_l1_atom(L1Atom(
            atom_id="atom_init_fee_rule",
            type="instruction",
            subject="RiskManager",
            predicate="enforces_fee_drag_limit",
            object="3.8% max round-trip commission drag",
            content="Risk Officer enforces a 3.8% max round-trip fee drag and €100 minimum order size on Euronext equities."
        ))

        # Seed CodeGraph
        await self.add_codegraph_node(CodeGraphNode(
            symbol_name="InvestmentCommittee",
            symbol_type="class",
            file_path="src/agents/committee.py",
            line_start=11,
            line_end=193,
            docstring="Multi-agent hedge fund committee presiding over investment decisions.",
            dependencies=["TechnicalAnalystAgent", "FundamentalAnalystAgent", "SentimentAnalystAgent", "RiskManagerAgent", "trade_learner"],
            callers=["run_market_session", "routes.api_evaluate"]
        ))
        await self.add_codegraph_node(CodeGraphNode(
            symbol_name="RiskManagerAgent",
            symbol_type="class",
            file_path="src/agents/risk_manager.py",
            line_start=12,
            line_end=150,
            docstring="Risk management agent with unilateral veto power over fee drag and risk limits.",
            dependencies=["evaluate_trade_economic_viability", "calculate_position_allocation"],
            callers=["InvestmentCommittee.evaluate_ticker"]
        ))
        await self.add_codegraph_node(CodeGraphNode(
            symbol_name="calculate_broker_fee",
            symbol_type="function",
            file_path="src/trading/fees.py",
            line_start=15,
            line_end=75,
            docstring="Accurate IBKR commission calculation for EU, US, and UK equities.",
            dependencies=["settings.IBKR_PRICING_TIER"],
            callers=["validate_buy_order", "evaluate_trade_economic_viability", "live_broker.execute_buy"]
        ))

        logger.info("TencentDB Agent Memory default assets seeded successfully.")

memory_store = AgentMemoryStore()
