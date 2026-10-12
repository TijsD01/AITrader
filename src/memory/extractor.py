import re
import time
import json
import logging
from typing import Dict, Any, List, Optional

from src.memory.models import L1Atom, L2Scenario, L3CoreProfile, SkillAsset, WikiPage
from src.memory.store import memory_store

logger = logging.getLogger(__name__)

class AssetExtractor:
    """
    TencentDB Agent Memory Asset Extraction Engine.
    Transforms raw interactions, trades, decisions, and transcripts (L0)
    into structured reusable assets:
    - L1 Atoms (Episodic facts, persona preferences, instruction constraints)
    - L2 Scenarios (Contextual scenario blocks)
    - L3 Core Profile (Cumulative trading wisdom and institutional rules)
    - Skills (Distilled executable workflows and tactical playbooks)
    """

    @staticmethod
    async def extract_from_text(
        text: str,
        session_id: Optional[str] = None,
        team_id: str = "trading-team",
        agent_id: str = "committee",
        sync_to_store: bool = True,
        use_llm: bool = False
    ) -> Dict[str, Any]:
        """
        Extracts L1 facts, L2 updates, L3 wisdom, and candidate skills
        from raw conversation, trade log, or deliberation text.
        """
        extracted_atoms: List[L1Atom] = []
        extracted_skills: List[SkillAsset] = []
        l2_updates: List[Dict[str, Any]] = []
        l3_wisdom_notes: List[str] = []

        now = time.time()
        session_id = session_id or f"sess_{int(now*1000)}"

        # 1. Pattern Extraction for Financial Entities & Tickers
        ticker_matches = re.findall(r'\b([A-Z0-9]{1,6}(?:\.[A-Z]{1,3})?)\b', text)
        known_stocks = set(t for t in ticker_matches if any(x in t for x in [".AS", ".PA", ".DE", ".L"]) or t in ["AAPL", "NVDA", "MSFT", "ASML", "BESI.AS", "TSLA"])

        # 2. Extract Episodic Atoms (Trades, Orders, Triggers)
        # Look for trigger entries
        trigger_matches = re.finditer(r'(?:trigger|order|pullback|limit)\s*(?:#?(\d+))?.*?(?:for|on)?\s*([A-Z0-9\.]+).*?(?:@|at)\s*€?([\d\.]+)', text, re.IGNORECASE)
        for m in trigger_matches:
            trig_id = m.group(1) or "unknown"
            t_ticker = m.group(2)
            t_price = m.group(3)
            atom = L1Atom(
                atom_id=f"atom_trig_{int(now*1000)}_{trig_id}",
                team_id=team_id,
                agent_id=agent_id,
                type="episodic",
                subject=t_ticker,
                predicate="trigger_price_level",
                object=f"€{t_price}",
                content=f"Trigger armed for {t_ticker} at €{t_price} (Ref #{trig_id}).",
                confidence=0.95,
                source_session_id=session_id
            )
            extracted_atoms.append(atom)

        # Look for fee & commission observations
        fee_matches = re.finditer(r'(?:fee|commission|drag|stamp duty).*?(\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:EUR|€|GBP|USD))', text, re.IGNORECASE)
        for fm in fee_matches:
            val = fm.group(1)
            atom = L1Atom(
                atom_id=f"atom_fee_{int(now*1000)}_{len(extracted_atoms)}",
                team_id=team_id,
                agent_id=agent_id,
                type="instruction",
                subject="IBKR_Broker_Fee",
                predicate="fee_constraint_observed",
                object=val,
                content=f"Fee constraint identified in trade context: {fm.group(0).strip()[:120]}.",
                confidence=0.90,
                source_session_id=session_id
            )
            extracted_atoms.append(atom)

        # Look for capital or cash allocation mentions
        cash_matches = re.finditer(r'(?:cash|portfolio|allocation|equity).*?€?([\d\.]+)\s*(?:EUR|€|euros?)', text, re.IGNORECASE)
        for cm in cash_matches:
            val = cm.group(1)
            atom = L1Atom(
                atom_id=f"atom_cash_{int(now*1000)}_{len(extracted_atoms)}",
                team_id=team_id,
                agent_id=agent_id,
                type="persona",
                subject="Capital_Allocation",
                predicate="account_balance_state",
                object=f"€{val}",
                content=f"Portfolio state noted: {cm.group(0).strip()[:100]}.",
                confidence=0.88,
                source_session_id=session_id
            )
            extracted_atoms.append(atom)

        # 3. Extract Committee Consensus / Action Verdicts
        action_match = re.search(r'\b(STRONG_BUY|BUY|HOLD|SELL)\b', text)
        score_match = re.search(r'(?:consensus|score|conviction).*?(\d{2,3})', text, re.IGNORECASE)
        if action_match and known_stocks:
            ticker = list(known_stocks)[0]
            verdict = action_match.group(1)
            score = score_match.group(1) if score_match else "75"
            atom = L1Atom(
                atom_id=f"atom_verdict_{int(now*1000)}",
                team_id=team_id,
                agent_id=agent_id,
                type="episodic",
                subject=ticker,
                predicate="committee_verdict",
                object=f"{verdict} (Score: {score})",
                content=f"Investment Committee rendered {verdict} verdict on {ticker} with consensus conviction of {score}/100.",
                confidence=0.98,
                source_session_id=session_id
            )
            extracted_atoms.append(atom)

            # L2 Scenario update
            l2_updates.append({
                "scenario_key": f"evaluation-{ticker.lower()}",
                "title": f"Committee Deliberation on {ticker}",
                "summary": f"Deliberated {verdict} on {ticker} factoring in technicals, fundamentals, sentiment, and fees.",
                "context_content": text[:500],
                "active_entities": [ticker, verdict]
            })

        # 4. Extract Reusable Skills (if playbook steps detected)
        if "pullback" in text.lower() and ("limit" in text.lower() or "rsi" in text.lower()):
            skill_id = f"skill-pullback-{int(now)}"
            extracted_skills.append(SkillAsset(
                skill_id=skill_id,
                name="Dynamically Extracted Pullback Entry",
                team_id=team_id,
                category="execution",
                description="Identified automated pullback protocol from live committee action.",
                trigger_conditions=["Oversold conditions met", "Consensus score >= 70"],
                execution_steps=["Arm limit order", "Check IBKR fee minimums", "Monitor fill"],
                version="1.0.0"
            ))

        # 5. Extract L3 Cumulative Wisdom
        if "veto" in text.lower() or "fee drag" in text.lower():
            l3_wisdom_notes.append("Enforce strict Risk Officer veto when broker commissions reduce trade profit below viable bounds.")

        # 6. LLM-Assisted Extraction if available
        try:
            from src.agents.base import llm_client
            if use_llm and llm_client.has_active_key() and len(text) > 100:
                prompt_sys = (
                    "You are the TencentDB Agent Memory Asset Extractor. "
                    "Extract structured memory assets from the following agent session or trading event. "
                    "Return a JSON object with: "
                    "{'atoms': [{'type': 'episodic'|'persona'|'instruction', 'subject': str, 'predicate': str, 'object': str, 'content': str}], "
                    " 'scenario_summary': str, 'wisdom_bullet': str}"
                )
                res = await llm_client.generate_json(prompt_sys, text[:2000])
                if res and isinstance(res, dict):
                    for a in res.get("atoms", []):
                        if isinstance(a, dict) and a.get("subject") and a.get("content"):
                            extracted_atoms.append(L1Atom(
                                atom_id=f"atom_llm_{int(now*1000)}_{len(extracted_atoms)}",
                                team_id=team_id,
                                agent_id=agent_id,
                                type=a.get("type", "episodic"),
                                subject=a.get("subject", "Agent"),
                                predicate=a.get("predicate", "observed"),
                                object=a.get("object", "event"),
                                content=a.get("content"),
                                confidence=0.92,
                                source_session_id=session_id
                            ))
                    if res.get("wisdom_bullet"):
                        l3_wisdom_notes.append(res["wisdom_bullet"])
        except Exception as e:
            logger.debug(f"LLM extraction skipped or failed: {e}")

        # 7. Persist to store if sync_to_store is True
        if sync_to_store:
            for atom in extracted_atoms:
                await memory_store.add_l1_atom(atom)

            for sc_dict in l2_updates:
                scenario = L2Scenario(
                    scenario_key=sc_dict["scenario_key"],
                    title=sc_dict["title"],
                    team_id=team_id,
                    agent_id=agent_id,
                    summary=sc_dict["summary"],
                    context_content=sc_dict["context_content"],
                    active_entities=sc_dict.get("active_entities", [])
                )
                await memory_store.update_scenario(scenario)

            if l3_wisdom_notes:
                current_l3 = await memory_store.get_core_profile(team_id=team_id, agent_id=agent_id)
                new_wisdom = current_l3.get("cumulative_wisdom", "") + "\n" + "\n".join(f"• {w}" for w in l3_wisdom_notes)
                updated_profile = L3CoreProfile(
                    team_id=team_id,
                    agent_id=agent_id,
                    persona_name=current_l3.get("persona_name", "Hedge Fund PM & Committee"),
                    core_profile=current_l3.get("core_profile", ""),
                    cumulative_wisdom=new_wisdom.strip(),
                    risk_rules=current_l3.get("risk_rules", [])
                )
                await memory_store.update_core_profile(updated_profile)

            for sk in extracted_skills:
                await memory_store.add_skill(sk)

        logger.info(
            "Extracted %d L1 atoms, %d scenarios, %d skills, %d wisdom notes from session %s",
            len(extracted_atoms), len(l2_updates), len(extracted_skills), len(l3_wisdom_notes), session_id
        )

        return {
            "session_id": session_id,
            "extracted_atoms_count": len(extracted_atoms),
            "extracted_atoms": [a.model_dump() for a in extracted_atoms],
            "extracted_scenarios": l2_updates,
            "extracted_skills": [s.model_dump() for s in extracted_skills],
            "extracted_wisdom": l3_wisdom_notes
        }

asset_extractor = AssetExtractor()
