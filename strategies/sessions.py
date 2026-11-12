"""Shared DST-aware session clock. One source of truth for every engine."""
from datetime import datetime, time
from typing import Optional, Tuple

from config.settings import PROP_CONFIG, SessionBounds, SYNTHETIC_PREFIXES


def is_synthetic(symbol: str) -> bool:
    return symbol.startswith(SYNTHETIC_PREFIXES)


def bounds_to_times(bounds: SessionBounds) -> Tuple[time, time]:
    return time(*bounds[0]), time(*bounds[1])


def within(bounds: SessionBounds, local: time) -> bool:
    start, end = bounds_to_times(bounds)
    return start <= local <= end


def active_session(dt_utc: datetime, symbol: str) -> Optional[str]:
    """
    Returns 'LDN' | 'NY' | 'GLOBAL' (synthetics, 24/7) | None outside every window.
    Bounds are session-LOCAL wall clock times, so zoneinfo applies the seasonal
    shift automatically: London 07:00-10:00 local is 07:00-10:00 UTC in winter
    and 06:00-09:00 UTC under BST; NY 13:30-16:30 local is 18:30-21:30 UTC in
    winter and 17:30-20:30 UTC under EDT.
    """
    if is_synthetic(symbol):
        return "GLOBAL"

    london_local = dt_utc.astimezone(PROP_CONFIG.TIMEZONE_LONDON).time()
    ny_local = dt_utc.astimezone(PROP_CONFIG.TIMEZONE_NY).time()

    if within(PROP_CONFIG.LONDON_KILLZONE, london_local):
        return "LDN"
    if within(PROP_CONFIG.NY_KILLZONE, ny_local):
        return "NY"
    return None


def in_forex_killzone(dt_utc: datetime, symbol: str) -> bool:
    """True inside London or New York killzones; synthetics are always allowed."""
    return active_session(dt_utc, symbol) is not None
