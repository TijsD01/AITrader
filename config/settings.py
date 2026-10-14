import os
from pathlib import Path
from typing import Optional
from pydantic import field_validator
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
    # Web security: with WEB_PASSWORD set, every request needs HTTP Basic auth (browser prompts once).
    # Without it, only clients on the local/private network (LAN, localhost) are allowed.
    WEB_USERNAME: str = "admin"
    WEB_PASSWORD: Optional[str] = None
    WEB_ALLOW_PRIVATE_NETWORK_ONLY: bool = True
    # Extra networks treated as local (comma-separated CIDRs). Default: Tailscale (IPv4 CGNAT range + IPv6 ULA).
    WEB_TRUSTED_NETWORKS: str = "100.64.0.0/10,fd7a:115c:a1e0::/48"
    CORS_ALLOW_ORIGINS: str = ""            # Comma-separated extra origins allowed cross-site (empty = same-origin only)
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
    MAX_CAPITAL_USAGE_PCT: float = 100.0    # 10% to 100%: % of deposited account cash allowed to be traded
    MAX_DEPLOYABLE_CAPITAL_EUR: Optional[float] = None  # Optional hard EUR cap on deployable capital
    MAX_POSITIONS: int = 8

    @field_validator("MAX_DEPLOYABLE_CAPITAL_EUR", mode="before")
    @classmethod
    def _parse_deployable_capital(cls, v):
        if v in ("", None) or (isinstance(v, str) and not v.strip()):
            return None
        return float(v)
    POSITION_SIZE_PCT: float = 0.08         # 8% of equity per trade
    MAX_POSITION_SIZE_PCT: float = 1.0      # Up to 100% position size allowed
    DEFAULT_STOP_LOSS_PCT: float = 0.04     # 4% stop loss
    DEFAULT_TAKE_PROFIT_PCT: float = 0.08   # 8% take profit (1:2 R:R)
    AUTO_BUY_ENABLED: bool = True           # Auto-execute high conviction signals
    MIN_CONSENSUS_SCORE: int = 70           # Out of 100 to trigger auto-trade
    TRIGGER_MIN_SCORE: int = 80             # Score threshold (>= 80) to automatically arm AI price triggers
    TRIGGER_EXPIRY_DAYS: int = 7            # Pending price triggers older than this are marked EXPIRED
    ENFORCE_MARKET_HOURS: bool = True       # When true, trades are strictly blocked when markets are closed

    # Research-backed strategy controls (see analyse/onderzoekspapers.md in the project files)
    REGIME_FILTER_ENABLED: bool = True      # No new automated entries while the market index is below its long-term average
    REGIME_SMA_DAYS: int = 200              # Moving-average length for the regime filter
    REGIME_INDEX_EU: str = "^STOXX50E"      # Index that sets the regime for European tickers
    REGIME_INDEX_US: str = "^GSPC"          # Index that sets the regime for US tickers
    RISK_PER_TRADE_PCT: float = 0.5         # Automated buys lose at most this % of portfolio value at the stop (0 = off)
    TRAILING_STOP_ATR_MULT: float = 2.5     # Past the breakeven step, trail the stop at highest price minus N x ATR (0 = off)
    SENTIMENT_ANONYMIZE: bool = True        # Hide company name and ticker from the news-sentiment prompt
    SENTIMENT_VETO_SCORE: float = -0.4      # News sentiment at or below this blocks a buy; positive news adds nothing
    LEARNER_MIN_TRADES: int = 20            # Closed trades required between automatic learner recalibrations

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
    IBKR_PRICING_TIER: str = "tiered"       # "tiered" (low min for retail) or "fixed"
    MAX_FEE_DRAG_PCT: float = 3.8           # Max allowed round-trip fee as % of order (3.8% allows €100 EU orders)
    MAX_PROFIT_EROSION_PCT: float = 48.0    # Max allowed profit consumed by commissions
    MIN_ORDER_EUR_IBKR_EU: float = 100.0    # Floor order size for European equities on IBKR
    MIN_ORDER_EUR_IBKR_US: float = 35.0     # Recommended min order for US equities on IBKR

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

def update_env_file(updates: dict, env_path: Path = BASE_DIR / ".env") -> None:
    """
    Safely updates or appends key-value pairs in the .env file.
    Preserves comments and existing structure.
    """
    if not env_path.exists():
        return
    try:
        # Strip CR/LF from keys and values so a value cannot inject extra .env lines.
        def _clean(x) -> str:
            return str(x).replace("\r", "").replace("\n", "")
        updates = {
            _clean(k): (_clean(v) if isinstance(v, str) else v) for k, v in updates.items()
        }
        lines = env_path.read_text(encoding="utf-8").splitlines()
        remaining_keys = set(updates.keys())
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped or "=" not in stripped:
                new_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                val = updates[key]
                val_str = str(val).lower() if isinstance(val, bool) else _clean(val) if val is not None else ""
                new_lines.append(f"{key}={val_str}")
                remaining_keys.discard(key)
            else:
                new_lines.append(line)
        for key in remaining_keys:
            val = updates[key]
            val_str = str(val).lower() if isinstance(val, bool) else _clean(val) if val is not None else ""
            new_lines.append(f"{key}={val_str}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to update .env: {e}")

