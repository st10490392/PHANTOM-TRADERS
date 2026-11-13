"""
Live MetaTrader 5 bar feed.

Mirrors CsvBarFeed's contract exactly: candles(symbol, timeframe, count) -> List[Dict]
with tz-aware UTC bar times (required by the DST session clock). The official
MetaTrader5 package is imported lazily so the repo still runs on non-Windows hosts
and in dry-run mode; a disconnected terminal degrades to an empty window instead of
raising into the engine loop.
"""
from datetime import datetime
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import MT5_SYMBOL_MAP, MT5_TIMEOUT_MS, broker_symbol

UTC = ZoneInfo("UTC")
DEFAULT_COUNT = 200
RatesFetcher = Callable[[str, object, int, int], List[Dict]]

_TIMEFRAME_NAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
                    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
                    "D1": "TIMEFRAME_D1"}


def _mt5_module():
    """Import MetaTrader5 lazily; None on hosts where it is unavailable."""
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return None
    return mt5


def _field(row, key: str, default=None):
    """MT5 returns numpy structured records; test/adapter payloads are plain dicts."""
    if isinstance(row, dict):
        return row.get(key, default)
    names = getattr(getattr(row, "dtype", None), "names", None)
    if names and key in names:
        return row[key]
    return default


class MT5BarFeed:
    """
    Live bars via mt5.copy_rates_from_pos(symbol, timeframe, 0, count).

    rates_fetch lets tests (or a custom transport) supply bar payloads without a
    terminal; it receives (broker_symbol, timeframe_code, 0, count).
    """

    def __init__(self, timeframe: str = "M5", count: int = DEFAULT_COUNT,
                 timeout_ms: int = MT5_TIMEOUT_MS, initialize: bool = True,
                 rates_fetch: Optional[RatesFetcher] = None, dry_run: bool = False):
        self.timeframe = timeframe
        self.count = count
        self.timeout_ms = timeout_ms
        self.dry_run = dry_run
        self.rates_fetch = rates_fetch
        self.missing: List[str] = []
        self._mt5 = None if rates_fetch is not None else _mt5_module()
        self._codes: Dict[str, object] = {}
        self.connected = False

        if dry_run:
            return
        if self.rates_fetch is not None:
            return                      # injected transport (tests, custom adapters)
        if self._mt5 is None:
            raise RuntimeError("MetaTrader5 package unavailable on this host "
                               "(use --feed csv or rates_fetch)")
        if initialize:
            self.connected = bool(self._mt5.initialize(timeout=timeout_ms))
            if not self.connected:
                print(f"[MT5 FEED] initialize failed: {self._mt5.last_error()}")
        for tag, name in _TIMEFRAME_NAMES.items():
            code = getattr(self._mt5, name, None)
            if code is not None:
                self._codes[tag] = code

    def timeframe_code(self, timeframe: str):
        code = self._codes.get(timeframe)
        if code is None and self.rates_fetch is None:
            raise ValueError(f"unknown MT5 timeframe {timeframe!r}")
        return code

    def candles(self, symbol: str, timeframe: Optional[str] = None,
                count: Optional[int] = None) -> List[Dict]:
        timeframe = timeframe or self.timeframe
        if self.dry_run:
            return []
        broker = broker_symbol(symbol)
        try:
            code = self.timeframe_code(timeframe)
            if self.rates_fetch is not None:
                rates = self.rates_fetch(broker, code, 0, count or self.count)
            else:
                if not self.connected or not self._mt5.symbol_info(broker):
                    if broker not in self.missing:
                        self.missing.append(broker)
                        print(f"[MT5 FEED] symbol not present in terminal: {broker} "
                              f"(internal tag {symbol})")
                    return []
                rates = self._mt5.copy_rates_from_pos(broker, code, 0, count or self.count)
        except Exception as exc:      # never stall the engine loop on a feed hiccup
            print(f"[MT5 FEED] {symbol} fetch failed: {exc}")
            return []
        if rates is None or len(rates) == 0:
            return []
        return [self._bar(row) for row in rates if _field(row, "close") is not None]

    @staticmethod
    def _bar(row) -> Dict:
        """MT5 rate (dict or numpy structured record) -> engine candle with tz-aware UTC time."""
        return {
            "time": datetime.fromtimestamp(int(_field(row, "time")), UTC),
            "open": float(_field(row, "open")),
            "high": float(_field(row, "high")),
            "low": float(_field(row, "low")),
            "close": float(_field(row, "close")),
            "volume": int(_field(row, "tick_volume", _field(row, "volume", 0))),
        }

    def symbols(self) -> List[str]:
        """Internal tags the terminal actually offers (mapped names present)."""
        if self.dry_run:
            return []
        if self.rates_fetch is not None or not self.connected:
            return sorted(MT5_SYMBOL_MAP)
        return [tag for tag, broker in MT5_SYMBOL_MAP.items() if self._mt5.symbol_info(broker)]

    def account_equity(self) -> Optional[float]:
        """Live equity for RiskManager.set_equity() (sanity-wrapped by the manager)."""
        if self.dry_run or self.rates_fetch is not None or not self.connected:
            return None
        info = self._mt5.account_info()
        return float(info.equity) if info else None

    def open_positions(self, magic: Optional[int] = None) -> List[Dict]:
        """
        Terminal positions tagged with our magic number, for crash-restart
        reconciliation: an engine that restarted mid-session has an empty
        in-memory position map, but the broker may still be holding trades it
        opened before the crash. Returns [] (never raises) when the terminal
        is unreachable, in dry-run, or under an injected transport - callers
        must not treat an empty list as proof nothing is open in that case.
        """
        if self.dry_run or self.rates_fetch is not None or not self.connected:
            return []
        try:
            positions = self._mt5.positions_get()
        except Exception as exc:
            print(f"[MT5 FEED] positions_get failed: {exc}")
            return []
        if not positions:
            return []
        out = []
        for pos in positions:
            if magic is not None and _field(pos, "magic") != magic:
                continue
            out.append({
                "ticket": _field(pos, "ticket"),
                "symbol": _field(pos, "symbol"),
                "volume": _field(pos, "volume"),
                "price_open": _field(pos, "price_open"),
                "sl": _field(pos, "sl"),
                "tp": _field(pos, "tp"),
                "type": _field(pos, "type"),   # 0 = BUY, 1 = SELL
                "comment": _field(pos, "comment", ""),
            })
        return out

    def reconnect(self) -> bool:
        """
        Tear down and re-initialize the terminal connection. Used by the
        runner's StaleFeedGuard when candles stop arriving - a terminal can
        drop its connection (VPS network blip, broker maintenance, terminal
        restart) without the Python process crashing, so nothing else would
        ever notice. No-op (returns current state) in dry-run or injected-
        transport mode, where there is no real connection to recycle.
        """
        if self.dry_run or self.rates_fetch is not None:
            return self.connected
        if self._mt5 is None:
            self._mt5 = _mt5_module()
            if self._mt5 is None:
                return False
            for tag, name in _TIMEFRAME_NAMES.items():
                code = getattr(self._mt5, name, None)
                if code is not None:
                    self._codes[tag] = code
        try:
            if self.connected:
                self._mt5.shutdown()
        except Exception:
            pass
        self.connected = bool(self._mt5.initialize(timeout=self.timeout_ms))
        if not self.connected:
            print(f"[MT5 FEED] reconnect failed: {self._mt5.last_error()}")
        else:
            print("[MT5 FEED] reconnected")
            self.missing = []   # symbol availability may differ after reconnect
        return self.connected

    def shutdown(self) -> None:
        if self._mt5 is not None and self.connected:
            self._mt5.shutdown()
            self.connected = False


def build_feed(kind: str, timeframe: str = "M5", count: int = DEFAULT_COUNT,
               dry_run: bool = False):
    """Factory used by the CLI runners: 'csv' keeps the offline dry-run mode."""
    from .data_feed import CsvBarFeed
    if kind == "csv":
        return CsvBarFeed(timeframe=timeframe, lookback=count)
    if kind == "mt5":
        return MT5BarFeed(timeframe=timeframe, count=count, dry_run=dry_run)
    raise ValueError(f"unknown feed {kind!r} (expected 'csv' or 'mt5')")