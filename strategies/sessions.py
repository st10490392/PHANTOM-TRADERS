"""Shared DST-aware session clock. One source of truth for every engine."""
from datetime import datetime, time
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG, SessionBounds, SYNTHETIC_PREFIXES


def is_synthetic(symbol: str) -> bool:
    """Synthetic = Deriv continuous-tick asset (no session market close)."""
    cfg = ACTIVE_SYMBOLS.get(symbol)
    if cfg is not None and cfg.asset_class == "synthetic":
        return True
    return symbol.startswith(SYNTHETIC_PREFIXES)


def bounds_to_times(bounds: SessionBounds) -> Tuple[time, time]:
    return time(*bounds[0]), time(*bounds[1])


def within(bounds: SessionBounds, local: time) -> bool:
    start, end = bounds_to_times(bounds)
    return start <= local <= end


def clock(dt_utc: datetime, tz: ZoneInfo) -> time:
    """
    Wall clock used for session comparisons. Under 'UTC_FIXED' every zone is
    compared against UTC; under 'DST_LOCAL' each zone uses its own local clock,
    which is what makes zoneinfo apply the seasonal shift automatically.
    """
    if PROP_CONFIG.KILLZONE_MODE == "UTC_FIXED":
        return dt_utc.astimezone(PROP_CONFIG.TIMEZONE_UTC).time()
    return dt_utc.astimezone(tz).time()


def active_session(dt_utc: datetime, symbol: str) -> Optional[str]:
    """
    Returns 'LDN' | 'NY' | 'GLOBAL' (synthetics, 24/7) | None outside every window.
    DST_LOCAL mode (default): London 07:00-10:00 local is 07:00-10:00 UTC in winter
    and 06:00-09:00 UTC under BST; NY 13:30-16:30 local is 18:30-21:30 UTC in winter
    and 17:30-20:30 UTC under EDT. UTC_FIXED mode pins London to 07:00-10:00 UTC and
    New York to 13:30-16:30 UTC regardless of season.
    """
    if is_synthetic(symbol):
        return "GLOBAL"

    london_clock = clock(dt_utc, PROP_CONFIG.TIMEZONE_LONDON)
    ny_clock = clock(dt_utc, PROP_CONFIG.TIMEZONE_NY)

    if within(PROP_CONFIG.LONDON_KILLZONE, london_clock):
        return "LDN"
    if within(PROP_CONFIG.NY_KILLZONE, ny_clock):
        return "NY"
    return None


def in_forex_killzone(dt_utc: datetime, symbol: str) -> bool:
    """True inside London or New York killzones; synthetics are always allowed."""
    return active_session(dt_utc, symbol) is not None
