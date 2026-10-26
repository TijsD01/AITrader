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

# Each exchange is evaluated in its own timezone so DST differences between the
# US and EU (e.g. late March and the last week of October) are handled correctly.
EU_EXCHANGE_TZ = "Europe/Amsterdam"
US_EXCHANGE_TZ = "America/New_York"
US_OPEN_LOCAL = datetime.time(9, 30)
US_CLOSE_LOCAL = datetime.time(16, 0)


def us_session_local(now: datetime.datetime) -> Tuple[datetime.datetime, datetime.datetime]:
    """US session (09:30-16:00 New York) for now's date, expressed in now's timezone."""
    ny = ZoneInfo(US_EXCHANGE_TZ)
    open_dt = datetime.datetime.combine(now.date(), US_OPEN_LOCAL, tzinfo=ny).astimezone(now.tzinfo)
    close_dt = datetime.datetime.combine(now.date(), US_CLOSE_LOCAL, tzinfo=ny).astimezone(now.tzinfo)
    return open_dt, close_dt


def _eu_session() -> Tuple[datetime.time, datetime.time]:
    return (
        datetime.time(settings.EU_OPEN_HOUR, settings.EU_OPEN_MINUTE),
        datetime.time(settings.EU_CLOSE_HOUR, settings.EU_CLOSE_MINUTE),
    )


def check_market_hours(ticker: str, now: datetime.datetime = None) -> Tuple[bool, str]:
    """
    Checks whether the appropriate stock exchange is currently open for the given ticker.
    European markets: Mon-Fri 09:00 - 17:30 Europe/Amsterdam
    US markets: Mon-Fri 09:30 - 16:00 America/New_York (15:30 - 22:00 CET outside DST mismatch weeks)
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

    is_eu = is_eu_ticker(ticker)
    exch_tz = ZoneInfo(EU_EXCHANGE_TZ if is_eu else US_EXCHANGE_TZ)
    local_now = now.astimezone(exch_tz)

    weekday = local_now.weekday()  # Monday is 0, Sunday is 6
    day_name = local_now.strftime("%A")
    current_time_str = now.strftime("%H:%M CET")

    # 1. Weekend Check (in the exchange's own timezone)
    if weekday >= 5:  # Saturday or Sunday
        return False, f"Markets are closed on weekends ({day_name}, {current_time_str}). Next trading session opens Monday."

    # 2. Market-specific hours check
    current_time = local_now.time()
    if is_eu:
        open_time, close_time = _eu_session()
        if open_time <= current_time <= close_time:
            return True, f"European market is OPEN ({current_time_str})."
        return False, (
            f"European market (AEX/DAX/CAC) is currently CLOSED at {current_time_str}. "
            f"Trading hours are Mon-Fri {open_time.strftime('%H:%M')} - {close_time.strftime('%H:%M')} "
            f"{local_now.tzname()} (Amsterdam time)."
        )

    if US_OPEN_LOCAL <= current_time <= US_CLOSE_LOCAL:
        return True, f"US market (NYSE/NASDAQ) is OPEN ({current_time_str})."
    return False, (
        f"US market (NYSE/NASDAQ) is currently CLOSED at {current_time_str} "
        f"({local_now.strftime('%H:%M %Z')}). "
        f"Trading hours are Mon-Fri {US_OPEN_LOCAL.strftime('%H:%M')} - {US_CLOSE_LOCAL.strftime('%H:%M')} "
        f"New York time."
    )

def get_market_statuses(now: datetime.datetime = None) -> Dict[str, Any]:
    """Returns the live status of European and US markets."""
    tz = ZoneInfo(settings.TIMEZONE)
    if now is None:
        now = datetime.datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    eu_now = now.astimezone(ZoneInfo(EU_EXCHANGE_TZ))
    us_now = now.astimezone(ZoneInfo(US_EXCHANGE_TZ))

    eu_open_t, eu_close_t = _eu_session()
    eu_is_open = (eu_now.weekday() < 5) and (eu_open_t <= eu_now.time() <= eu_close_t)
    us_is_open = (us_now.weekday() < 5) and (US_OPEN_LOCAL <= us_now.time() <= US_CLOSE_LOCAL)

    # Express the US session in the configured (CET) timezone for today's DST state.
    us_open_cet, us_close_cet = us_session_local(now)

    return {
        "current_time_cet": now.strftime("%Y-%m-%d %H:%M:%S CET"),
        "is_weekend": now.weekday() >= 5,
        "eu_market": {
            "status": "OPEN" if eu_is_open else "CLOSED",
            "is_open": eu_is_open,
            "hours": f"{eu_open_t.strftime('%H:%M')} - {eu_close_t.strftime('%H:%M')} CET"
        },
        "us_market": {
            "status": "OPEN" if us_is_open else "CLOSED",
            "is_open": us_is_open,
            "hours": f"{us_open_cet.strftime('%H:%M')} - {us_close_cet.strftime('%H:%M')} CET"
        }
    }
