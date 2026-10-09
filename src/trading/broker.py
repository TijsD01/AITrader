import logging
from typing import Dict, Any, List, Optional
from config.settings import settings
from src.trading.base import BaseBroker
from src.trading.paper import paper_broker, PaperBroker
from src.trading.live import live_broker, LiveBroker

logger = logging.getLogger(__name__)

def get_active_broker() -> BaseBroker:
    """
    Returns the currently active broker based on system configuration.
    If TRADING_MODE is 'LIVE', LIVE_TRADING_ENABLED is True, and KILL_SWITCH_ACTIVE is False,
    returns the LiveBroker instance. Otherwise, returns the PaperBroker instance.
    """
    if (
        settings.TRADING_MODE.upper() == "LIVE"
        and settings.LIVE_TRADING_ENABLED
        and not settings.KILL_SWITCH_ACTIVE
    ):
        return live_broker
    return paper_broker

class ActiveBrokerProxy(BaseBroker):
    """
    Transparent proxy delegating calls dynamically to the active broker.
    Allows callers to use `active_broker` without worrying about dynamic mode switches.
    """

    async def get_portfolio_summary(self) -> Dict[str, Any]:
        return await get_active_broker().get_portfolio_summary()

    async def get_positions(self) -> List[Dict[str, Any]]:
        return await get_active_broker().get_positions()

    async def execute_buy(
        self,
        ticker: str,
        name: str,
        price_native: float,
        currency: str,
        stop_loss_native: float,
        take_profit_native: float,
        allocation_eur: float,
        consensus_score: int,
        rationale: str,
        is_manual: bool = False,
        trigger_id: Optional[int] = None
    ) -> Dict[str, Any]:
        return await get_active_broker().execute_buy(
            ticker=ticker,
            name=name,
            price_native=price_native,
            currency=currency,
            stop_loss_native=stop_loss_native,
            take_profit_native=take_profit_native,
            allocation_eur=allocation_eur,
            consensus_score=consensus_score,
            rationale=rationale,
            is_manual=is_manual,
            trigger_id=trigger_id
        )

    async def execute_sell(self, ticker: str, reason: str = "Manual Close") -> Dict[str, Any]:
        return await get_active_broker().execute_sell(ticker=ticker, reason=reason)

    async def create_price_trigger(
        self,
        ticker: str,
        name: str,
        currency: str,
        trigger_type: str,
        target_price: float,
        current_price_at_creation: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        allocation_eur: float,
        consensus_score: int,
        ai_rationale: str
    ) -> Dict[str, Any]:
        return await get_active_broker().create_price_trigger(
            ticker=ticker,
            name=name,
            currency=currency,
            trigger_type=trigger_type,
            target_price=target_price,
            current_price_at_creation=current_price_at_creation,
            stop_loss=stop_loss,
            take_profit=take_profit,
            allocation_eur=allocation_eur,
            consensus_score=consensus_score,
            ai_rationale=ai_rationale
        )

    async def cancel_price_trigger(self, trigger_id: int) -> Dict[str, Any]:
        return await get_active_broker().cancel_price_trigger(trigger_id)

    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        return await get_active_broker().get_triggers(status=status)

    async def check_and_execute_triggers(self) -> List[Dict[str, Any]]:
        return await get_active_broker().check_and_execute_triggers()

    async def check_stops_and_targets(self) -> List[Dict[str, Any]]:
        return await get_active_broker().check_stops_and_targets()

    async def sync_account_state(self) -> Dict[str, Any]:
        return await get_active_broker().sync_account_state()

active_broker = ActiveBrokerProxy()

__all__ = [
    "BaseBroker",
    "PaperBroker",
    "LiveBroker",
    "paper_broker",
    "live_broker",
    "get_active_broker",
    "active_broker"
]
