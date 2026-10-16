import re
import time
import json
import logging
from typing import Dict, Any, List, Optional

from src.memory.models import L1Atom, L2Scenario, L3CoreProfile, SkillAsset, WikiPage
from src.memory.store import memory_store

logger = logging.getLogger(__name__)

from config.default_universe import EU_STOCKS, US_STOCKS

_STOP_TICKERS = {
    "A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "FOR", "FROM", "GO", "IF", "IN", "IS", "IT",
    "NO", "NOT", "OF", "ON", "OR", "SO", "THE", "TO", "UP", "US", "WE", "WITH", "AND", "ALL",
    "ANY", "NEW", "OLD", "ONE", "TWO", "TOP", "MAX", "MIN", "CAP", "FEE", "NET", "RAW",
    "BUY", "SELL", "HOLD", "STRONG_BUY", "LIMIT", "PULLBACK", "BREAKOUT", "LIMIT_PULLBACK",
    "BREAKOUT_BUY", "ORDER", "TRIGGER", "ENTRY", "PRICE", "TARGET", "HIT", "STOP", "LOSS",
    "EUR", "USD", "GBP", "EU", "UK", "NL", "DE", "FR", "VETO", "RISK", "NEWS", "SCORE",
    "USER", "AI", "PM", "EMA", "SMA", "RSI", "MACD", "ATR", "PEG", "ROE", "PE", "PB", "PS",
    "EV", "EPS", "FCF", "YOY", "QOQ", "ETF", "AEX", "DAX", "CAC", "XETRA", "NYSE", "IBKR",
    "SEC", "SDRT", "WIN", "HIGH", "LOW", "FAIR", "STRONG", "WEAK", "POOR", "GOOD", "FLAT",
    "MIXED", "BULL", "BEAR", "BULLISH", "BEARISH", "NEUTRAL", "WAIT", "PASS", "LONG", "SHORT",
    "CASH", "SPOT", "FILL", "OPEN", "CLOSE", "LEAD", "ARMED", "VERDICT", "TECHNICAL",
}

