"""
24/7 Deriv synthetic background scanner.

Deriv synthetics (Volatility / Boom / Crash / Step) never close, so this scanner
runs continuously with no session or market-close gate. Approved setups are sent
to the Telegram dispatcher tagged [DERIV SYNTHETICS].
"""
import argparse
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .backtest import Position
from .data_feed import CsvBarFeed, DEFAULT_WINDOW
from .risk_manager import RiskManager
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG, SYNTHETIC_UNIVERSE
from strategies import STRATEGY_REGISTRY, SYNTHETIC_STRATEGIES
from telegram.dispatcher import TelegramDispatcher

UTC = ZoneInfo("UTC")


class SyntheticScanner:
    def __init__(self, feed, dispatcher: Optional[TelegramDispatcher] = None,
                 timeframe: str = "M5", equity: float = PROP_CONFIG.BASE_EQUITY,
                 lookback: int = DEFAULT_WINDOW):
        self.feed = feed
        self.dispatch = dispatcher if dispatcher is not None else TelegramDispatcher()
        self.timeframe = timeframe
        self.lookback = lookback
        self.strategies = {name: cls() for name, cls in STRATEGY_REGISTRY.items()
                           if name in SYNTHETIC_STRATEGIES}
        self.risk = RiskManager(equity=equity, day_start_equity=equity)
        self.equity = equity
        self.positions: Dict[str, Position] = {}
        self.alerts = 0
        self.rejections: Counter = Counter()
        self.max_daily_dd_pct = 0.0
        self._day: Optional[str] = None
        self._day_start = equity

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

    def _manage(self, symbol: str, candle: Dict) -> None:
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

    def scan_once(self, now: Optional[datetime] = None,
                  symbols: Optional[List[str]] = None) -> Dict:
        universe = symbols or SYNTHETIC_UNIVERSE
        stamp = now or datetime.now(UTC)
        day_key = self._roll_day(stamp)

        for symbol in universe:
            cfg = ACTIVE_SYMBOLS.get(symbol)
            if cfg is None or cfg.asset_class != "synthetic":
                continue                    # no session gate: synthetics trade 24/7
            candles = self.feed.candles(symbol, self.timeframe)
            if not candles:
                continue
            self._manage(symbol, candles[-1])
            if self._dd_breached():
                self.rejections["daily drawdown breach"] += 1
                continue
            if symbol in self.positions:
                continue

            window = candles[-self.lookback:]
            for name, strategy in self.strategies.items():
                if name not in cfg.authorized_strategies:
                    continue
                signal = strategy.analyze(window, symbol, stamp)   # live clock; 24/7 anyway
                if signal is None:
                    continue
                verdict = self.risk.validate_signal(
                    signal, [{"symbol": held} for held in self.positions], day_key=day_key)
                if not verdict["approved"]:
                    self.rejections[verdict["reason"]] += 1
                    continue

                position = Position(symbol=symbol, strategy=name,
                                    direction=signal.direction.value, entry=signal.entry_price,
                                    stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                                    lots=verdict["lot_size"], risk_usd=verdict["risk_usd"],
                                    opened_at=candles[-1]["time"], signal=signal)
                self.positions[symbol] = position
                self.alerts += 1
                self.dispatch.send_alert(signal, verdict)   # card carries [DERIV SYNTHETICS]

        return {"equity": round(self.equity, 2), "alerts": self.alerts,
                "open_positions": len(self.positions), "day": day_key,
                "max_daily_dd_pct": round(100 * self.max_daily_dd_pct, 2),
                "rejections": dict(self.rejections)}

    def run(self, interval: int = 30, symbols: Optional[List[str]] = None,
            max_iterations: Optional[int] = None) -> Dict:
        iterations = 0
        summary: Dict = {}
        while max_iterations is None or iterations < max_iterations:
            summary = self.scan_once(symbols=symbols)
            iterations += 1
            print(f"[SYNTHETICS] scan {iterations} {summary['day']} open={summary['open_positions']} "
                  f"alerts={summary['alerts']} equity={summary['equity']}")
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="PHANTOM-TRADERS 24/7 Deriv synthetic scanner")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true", help="single scan (dry run)")
    args = parser.parse_args()

    scanner = SyntheticScanner(CsvBarFeed(timeframe=args.timeframe), timeframe=args.timeframe)
    summary = scanner.run(interval=args.interval, symbols=args.symbols,
                          max_iterations=1 if args.once else None)
    print("summary:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
