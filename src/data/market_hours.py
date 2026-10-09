import datetime
from zoneinfo import ZoneInfo
from typing import Tuple, Dict, Any
from config.settings import settings
from config.default_universe import EU_STOCKS

# European suffixes commonly used on Yahoo Finance
EU_SUFFIXES = (
    ".AS", ".DE", ".PA", ".BR", ".MC", ".MI", ".L",
    ".ST", ".SW", ".HE", ".VI", ".CO", ".OL", ".LS"
)

def is_eu_ticker(ticker: str) -> bool:
    t = ticker.upper().strip()
    if any(t.endswith(sfx) for sfx in EU_SUFFIXES):
        return True
    eu_tickers = {s["ticker"].upper() for s in EU_STOCKS}
    return t in eu_tickers

def check_market_hours(ticker: str, now: datetime.datetime = None) -> Tuple[bool, str]:
    """
    Checks whether the appropriate stock exchange is currently open for the given ticker.
    European markets: Mon-Fri 09:00 - 17:30 CET
    US markets: Mon-Fri 15:30 - 22:00 CET (09:30 - 16:00 EST)
    Weekends: Closed
    """
    if not getattr(settings, "ENFORCE_MARKET_HOURS", True):
        return True, "Market hours check disabled by configuration."

    tz = ZoneInfo(settings.TIMEZONE)
    if now is None:
        now = datetime.datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    weekday = now.weekday()  # Monday is 0, Sunday is 6
    day_name = now.strftime("%A")
    current_time_str = now.strftime("%H:%M CET")

    # 1. Weekend Check
    if weekday >= 5:  # Saturday or Sunday
        return False, f"Markets are closed on weekends ({day_name}, {current_time_str}). Next trading session opens Monday."

    # 2. Market-specific hours check
    is_eu = is_eu_ticker(ticker)

    if is_eu:
        # European session: 09:00 to 17:30 CET
        open_time = datetime.time(settings.EU_OPEN_HOUR, settings.EU_OPEN_MINUTE)
        close_time = datetime.time(settings.EU_CLOSE_HOUR, settings.EU_CLOSE_MINUTE)
        current_time = now.time()

        if open_time <= current_time <= close_time:
            return True, f"European market is OPEN ({current_time_str})."
        else:
            return False, (
                f"European market (AEX/DAX/CAC) is currently CLOSED at {current_time_str}. "
                f"Trading hours are Mon-Fri {settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} - "
                f"{settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET."
            )
    else:
        # US session: 15:30 to 22:00 CET (09:30 - 16:00 EST)
        open_time = datetime.time(settings.US_OPEN_HOUR, settings.US_OPEN_MINUTE)
        close_time = datetime.time(settings.US_CLOSE_HOUR, settings.US_CLOSE_MINUTE)
        current_time = now.time()

        if open_time <= current_time <= close_time:
            return True, f"US market (NYSE/NASDAQ) is OPEN ({current_time_str})."
        else:
            return False, (
                f"US market (NYSE/NASDAQ) is currently CLOSED at {current_time_str}. "
                f"Trading hours are Mon-Fri {settings.US_OPEN_HOUR:02d}:{settings.US_OPEN_MINUTE:02d} - "
                f"{settings.US_CLOSE_HOUR:02d}:{settings.US_CLOSE_MINUTE:02d} CET (09:30 - 16:00 EST)."
            )

def get_market_statuses() -> Dict[str, Any]:
    """Returns the live status of European and US markets."""
    tz = ZoneInfo(settings.TIMEZONE)
    now = datetime.datetime.now(tz)
    weekday = now.weekday()
    curr_time = now.time()

    eu_open_t = datetime.time(settings.EU_OPEN_HOUR, settings.EU_OPEN_MINUTE)
    eu_close_t = datetime.time(settings.EU_CLOSE_HOUR, settings.EU_CLOSE_MINUTE)
    us_open_t = datetime.time(settings.US_OPEN_HOUR, settings.US_OPEN_MINUTE)
    us_close_t = datetime.time(settings.US_CLOSE_HOUR, settings.US_CLOSE_MINUTE)

    eu_is_open = (weekday < 5) and (eu_open_t <= curr_time <= eu_close_t)
    us_is_open = (weekday < 5) and (us_open_t <= curr_time <= us_close_t)

    return {
        "current_time_cet": now.strftime("%Y-%m-%d %H:%M:%S CET"),
        "is_weekend": weekday >= 5,
        "eu_market": {
            "status": "OPEN" if eu_is_open else "CLOSED",
            "is_open": eu_is_open,
            "hours": f"{settings.EU_OPEN_HOUR:02d}:{settings.EU_OPEN_MINUTE:02d} - {settings.EU_CLOSE_HOUR:02d}:{settings.EU_CLOSE_MINUTE:02d} CET"
        },
        "us_market": {
            "status": "OPEN" if us_is_open else "CLOSED",
            "is_open": us_is_open,
            "hours": f"{settings.US_OPEN_HOUR:02d}:{settings.US_OPEN_MINUTE:02d} - {settings.US_CLOSE_HOUR:02d}:{settings.US_CLOSE_MINUTE:02d} CET"
        }
    }
