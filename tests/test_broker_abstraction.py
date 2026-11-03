import pytest
from config.settings import settings
from src.trading.base import BaseBroker
from src.trading.paper import paper_broker, PaperBroker
from src.trading.live import live_broker, LiveBroker
from src.trading.broker import get_active_broker, active_broker

def test_brokers_implement_base_broker():
    """Verify that both PaperBroker and LiveBroker implement BaseBroker."""
    assert isinstance(paper_broker, BaseBroker)
    assert isinstance(paper_broker, PaperBroker)
    assert isinstance(live_broker, BaseBroker)
    assert isinstance(live_broker, LiveBroker)

def test_get_active_broker_mode_switching():
    """Verify that get_active_broker dynamically toggles based on settings."""
    # Default is paper
    orig_mode = settings.TRADING_MODE
    orig_live_enabled = settings.LIVE_TRADING_ENABLED
    orig_kill = settings.KILL_SWITCH_ACTIVE
    try:
        settings.TRADING_MODE = "PAPER"
        settings.LIVE_TRADING_ENABLED = False
        settings.KILL_SWITCH_ACTIVE = False
        assert get_active_broker() is paper_broker

        # When set to LIVE and armed
        settings.TRADING_MODE = "LIVE"
        settings.LIVE_TRADING_ENABLED = True
        settings.KILL_SWITCH_ACTIVE = False
        assert get_active_broker() is live_broker

        # Kill switch halts new buys but keeps the live broker so real positions stay managed
        settings.KILL_SWITCH_ACTIVE = True
        assert get_active_broker() is live_broker
        armed, _ = live_broker.is_live_armed()
        assert armed is False

        # When LIVE_TRADING_ENABLED is False, fallback to paper
        settings.KILL_SWITCH_ACTIVE = False
        settings.LIVE_TRADING_ENABLED = False
        assert get_active_broker() is paper_broker
    finally:
        settings.TRADING_MODE = orig_mode
        settings.LIVE_TRADING_ENABLED = orig_live_enabled
        settings.KILL_SWITCH_ACTIVE = orig_kill

@pytest.mark.asyncio
async def test_active_broker_proxy_delegation():
    """Verify active_broker proxy forwards calls to the current active broker."""
    orig_mode = settings.TRADING_MODE
    orig_enforce = settings.ENFORCE_MARKET_HOURS
    settings.TRADING_MODE = "PAPER"
    settings.ENFORCE_MARKET_HOURS = False
    try:
        summary = await active_broker.get_portfolio_summary()
        assert "base_currency" in summary
        assert summary["base_currency"] == "EUR"
        assert "cash_eur" in summary
    finally:
        settings.TRADING_MODE = orig_mode
        settings.ENFORCE_MARKET_HOURS = orig_enforce
