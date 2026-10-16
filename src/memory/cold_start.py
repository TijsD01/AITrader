import time
import logging
from typing import Dict, Any, List, Optional
from sqlalchemy import select, desc

from src.memory.models import ColdStartLoadout
from src.memory.store import memory_store

logger = logging.getLogger(__name__)

PORTFOLIO_CACHE_TTL_S = 30.0

class ColdStartEngine:
    """
    Builds the instant situational memory loadout for AI agents upon startup.
    Ensures that from turn 1, any agent knows:
    - Exactly what happened previously (recent trades, triggers, post-mortems, fees)
    - The live financial status (cash, reserved trigger funds, open positions)
    - Active L1/L2/L3 memory assets and executable skills.
    """

    # (timestamp, state) of the last successful portfolio fetch
    _portfolio_cache: Optional[tuple] = None

    @staticmethod
    async def generate_cold_start_loadout(
        team_id: str = "trading-team",
        agent_id: str = "committee",
        query: Optional[str] = "",
        char_budget: int = 3200,
        include_lessons: bool = True
    ) -> ColdStartLoadout:
        import asyncio
        t0 = time.time()
        await memory_store.init_db()

        # 1. Fetch portfolio state and memory layers in parallel
        portfolio_state, l3, scenarios, l1_atoms, skills, wiki_pages = await asyncio.gather(
            ColdStartEngine._fetch_portfolio_status(),
            memory_store.get_core_profile(team_id=team_id, agent_id=agent_id),
            memory_store.list_scenarios(team_id=team_id),
            memory_store.search_l1_atoms(query=query or None, team_id=team_id, limit=6),
            memory_store.search_skills(query=query or None, team_id=team_id, top_k=3),
            memory_store.list_wiki_pages(team_id=team_id),
        )

        # Fall back to core skills when query is a ticker symbol that matches no skill name
        if not skills:
            skills = await memory_store.search_skills(query=None, team_id=team_id, top_k=2)

        # 2. Build "What Had Happened" history recap
        what_happened_text = await ColdStartEngine._build_history_recap(
            portfolio_state, include_lessons=include_lessons
        )

        # 3. Format L3 Core Profile (deduplicating wisdom lines)
        wisdom_lines = []
        for line in (l3.get("cumulative_wisdom") or "").splitlines():
            clean = line.strip()
            if clean and clean not in wisdom_lines:
                wisdom_lines.append(clean)
        l3_text = (
            f"**Persona**: {l3.get('persona_name')}\n"
            f"**Mandate**: {l3.get('core_profile')}\n"
            f"**Rules**:\n" + "\n".join(wisdom_lines[:3])
        )

        # 4. Select L2 Active Scenario (match query ticker/entity tokens; never inject an unrelated ticker's deliberation)
        matched_sc = None
        q_clean = (query or "").strip()
        if q_clean and scenarios:
            from src.memory.store import _query_tokens
            _generic_sc_words = {
                "buy", "sell", "hold", "strong_buy", "committee", "deliberation",
                "evaluation", "scenario", "active", "session", "status", "today",
            }
            q_tokens = [t.lower() for t in _query_tokens(q_clean) if t.lower() not in _generic_sc_words]
            for sc in scenarios:
                sc_key = sc.get("scenario_key", "").lower()
                sc_ticker = sc_key[len("evaluation-"):] if sc_key.startswith("evaluation-") else sc_key
                entities_lower = {
                    str(e).lower() for e in sc.get("active_entities", [])
                    if str(e).lower() not in _generic_sc_words
                }
                if any(
                    tok == sc_ticker
                    or tok == sc_key
                    or tok in entities_lower
                    or any(ent.startswith(f"{tok}.") for ent in entities_lower if len(tok) >= 2)
                    for tok in q_tokens
                ):
                    matched_sc = sc
                    break
        elif scenarios:
            matched_sc = scenarios[0]

        if matched_sc:
            l2_text = f"**Scenario: {matched_sc['title']}** — {matched_sc['summary']}"
        else:
            l2_text = "**Active Scenario**: Live Equities Session (Euronext, XETRA, US Equities with Tiered Fee Accounting)."

        # 5. Format L1 Facts (prioritize non-redundant facts; keep at least 1 fact)
        all_l1_facts = [f"[{a['type'].upper()}] {a['content']}" for a in l1_atoms]
        filtered_l1 = [
            f for a, f in zip(l1_atoms, all_l1_facts)
            if a.get("subject") not in ("IBKR_Broker_Fee", "RiskManager")
        ]
        l1_key_facts = (filtered_l1 or all_l1_facts[:2])[:5]

        # 6. Format Active Skills compactly
        active_skills = [
            f"**{s['name']}** ({s['category']}): {s['description']}"
            for s in skills[:3]
        ]

        # 7. Format Wiki Highlights
        wiki_highlights = [
            f"**{w['title']}**: {w['content_md'].splitlines()[2] if len(w['content_md'].splitlines()) > 2 else w['title']}"
            for w in wiki_pages[:3]
        ]

        # 8. Assemble Compact Full Prompt Context
        if portfolio_state.get("available", True):
            status_summary = (
                f"Portfolio: €{portfolio_state.get('total_equity_eur', 0):.2f} Total Equity | "
                f"€{portfolio_state.get('available_cash_eur', 0):.2f} Deployable Cash | "
                f"€{portfolio_state.get('reserved_trigger_cash_eur', 0):.2f} Ring-Fenced in {portfolio_state.get('pending_triggers_count', 0)} Trigger(s) | "
                f"{portfolio_state.get('open_positions_count', 0)} Open Position(s)."
            )
        else:
            status_summary = "Portfolio status unavailable: live broker state could not be queried. Do not assume cash, positions or triggers."

        sections = [
            "=== INSTANT MEMORY LOADOUT (TENCENTDB AGENT MEMORY) ===",
            f"[SYSTEM SITUATION REPORT]\n{status_summary}",
            f"--- WHAT HAD HAPPENED RECENTLY ---\n{what_happened_text}",
            f"--- L3 CORE PERSONA & INSTITUTIONAL RULES ---\n{l3_text}",
            f"--- L2 ACTIVE OPERATIONAL SCENARIO ---\n{l2_text}",
        ]
        if l1_key_facts:
            sections.append("--- L1 KEY EXTRACTED ATOMS & CONSTRAINTS ---\n" + "\n".join(f"- {f}" for f in l1_key_facts))
        if active_skills:
            sections.append("--- ACTIVE SKILL PLAYBOOKS ---\n" + "\n".join(f"- {s}" for s in active_skills))
        sections.append("======================================================")
        full_prompt_context = "\n\n".join(sections) + "\n"

        # Enforce character budget
        if len(full_prompt_context) > char_budget:
            full_prompt_context = full_prompt_context[:char_budget] + "\n[...Context truncated to character budget...]"

        dur_ms = round((time.time() - t0) * 1000, 1)
        logger.info("Generated cold start memory loadout in %sms (%s chars)", dur_ms, len(full_prompt_context))

        return ColdStartLoadout(
            status_summary=status_summary,
            what_had_happened=what_happened_text,
            l3_core_profile=l3_text,
            l2_active_scenario=l2_text,
            l1_key_facts=l1_key_facts,
            active_skills=active_skills,
            wiki_highlights=wiki_highlights,
            portfolio_state=portfolio_state,
            full_prompt_context=full_prompt_context,
            timestamp=time.time()
        )

    @staticmethod
    async def _fetch_portfolio_status() -> Dict[str, Any]:
        """
        Queries the live trading bot state from the active broker.
        Broker reads (paper) refresh P&L and re-adjust triggers (DB writes), so a
        successful result is cached for PORTFOLIO_CACHE_TTL_S seconds.
        """
        cached = ColdStartEngine._portfolio_cache
        if cached is not None and time.time() - cached[0] < PORTFOLIO_CACHE_TTL_S:
            return cached[1]
        try:
            from src.trading.broker import get_active_broker
            broker = get_active_broker()
            summary = await broker.get_portfolio_summary()
            triggers = await broker.get_triggers(status="PENDING")
            positions = await broker.get_positions()
            state = {
                "available": True,
                "total_equity_eur": summary["total_value_eur"],
                "cash_eur": summary["cash_eur"],
                "available_cash_eur": summary["available_cash_eur"],
                "reserved_trigger_cash_eur": summary["reserved_triggers_eur"],
                "open_positions_count": summary.get("open_positions_count", len(positions)),
                "pending_triggers_count": len(triggers),
                "active_triggers": [
                    {
                        "id": t.get("id"),
                        "ticker": t.get("ticker"),
                        "trigger_type": t.get("trigger_type"),
                        "target_price": t.get("target_price") or 0.0,
                        "allocation_eur": t.get("allocation_eur") or 0.0,
                        "armed_at": str(t.get("created_at", "")),
                    }
                    for t in triggers
                ],
                "positions": [
                    {
                        "ticker": p.get("ticker"),
                        "shares": p.get("shares"),
                        "entry_price": p.get("entry_price_native"),
                        "pnl_eur": p.get("unrealized_pnl_eur"),
                    }
                    for p in positions
                ],
            }
            ColdStartEngine._portfolio_cache = (time.time(), state)
            return state
        except Exception as e:
            logger.warning(f"Failed to query live portfolio status: {e}")
            return {
                "available": False,
                "error": str(e),
                "pending_triggers_count": 0,
                "open_positions_count": 0,
                "active_triggers": [],
                "positions": [],
            }

    @staticmethod
    async def _build_history_recap(portfolio_state: Dict[str, Any], include_lessons: bool = True) -> str:
        """Constructs concise narrative of recent trades, triggers, and post-mortems."""
        lines = []

        # 1. Triggers
        triggers = portfolio_state.get("active_triggers", [])
        available = portfolio_state.get("available", True)
        if not available:
            lines.append("• **Portfolio & Triggers**: status unavailable (broker could not be queried); do not assume any armed triggers.")
        elif triggers:
            lines.append("• **Active Order Triggers Armed**:")
            for t in triggers:
                lines.append(
                    f"  - Trigger #{t['id']}: {t['ticker']} ({t['trigger_type']} @ €{t['target_price']:.2f}, alloc €{t['allocation_eur']:.2f})"
                )
        else:
            lines.append("• **Active Triggers**: None armed currently.")

        # 2. Recent closed trades & lessons from DB
        try:
            from src.db.session import AsyncSessionLocal
            from src.db.models import Trade, TradeLesson
            async with AsyncSessionLocal() as session:
                trade_res = await session.execute(select(Trade).order_by(desc(Trade.executed_at)).limit(3))
                trades = trade_res.scalars().all()
                if trades:
                    lines.append("• **Recent Executed Trades**:")
                    for tr in trades:
                        details = []
                        if tr.realized_pnl_eur is not None:
                            pct_str = f" ({tr.realized_pnl_pct:+.2f}%)" if tr.realized_pnl_pct is not None else ""
                            details.append(f"PnL: €{tr.realized_pnl_eur:+.2f}{pct_str}")
                        if getattr(tr, "fee_eur", None):
                            details.append(f"Fee: €{tr.fee_eur:.2f}")
                        detail_str = f" ({', '.join(details)})" if details else ""
                        lines.append(
                            f"  - {tr.order_type} {tr.shares}x {tr.ticker} @ {tr.price_native} {tr.currency}{detail_str}"
                        )

                if include_lessons:
                    lesson_res = await session.execute(select(TradeLesson).order_by(desc(TradeLesson.created_at)).limit(2))
                    lessons = lesson_res.scalars().all()
                    if lessons:
                        lines.append("• **Recent Trade Post-Mortems & Lessons**:")
                        for les in lessons:
                            lines.append(f"  - [{les.ticker} {les.outcome}]: {les.lesson}")
        except Exception as e:
            logger.debug(f"Could not load trade history from database: {e}")

        return "\n".join(lines)

cold_start_engine = ColdStartEngine()
