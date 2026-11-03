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
    If TRADING_MODE is 'LIVE' and LIVE_TRADING_ENABLED is True, returns the LiveBroker instance.
    Otherwise, returns the PaperBroker instance.
    The kill switch deliberately does not switch brokers: it blocks new buys (validate_buy_order,
    LiveBroker.is_live_armed) while real positions stay monitored and closable.
    """
    if settings.TRADING_MODE.upper() == "LIVE" and settings.LIVE_TRADING_ENABLED:
        return live_broker
    return paper_broker

class ActiveBrokerProxy:
    """Transparent proxy delegating calls dynamically to the active broker."""
    def __getattr__(self, name: str):
        return getattr(get_active_broker(), name)

active_broker = ActiveBrokerProxy()

async def check_broker_status(force: bool = False, probe_live: bool = False) -> Dict[str, Any]:
    """
    Checks connection status.
    If probe_live is True, explicitly probes the LiveBroker even when currently in paper mode.
    Otherwise checks the currently active broker.
    """
    if probe_live:
        return await live_broker.check_connection(force=force)
    return await active_broker.check_connection(force=force)

__all__ = [
    "BaseBroker",
    "PaperBroker",
    "LiveBroker",
    "paper_broker",
    "live_broker",
    "get_active_broker",
    "active_broker",
    "check_broker_status"
]
