import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Application
    APP_NAME: str = "Antigravity Multi-Agent Trading System"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DATA_DIR: Path = BASE_DIR / "data"

    # Database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'trading_bot.db'}"

    # LLM Settings
    LLM_PROVIDER: str = "antigravity"  # "antigravity", "gemini", or "openai"
    AGY_CLI_PATH: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-flash-latest"
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: str = "gpt-4o-mini"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Portfolio & Risk Management
    BASE_CURRENCY: str = "EUR"
    INITIAL_PAPER_CAPITAL: float = 10000.0  # €10,000
    MAX_POSITIONS: int = 8
    POSITION_SIZE_PCT: float = 0.08         # 8% of equity per trade
    MAX_POSITION_SIZE_PCT: float = 1.0      # Up to 100% position size allowed
    DEFAULT_STOP_LOSS_PCT: float = 0.04     # 4% stop loss
    DEFAULT_TAKE_PROFIT_PCT: float = 0.08   # 8% take profit (1:2 R:R)
    AUTO_BUY_ENABLED: bool = True           # Auto-execute high conviction signals
    MIN_CONSENSUS_SCORE: int = 70           # Out of 100 to trigger auto-trade
    TRIGGER_MIN_SCORE: int = 80             # Score threshold (>= 80) to automatically arm AI price triggers
    ENFORCE_MARKET_HOURS: bool = True       # When true, trades are strictly blocked when markets are closed

    # Real Money Trading & Broker Scheme
    TRADING_MODE: str = "PAPER"             # "PAPER" or "LIVE"
    LIVE_TRADING_ENABLED: bool = False      # Explicit safety interlock for real money
    FULL_BUDGET_UTILIZATION: bool = True    # Fully deploy available capital across active slots
    LIVE_BROKER: str = "ibkr"               # "ibkr" (Interactive Brokers) or "alpaca"
    KILL_SWITCH_ACTIVE: bool = False        # Emergency trading halt
    MAX_DAILY_LOSS_EUR: float = 500.0       # Daily drawdown stop (halts buys if exceeded)
    MAX_SINGLE_ORDER_EUR: float = 10000.0   # Hard cap on any single order

    # Interactive Brokers (IBKR) Config (Supports EU & US equities)
    IBKR_HOST: str = "172.17.0.2"
    IBKR_PORT: int = 4003                   # 4003 via socat gateway bridge
    IBKR_CLIENT_ID: int = 1
    IBKR_ACCOUNT_ID: Optional[str] = None

    # Alpaca Config (Supports US equities)
    ALPACA_API_KEY: Optional[str] = None
    ALPACA_SECRET_KEY: Optional[str] = None
    ALPACA_BASE_URL: str = "https://api.alpaca.markets"

    # Markets & Schedule (Times in CET / Europe/Amsterdam)
    TIMEZONE: str = "Europe/Amsterdam"
    EU_OPEN_HOUR: int = 9
    EU_OPEN_MINUTE: int = 0
    EU_CLOSE_HOUR: int = 17
    EU_CLOSE_MINUTE: int = 30
    US_OPEN_HOUR: int = 15
    US_OPEN_MINUTE: int = 30
    US_CLOSE_HOUR: int = 22
    US_CLOSE_MINUTE: int = 0

settings = Settings()

# Ensure data directory exists
os.makedirs(settings.DATA_DIR, exist_ok=True)
