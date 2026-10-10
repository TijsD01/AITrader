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

class ActiveBrokerProxy:
    """Transparent proxy delegating calls dynamically to the active broker."""
    def __getattr__(self, name: str):
        return getattr(get_active_broker(), name)

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