_UNIVERSE_TICKERS = {s["ticker"] for s in (EU_STOCKS + US_STOCKS)} | {
    "ASML", "INGA", "PHIA", "HEIA", "SHEL", "BESI", "SAP", "SIE", "ALV", "TTE", "SAN",
}


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
        use_llm: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Extracts L1 facts, L2 updates, L3 wisdom, and candidate skills
        from raw conversation, trade log, or deliberation text.
        `metadata` (ticker / action / consensus_score, as passed by the committee)
        takes precedence over regex guessing for the committee verdict.
        """
        extracted_atoms: List[L1Atom] = []
        extracted_skills: List[SkillAsset] = []
        l2_updates: List[Dict[str, Any]] = []
        l3_wisdom_notes: List[str] = []

        now = time.time()
        session_id = session_id or f"sess_{int(now*1000)}"
        metadata = metadata or {}

        # 1. Pattern Extraction for Financial Entities & Tickers (case-sensitive uppercase tokens)
        raw_tickers = re.findall(r'\b([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,3})?)\b', text)
        ticker_matches = [
            t for t in raw_tickers
            if t not in _STOP_TICKERS and not t.isdigit() and (len(t) >= 2 or t in _UNIVERSE_TICKERS)
        ]
        known_stocks = {
            t for t in ticker_matches
            if any(x in t for x in (".AS", ".PA", ".DE", ".L")) or t in _UNIVERSE_TICKERS
        }

        is_hold_or_sell_delib = (
            metadata.get("action") in ("HOLD", "SELL")
            or bool(re.search(r'Deliberation on [^\n]+\((?:HOLD|SELL)\b', text))
        )

        # 2. Extract Episodic Atoms (Trades, Orders, Triggers)
        for sent in re.split(r'(?<=[.!?])\s+|\n+', text):
            if not re.search(
                r'\b(?:trigger|pullback\s+entry|limit\s+(?:order|entry|pullback)|breakout\s+(?:buy|entry|order))\b',
                sent,
                re.IGNORECASE,
            ):
                continue
            id_m = re.search(r'(?:trigger|order|ref)\s*#(\d+)', sent, re.IGNORECASE)
            # In a HOLD/SELL deliberation without an explicit trigger #<id>, no trigger was actually armed
            if is_hold_or_sell_delib and not id_m:
                continue
            price_m = re.search(r'(?:@|\bat\b)\s*(?:target\s+price\s*)?[€$£]?(\d+(?:\.\d+)?)', sent, re.IGNORECASE)
            if not price_m:
                continue
            t_price = price_m.group(1).rstrip(".")
            trig_id = id_m.group(1) if id_m else str(metadata.get("trigger_id") or "latest")
            sent_tickers = [
                t for t in re.findall(r'\b([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,3})?)\b', sent)
                if t not in _STOP_TICKERS and not t.isdigit() and (len(t) >= 2 or t in _UNIVERSE_TICKERS)
            ]
            t_ticker = (
                metadata.get("ticker")
                or next((t for t in sent_tickers if t in known_stocks), None)
                or (sent_tickers[0] if sent_tickers else None)
            )
            if not t_ticker or t_ticker.upper() in _STOP_TICKERS:
                continue
            slug = re.sub(r'[^a-z0-9.]', '_', t_ticker.lower())
            atom = L1Atom(
                atom_id=f"atom_trig_{slug}_{trig_id}",
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

        # Look for fee & commission rules/observations (deduplicated by normalized value)
        fee_matches = re.finditer(r'(?:fee|commission|drag|stamp duty)[^.\n]{0,60}?(\d+(?:\.\d+)?%|\d+(?:\.\d+)?\s*(?:EUR|€|GBP|USD)|€\d+(?:\.\d+)?)', text, re.IGNORECASE)
        seen_fees = set()
        for fm in fee_matches:
            val = fm.group(1).strip()
            snippet = fm.group(0).strip()[:120]
            # Require rule/constraint context so routine "with fee of €1.90" trade logs or "Est. commission: €1.79 (0.7% drag)" are skipped
            has_rule_kw = bool(re.search(r'\b(?:limit|max|min|minimum|exceed|cap|rule|enforce|threshold|floor)\b', snippet, re.IGNORECASE))
            is_rule = has_rule_kw or "3.8%" in val
            if not is_rule:
                continue
            norm_val = re.sub(r'[^a-z0-9]', '', val.lower())
            if norm_val in seen_fees:
                continue
            seen_fees.add(norm_val)
            atom = L1Atom(
                atom_id=f"atom_fee_rule_{norm_val}",
                team_id=team_id,
                agent_id=agent_id,
                type="instruction",
                subject="IBKR_Broker_Fee",
                predicate="fee_constraint_observed",
                object=val,
                content=f"Fee constraint: {snippet}.",
                confidence=0.90,
                source_session_id=session_id
            )
            extracted_atoms.append(atom)

        # Look for capital or cash allocation mentions (excluding "Portfolio Manager:" speaker labels)
        cash_matches = list(re.finditer(
            r'(?:deployable\s+cash|available\s+cash|cash\s+balance|account\s+balance|portfolio\s+(?!manager\b)[^.\n]{0,30}?|allocated\s+capital[^.\n]{0,20}?|with\s+)(?:€(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:EUR|€|euros?))',
            text,
            re.IGNORECASE,
        ))
        if cash_matches:
            cm = cash_matches[-1]
            val = cm.group(1) or cm.group(2)
            atom = L1Atom(
                atom_id="atom_cash_balance",
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
        # Only the assistant's own lines can carry a verdict, never quoted "User:" lines or raw trade fill logs
        is_trade_fill_event = metadata.get("event") == "trade_execution" or bool(re.match(r'^\s*Executed\s+(?:BUY|SELL)\s+on\b', text))
        verdict_text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("User:"))
        action_match = re.search(r'\b(STRONG_BUY|BUY|HOLD|SELL)\b', verdict_text)
        score_match = re.search(r'(?:consensus|score|conviction).*?(\d{2,3})', verdict_text, re.IGNORECASE)
        has_deliberation_cue = bool(
            metadata.get("action") in ("STRONG_BUY", "BUY", "HOLD", "SELL")
            or score_match
            or re.search(r'\b(?:committee|deliberat|consensus|verdict|conviction)\b', verdict_text, re.IGNORECASE)
        )

        ticker = metadata.get("ticker") if isinstance(metadata.get("ticker"), str) else None
        verdict = metadata.get("action") if metadata.get("action") in ("STRONG_BUY", "BUY", "HOLD", "SELL") else None
        score = metadata.get("consensus_score")
        if not is_trade_fill_event and has_deliberation_cue:
            if ticker is None or verdict is None:
                # Regex fallback: first known ticker in order of appearance
                ordered = [t for t in ticker_matches if t in known_stocks and t in verdict_text]
                if ticker is None and ordered:
                    ticker = ordered[0]
                if verdict is None and action_match:
                    verdict = action_match.group(1)
            if score is None:
                score = score_match.group(1) if score_match else "75"

            if ticker and verdict and ticker.upper() not in _STOP_TICKERS:
                slug = re.sub(r'[^a-z0-9.]', '_', ticker.lower())
                atom = L1Atom(
                    atom_id=f"atom_verdict_{slug}",
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
                    "summary": f"Deliberated {verdict} on {ticker} (Score {score}/100).",
                    "context_content": text[:350],
                    "active_entities": [ticker, verdict]
                })
        elif is_trade_fill_event and ticker:
            order_m = re.search(r'\bExecuted\s+(BUY|SELL)\s+on\s+([\d.]+)x\s+\S+\s+at\s+([\d.]+)\s+(\w+)', text)
            if order_m:
                side, shares_s, price_s, curr_s = order_m.groups()
                slug = re.sub(r'[^a-z0-9.]', '_', ticker.lower())
                extracted_atoms.append(L1Atom(
                    atom_id=f"atom_trade_{slug}",
                    team_id=team_id,
                    agent_id=agent_id,
                    type="episodic",
                    subject=ticker,
                    predicate="trade_executed",
                    object=f"{side} {shares_s}x @ {price_s} {curr_s}",
                    content=text.strip()[:160],
                    confidence=0.98,
                    source_session_id=session_id
                ))

        # 4. Extract Reusable Skills (if playbook steps detected)
        if "pullback" in text.lower() and ("limit" in text.lower() or "rsi" in text.lower()):
            skill_id = "skill-pullback-extracted"  # fixed id so repeated extractions upsert
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
                existing_wisdom = current_l3.get("cumulative_wisdom", "")
                fresh_notes: List[str] = []
                for w in l3_wisdom_notes:
                    if w not in existing_wisdom and w not in fresh_notes:
                        fresh_notes.append(w)
                if fresh_notes:
                    new_wisdom = existing_wisdom + "\n" + "\n".join(f"• {w}" for w in fresh_notes)
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
