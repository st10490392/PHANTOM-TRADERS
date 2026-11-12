"""
PHANTOM-TRADERS Global Configuration
Optimized for $5k Instant Funding Prop Firm Accounts
"""
from os import getenv
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Dict, List, Tuple

SessionBounds = Tuple[Tuple[int, int], Tuple[int, int]]


@dataclass(frozen=True)
class PropFirmConfig:
    """Immutable configuration for prop firm constraints."""
    BASE_EQUITY: float = 5000.00
    RISK_PERCENT: float = 0.003            # 0.30% fixed risk
    MIN_RISK_PERCENT: float = 0.0025       # 0.25% floor (payout-cycle discipline)
    MAX_RISK_PERCENT: float = 0.005        # 0.50% ceiling (never exceed on drawdown)
    BASE_RISK_AMOUNT: float = 15.00        # $15.00 per trade at base equity
    MAX_DAILY_DD_PERCENT: float = 0.04     # 4% hard breach buffer
    MAX_CORRELATED_POSITIONS: int = 2      # global shared-currency cap
    MAX_OPEN_POSITIONS: int = 2            # 1-2 setups/day frequency

    # Killzone bounds are SESSION LOCAL wall-clock times (hour, minute).
    # zoneinfo converts them against UTC every tick, so DST shifts are handled
    # automatically: London 07:00-10:00 local, New York 13:30-16:30 local.
    LONDON_KILLZONE: SessionBounds = ((7, 0), (10, 0))
    NY_KILLZONE: SessionBounds = ((13, 30), (16, 30))

    # Prior-session liquidity pools (ICT/Judas sweep targets).
    ASIA_SESSION: SessionBounds = ((0, 0), (7, 0))      # London local clock
    LONDON_SESSION: SessionBounds = ((7, 0), (10, 0))   # London local clock

    TIMEZONE_LONDON: ZoneInfo = ZoneInfo("Europe/London")
    TIMEZONE_NY: ZoneInfo = ZoneInfo("America/New_York")
    TIMEZONE_UTC: ZoneInfo = ZoneInfo("UTC")


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    authorized_strategies: List[str]
    contract_size: float = 100000.0
    point_value: float = 0.00001
    min_lot: float = 0.01
    max_lot: float = 1.0
    asset_class: str = "forex"  # forex | metal | index | synthetic


# Active Universe Mapping
ACTIVE_SYMBOLS: Dict[str, SymbolConfig] = {
    "EURUSD": SymbolConfig("EURUSD", ["SMC_FIB", "Z_SCORE"]),
    "GBPUSD": SymbolConfig("GBPUSD", ["SMC_FIB", "Z_SCORE"]),
    "USDJPY": SymbolConfig("USDJPY", ["SMC_FIB", "Z_SCORE"]),
    "EURGBP": SymbolConfig("EURGBP", ["SMC_FIB"]),
    "XAUUSD": SymbolConfig("XAUUSD", ["SMC_FIB", "Z_SCORE"],
                           contract_size=100.0, point_value=0.01, asset_class="metal"),
    "US30": SymbolConfig("US30", ["SMC_FIB"],
                         contract_size=1.0, point_value=1.0, asset_class="index"),
    "VOL75": SymbolConfig("VOL75", ["Z_SCORE"],
                          contract_size=1.0, point_value=0.01, asset_class="synthetic"),
    "VOL100": SymbolConfig("VOL100", ["Z_SCORE"],
                           contract_size=1.0, point_value=0.01, asset_class="synthetic"),
    "JUMP100": SymbolConfig("JUMP100", ["Z_SCORE"],
                            contract_size=1.0, point_value=0.01, asset_class="synthetic"),
}

# Deriv synthetic prefixes are monitored 24/7 (no forex session filters).
SYNTHETIC_PREFIXES = ("VOL", "JUMP", "BOOM", "CRASH")

# Telegram Configuration (never commit real secrets; supply via environment)
TELEGRAM_BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = getenv("TELEGRAM_CHAT_ID", "YOUR_PHANTOM_ALERTS_CHANNEL_ID")

PROP_CONFIG = PropFirmConfig()
