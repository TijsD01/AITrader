from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional

class BaseBroker(ABC):
    """
    Abstract Broker Interface for the Antigravity Trading System.
    Provides a unified API for PaperBroker (simulated) and LiveBroker (real money).
    """

    @abstractmethod
    async def get_portfolio_summary(self) -> Dict[str, Any]:
        """Returns cash, equity, unrealized PnL, open positions, and return %."""
        pass

    @abstractmethod
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Returns open positions with live prices, unrealized PnL, and stops."""
        pass

    @abstractmethod
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
        """Executes a buy order (or submits real market/limit order to broker)."""
        pass

    @abstractmethod
    async def execute_sell(
        self,
        ticker: str,
        reason: str = "Manual Close"
    ) -> Dict[str, Any]:
        """Executes a sell order closing the open position."""
        pass

    @abstractmethod
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
        """Creates a pending limit pullback or breakout trigger."""
        pass

    @abstractmethod
    async def cancel_price_trigger(self, trigger_id: int) -> Dict[str, Any]:
        """Cancels an armed price trigger."""
        pass

    @abstractmethod
    async def get_triggers(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves active or past order triggers."""
        pass

    @abstractmethod
    async def check_and_execute_triggers(self) -> List[Dict[str, Any]]:
        """Evaluates pending triggers against current prices and executes hits."""
        pass

    @abstractmethod
    async def check_stops_and_targets(self) -> List[Dict[str, Any]]:
        """Scans positions to trigger automated stop loss or take profit orders."""
        pass

    @abstractmethod
    async def sync_account_state(self) -> Dict[str, Any]:
        """Synchronizes local database records with broker cash and open positions."""
        pass

    @abstractmethod
    async def sync_cash_balance(self) -> None:
        """Refreshes the locally recorded cash balance from the broker without reconciling positions."""
        pass

    @abstractmethod
    async def check_connection(self, force: bool = False) -> Dict[str, Any]:
        """Checks connectivity with the broker API and returns detailed status and latency."""
        pass

    @abstractmethod
    async def adjust_triggers_dynamically(self) -> List[Dict[str, Any]]:
        """Recalculates and updates allocations for pending triggers based on available cash and remaining position slots."""
        pass
