import time
import asyncio
import logging
from typing import Dict, Any, List, Optional

from src.memory.models import L0Message, ColdStartLoadout
from src.memory.store import memory_store
from src.memory.cold_start import cold_start_engine
from src.memory.extractor import asset_extractor

logger = logging.getLogger(__name__)

class AgentMemoryClient:
    """
    High-level Python client for TradingBot and AI agents.
    Allows instant cold-start memory retrieval, session tracking,
    and automatic asset extraction.
    """

    def __init__(self, default_team_id: str = "trading-team", default_agent_id: str = "committee"):
        self.team_id = default_team_id
        self.agent_id = default_agent_id

    async def get_cold_start_loadout(
        self,
        query: Optional[str] = "",
        agent_id: Optional[str] = None,
        char_budget: int = 3200,
        include_lessons: bool = True
    ) -> ColdStartLoadout:
        """
        Instantly builds the Cold Start Situation Report so an agent
        waking up knows everything that previously happened.
        """
        target_agent = agent_id or self.agent_id
        return await cold_start_engine.generate_cold_start_loadout(
            team_id=self.team_id,
            agent_id=target_agent,
            query=query,
            char_budget=char_budget,
            include_lessons=include_lessons
        )

    async def log_interaction(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        trigger_extraction: bool = True
    ) -> None:
        """
        Appends an interaction to L0 raw memory and optionally triggers asset extraction.
        """
        msg = L0Message(role=role, content=content, metadata=metadata)
        await memory_store.add_l0_messages(
            session_id=session_id,
            messages=[msg],
            team_id=self.team_id,
            agent_id=self.agent_id
        )

        if trigger_extraction:
            # Run extraction asynchronously in background
            asyncio.create_task(
                asset_extractor.extract_from_text(
                    text=content,
                    session_id=session_id,
                    team_id=self.team_id,
                    agent_id=self.agent_id,
                    sync_to_store=True,
                    metadata=metadata
                )
            )

    async def record_trade_event(
        self,
        ticker: str,
        order_type: str,
        shares: float,
        price: float,
        currency: str,
        pnl_eur: Optional[float] = None,
        fee_eur: Optional[float] = None,
        rationale: str = ""
    ) -> None:
        """
        Specialized helper to log a trade execution and immediately extract memory assets.
        """
        fee_info = f"with fee of €{fee_eur:.2f}" if fee_eur else ""
        pnl_info = f"realizing PnL of €{pnl_eur:+.2f}" if pnl_eur is not None else ""
        text = (
            f"Executed {order_type} on {shares}x {ticker} at {price} {currency} {fee_info} {pnl_info}. "
            f"Rationale: {rationale}"
        )
        session_id = f"trade_{ticker}_{int(time.time())}"
        await self.log_interaction(
            session_id=session_id,
            role="system",
            content=text,
            metadata={
                "event": "trade_execution",
                "ticker": ticker,
                "shares": shares,
                "price": price,
                "pnl_eur": pnl_eur,
                "fee_eur": fee_eur
            },
            trigger_extraction=True
        )

    async def record_trigger_event(
        self,
        trigger_id: int,
        ticker: str,
        trigger_type: str,
        target_price: float,
        allocation_eur: float,
        rationale: str = ""
    ) -> None:
        """
        Logs order trigger creation or update into memory.
        """
        text = (
            f"Armed order trigger #{trigger_id} for {ticker} ({trigger_type}) at target price €{target_price:.2f} "
            f"with allocated capital of €{allocation_eur:.2f}. Rationale: {rationale}"
        )
        session_id = f"trigger_{trigger_id}_{int(time.time())}"
        await self.log_interaction(
            session_id=session_id,
            role="system",
            content=text,
            metadata={
                "event": "trigger_armed",
                "trigger_id": trigger_id,
                "ticker": ticker,
                "target_price": target_price,
                "allocation_eur": allocation_eur
            },
            trigger_extraction=True
        )

    # --- Hermes / OpenClaw Compatibility Methods ---

    async def recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Recalls relevant memories for a query (Hermes compatibility)."""
        atoms = await memory_store.search_l1_atoms(query=query, team_id=self.team_id, limit=limit)
        return atoms

    async def capture(self, text: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Captures an interaction and extracts memories (Hermes compatibility)."""
        sess = session_id or f"hermes_{int(time.time()*1000)}"
        return await asset_extractor.extract_from_text(
            text=text,
            session_id=sess,
            team_id=self.team_id,
            agent_id=self.agent_id,
            sync_to_store=True
        )

agent_memory_client = AgentMemoryClient()
