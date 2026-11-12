"""Historical bar ingestion for offline simulation."""
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import DATA_DIR, TIMEFRAME_MINUTES

UTC = ZoneInfo("UTC")
DEFAULT_WINDOW = 200


def csv_path(symbol: str, timeframe: str = "M5", data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / f"{symbol}_{timeframe}.csv"


def available_symbols(data_dir: Path = DATA_DIR, timeframe: str = "M5") -> List[str]:
    """Symbols present on disk as '<SYMBOL>_<TIMEFRAME>.csv'."""
    return sorted(path.stem.split("_")[0] for path in Path(data_dir).glob(f"*_{timeframe}.csv"))


def bar_minutes(timeframe: str) -> int:
    return TIMEFRAME_MINUTES[timeframe]


def load_candles(symbol: str, timeframe: str = "M5", data_dir: Path = DATA_DIR,
                 start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[Dict]:
    """
    Read '<SYMBOL>_<TIMEFRAME>.csv' (time,open,high,low,close[,volume]).
    Bar times are epoch seconds, converted to tz-aware UTC datetimes so the
    DST-aware session clock resolves correctly.
    """
    path = csv_path(symbol, timeframe, data_dir)
    if not path.exists():
        return []

    candles: List[Dict] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                stamp = datetime.fromtimestamp(int(row["time"]), UTC)
                candle = {
                    "time": stamp,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume") or 0),
                }
            except (KeyError, ValueError, TypeError):
                continue  # skip malformed rows rather than abort the replay
            if start is not None and stamp < start:
                continue
            if end is not None and stamp > end:
                continue
            candles.append(candle)
    return candles


def window_at(candles: List[Dict], index: int, lookback: int = DEFAULT_WINDOW) -> List[Dict]:
    """Bounded trailing window, mirroring what a live feed supplies per tick."""
    return candles[max(0, index - lookback + 1):index + 1]
