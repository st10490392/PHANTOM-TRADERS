"""
Live runner: bar feed -> engines -> risk gates -> Telegram dispatch.

The feed is any object exposing candles(symbol, timeframe) -> List[Dict];
CsvBarFeed covers offline datasets, and a live adapter (MT5 ZeroMQ bridge, broker
REST, Deriv WS) only has to implement the same method.
"""
import argparse
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .backtest import SCAN_WINDOW, Position
from .data_feed import CsvBarFeed
from .mt5_bridge import ExecutionBridge, build_execution
from .mt5_feed import build_feed
from .risk_manager import RiskManager
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG
from strategies import STRATEGY_REGISTRY
from telegram.dispatcher import TelegramDispatcher

UTC = ZoneInfo("UTC")
FOREX_STRATEGIES = ("SMC_FIB", "Z_SCORE")   # session engine; synthetics have their own scanner


class LiveEngine:
    def __init__(self, feed, dispatcher: Optional[TelegramDispatcher] = None,
                 timeframe: str = "M5", equity: float = PROP_CONFIG.BASE_EQUITY,
                 lookback: int = SCAN_WINDOW, bridge: bool = False,
                 execution: Optional[ExecutionBridge] = None):
        self.feed = feed
        self.dispatch = dispatcher if dispatcher is not None else TelegramDispatcher()
        self.timeframe = timeframe
        self.lookback = lookback
        self.strategies = {name: cls() for name, cls in STRATEGY_REGISTRY.items()
                           if name in FOREX_STRATEGIES}
        self.risk = RiskManager(equity=equity, day_start_equity=equity)
        self.equity = equity
        self.positions: Dict[str, Position] = {}
        self.alerts = 0
        self.rejections: Counter = Counter()
        self.max_daily_dd_pct = 0.0
        self.execution = execution if execution is not None else build_execution(bridge, self.dispatch)
        self._day: Optional[str] = None
        self._day_start = equity

    # ---------------- Daily accounting ----------------
    def _roll_day(self, stamp: datetime) -> str:
        day = stamp.date().isoformat()
        if day != self._day:
            self._day = day
            self._day_start = self.equity
            self.risk.day_start_equity = self.equity
        return day

    def _dd_breached(self) -> bool:
        if self._day_start <= 0:
            return True
        pct = max(0.0, (self._day_start - self.equity) / self._day_start)
        self.max_daily_dd_pct = max(self.max_daily_dd_pct, pct)
        return pct >= PROP_CONFIG.MAX_DAILY_DD_PERCENT

    # ---------------- Position lifecycle ----------------
    def _manage(self, symbol: str, candle: Dict) -> None:
        """Paper-simulated exits; with a live bridge the terminal owns SL/TP."""
        if self.execution is not None:
            return
        position = self.positions.get(symbol)
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if position is None or cfg is None:
            return
        sell = position.direction == "SELL"
        sl_hit = (candle["high"] >= position.stop_loss if sell
                  else candle["low"] <= position.stop_loss)
        tp_hit = (candle["low"] <= position.take_profit if sell
                  else candle["high"] >= position.take_profit)
        if not (sl_hit or tp_hit):
            return

        exit_price = position.stop_loss if sl_hit else position.take_profit
        reason = "SL" if sl_hit else "TP"
        move = (exit_price - position.entry) if not sell else (position.entry - exit_price)
        pnl = move * cfg.contract_size * position.lots
        self.equity += pnl
        self.risk.set_equity(self.equity)
        self.positions.pop(symbol)
        self.dispatch.send_lifecycle(reason, position.signal, exit_price, round(pnl, 2),
                                     f"{symbol} closed on {reason}")

    # ---------------- Signal gate + dispatch ----------------
    def _scan(self, symbol: str, candles: List[Dict], stamp: datetime, day_key: str) -> None:
        if self._dd_breached():
            self.rejections["daily drawdown breach"] += 1
            return
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if cfg is None or symbol in self.positions:
            return

        window = candles[-self.lookback:]
        for name, strategy in self.strategies.items():
            if name not in cfg.authorized_strategies:
                continue
            signal = strategy.analyze(window, symbol, stamp)
            if signal is None:
                continue
            verdict = self.risk.validate_signal(
                signal, [{"symbol": held} for held in self.positions], day_key=day_key)
            if not verdict["approved"]:
                self.rejections[verdict["reason"]] += 1
                continue

            position = Position(symbol=symbol, strategy=name, direction=signal.direction.value,
                                entry=signal.entry_price, stop_loss=signal.stop_loss,
                                take_profit=signal.take_profit, lots=verdict["lot_size"],
                                risk_usd=verdict["risk_usd"], opened_at=stamp, signal=signal)
            self.positions[symbol] = position
            self.alerts += 1
            self.dispatch.send_alert(signal, verdict)   # only approved setups reach the channel
            if self.execution is not None:
                self.execution.submit(signal, verdict)  # atomic order file for the EA

    def _ingest_fills(self) -> None:
        """Apply terminal receipts: equity from closes, cards from lifecycle events."""
        if self.execution is None:
            return
        for close in self.execution.ingest(self.positions):
            self.equity += close["pnl"]
            self.risk.set_equity(self.equity)
        for reason in self.execution.rejected:
            self.rejections[reason] += 1

    def tick(self, now: Optional[datetime] = None,
             symbols: Optional[List[str]] = None) -> Dict:
        universe = symbols if symbols is not None else [symbol for symbol, cfg in ACTIVE_SYMBOLS.items()
                                                         if cfg.asset_class != "synthetic"]
        stamp = now or datetime.now(UTC)
        day_key = self._roll_day(stamp)
        self._ingest_fills()

        for symbol in universe:
            candles = self.feed.candles(symbol, self.timeframe)
            if not candles:
                continue
            self._manage(symbol, candles[-1])
            # Gate on the live tick clock, not the bar's open time: an M5 bar that
            # opened at 09:55 closes at 10:00 and must not fire outside the killzone.
            self._scan(symbol, candles, stamp, day_key)

        return {"equity": round(self.equity, 2), "alerts": self.alerts,
                "open_positions": len(self.positions), "day": day_key,
                "max_daily_dd_pct": round(100 * self.max_daily_dd_pct, 2),
                "rejections": dict(self.rejections)}

    def run(self, interval: int = 60, symbols: Optional[List[str]] = None,
            max_iterations: Optional[int] = None) -> Dict:
        iterations = 0
        summary: Dict = {}
        while max_iterations is None or iterations < max_iterations:
            summary = self.tick(symbols=symbols)
            iterations += 1
            print(f"[ENGINE] tick {iterations} {summary['day']} equity={summary['equity']} "
                  f"open={summary['open_positions']} alerts={summary['alerts']}")
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="PHANTOM-TRADERS live engine loop")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--feed", choices=("csv", "mt5"), default="csv",
                        help="csv = offline dry-run bars, mt5 = live terminal bars")
    parser.add_argument("--bridge", action="store_true",
                        help="write order files and ingest terminal fill receipts")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the MT5 feed without touching a terminal")
    parser.add_argument("--once", action="store_true", help="single tick (dry run)")
    args = parser.parse_args()

    feed = build_feed(args.feed, args.timeframe, SCAN_WINDOW, dry_run=args.dry_run)
    engine = LiveEngine(feed, timeframe=args.timeframe, bridge=args.bridge)
    print(f"[ENGINE] feed={args.feed} bridge={'on' if args.bridge else 'off'} "
          f"files={engine.execution.bridge.root if engine.execution else 'n/a'}")
    try:
        summary = engine.run(interval=args.interval, symbols=args.symbols,
                             max_iterations=1 if args.once else None)
    finally:
        if hasattr(feed, "shutdown"):
            feed.shutdown()
    print("summary:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
