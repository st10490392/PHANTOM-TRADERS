"""
Offline portfolio replay. Historical bars travel through the same
Signal -> RiskManager -> position lifecycle path used for live execution.

    python -m core.backtest --symbols EURUSD XAUUSD --days 120
"""
import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import ACTIVE_SYMBOLS, DATA_DIR, PROP_CONFIG, RESULTS_DIR
from core.data_feed import available_symbols, load_candles, window_at
from core.risk_manager import RiskManager
from strategies import STRATEGY_REGISTRY

UTC = ZoneInfo("UTC")
SCAN_WINDOW = 200


@dataclass
class Position:
    symbol: str
    strategy: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    lots: float
    risk_usd: float
    opened_at: datetime


@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: str
    entry: float
    exit_price: float
    lots: float
    pnl_usd: float
    r_multiple: float
    reason: str
    opened_at: datetime
    closed_at: datetime


class BacktestRunner:
    def __init__(self, data_dir: Path = DATA_DIR, timeframe: str = "M5",
                 equity: float = PROP_CONFIG.BASE_EQUITY, source: str = "csv"):
        self.data_dir = Path(data_dir)
        self.timeframe = timeframe
        self.source = source
        self.strategies = {name: cls() for name, cls in STRATEGY_REGISTRY.items()}
        self.risk = RiskManager(equity=equity, day_start_equity=equity)
        self.equity = equity
        self.positions: List[Position] = []
        self.trades: List[Trade] = []
        self.signals: Counter = Counter()
        self.rejections: Counter = Counter()
        self.breach_days: List[str] = []
        self.max_daily_dd_pct = 0.0
        self.skipped: List[str] = []
        self._day: Optional[str] = None
        self._day_start = equity

    # ---------------- Daily accounting ----------------
    def _roll_day(self, stamp: datetime) -> None:
        day = stamp.date().isoformat()
        if day != self._day:
            self._day = day
            self._day_start = self.equity          # daily DD anchor resets at UTC midnight

    def _daily_dd_pct(self) -> float:
        if self._day_start <= 0:
            return 1.0
        return max(0.0, (self._day_start - self.equity) / self._day_start)

    def _dd_breached(self) -> bool:
        pct = self._daily_dd_pct()
        self.max_daily_dd_pct = max(self.max_daily_dd_pct, pct)
        if pct >= PROP_CONFIG.MAX_DAILY_DD_PERCENT:
            if self._day not in self.breach_days:
                self.breach_days.append(self._day or "")
            return True
        return False

    # ---------------- Position lifecycle ----------------
    def _manage(self, symbol: str, candle: Dict) -> None:
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if cfg is None:
            return
        for position in [p for p in self.positions if p.symbol == symbol]:
            sell = position.direction == "SELL"
            sl_hit = (candle["high"] >= position.stop_loss if sell
                      else candle["low"] <= position.stop_loss)
            tp_hit = (candle["low"] <= position.take_profit if sell
                      else candle["high"] >= position.take_profit)
            if sl_hit:                       # conservative: stop fills before target
                exit_price, reason = position.stop_loss, "SL"
            elif tp_hit:
                exit_price, reason = position.take_profit, "TP"
            else:
                continue

            move = (exit_price - position.entry) if not sell else (position.entry - exit_price)
            pnl = move * cfg.contract_size * position.lots
            self.trades.append(Trade(symbol=symbol, strategy=position.strategy,
                                     direction=position.direction, entry=position.entry,
                                     exit_price=exit_price, lots=position.lots,
                                     pnl_usd=round(pnl, 2),
                                     r_multiple=round(pnl / position.risk_usd, 3) if position.risk_usd else 0.0,
                                     reason=reason, opened_at=position.opened_at,
                                     closed_at=candle["time"]))
            self.positions.remove(position)
            self.equity += pnl
            self.risk.set_equity(self.equity)   # sanity wrapper refuses stale/extreme feeds

    # ---------------- Signal scan ----------------
    def _scan(self, symbol: str, candles: List[Dict], index: int) -> None:
        if self._dd_breached():
            return
        stamp = candles[index]["time"]
        window = window_at(candles, index, SCAN_WINDOW)
        for name, strategy in self.strategies.items():
            if name not in ACTIVE_SYMBOLS[symbol].authorized_strategies:
                continue
            signal = strategy.analyze(window, symbol, stamp)
            if signal is None:
                continue
            self.signals[(symbol, name)] += 1
            verdict = self.risk.validate_signal(
                signal, [{"symbol": p.symbol} for p in self.positions], day_key=self._day)
            if not verdict["approved"]:
                self.rejections[verdict["reason"]] += 1
                continue
            self.positions.append(Position(symbol=symbol, strategy=name,
                                           direction=signal.direction.value,
                                           entry=signal.entry_price, stop_loss=signal.stop_loss,
                                           take_profit=signal.take_profit,
                                           lots=verdict["lot_size"], risk_usd=verdict["risk_usd"],
                                           opened_at=stamp))

    # ---------------- Replay ----------------
    def run(self, symbols: Optional[List[str]] = None, days: Optional[int] = None,
            start: Optional[datetime] = None, end: Optional[datetime] = None) -> List[Dict]:
        universe = symbols or available_symbols(self.data_dir, self.timeframe)
        self.skipped = [symbol for symbol in universe if symbol not in ACTIVE_SYMBOLS]
        series: Dict[str, List[Dict]] = {}

        for symbol in universe:
            if symbol not in ACTIVE_SYMBOLS:
                continue                       # unauthorized assets never enter the book
            if days is not None and start is None and end is None:
                end = datetime.now(UTC).replace(microsecond=0)
                start = end - timedelta(days=days)
            bars = load_candles(symbol, self.timeframe, self.data_dir, start=start, end=end)
            if bars:
                series[symbol] = bars

        timeline = sorted((bars[index]["time"], symbol, index)
                          for symbol, bars in series.items() for index in range(len(bars)))
        for stamp, symbol, index in timeline:
            self._roll_day(stamp)
            self._manage(symbol, series[symbol][index])
            self._scan(symbol, series[symbol], index)

        for position in list(self.positions):          # flatten anything still open
            cfg = ACTIVE_SYMBOLS[position.symbol]
            last_bar = series[position.symbol][-1]
            sell = position.direction == "SELL"
            move = (last_bar["close"] - position.entry) if not sell else (position.entry - last_bar["close"])
            pnl = move * cfg.contract_size * position.lots
            self.trades.append(Trade(symbol=position.symbol, strategy=position.strategy,
                                     direction=position.direction, entry=position.entry,
                                     exit_price=last_bar["close"], lots=position.lots,
                                     pnl_usd=round(pnl, 2),
                                     r_multiple=round(pnl / position.risk_usd, 3) if position.risk_usd else 0.0,
                                     reason="FLAT_AT_END", opened_at=position.opened_at,
                                     closed_at=last_bar["time"]))
            self.positions.remove(position)
            self.equity += pnl

        return self._records()

    def _records(self) -> List[Dict]:
        records: List[Dict] = []
        for (symbol, strategy), count in sorted(self.signals.items()):
            trades = [t for t in self.trades if t.symbol == symbol and t.strategy == strategy]
            wins = sum(1 for t in trades if t.pnl_usd > 0)
            records.append({
                "symbol": symbol, "asset_class": ACTIVE_SYMBOLS[symbol].asset_class,
                "strategy": strategy, "source": self.source, "status": "complete",
                "signals": count, "trades": len(trades), "wins": wins,
                "win_rate_pct": round(100 * wins / len(trades), 2) if trades else 0.0,
                "net_r": round(sum(t.r_multiple for t in trades), 3),
                "net_usd": round(sum(t.pnl_usd for t in trades), 2),
                "avg_lot": round(sum(t.lots for t in trades) / len(trades), 3) if trades else 0.0,
                "sl_hits": sum(1 for t in trades if t.reason == "SL"),
                "tp_hits": sum(1 for t in trades if t.reason == "TP"),
            })
        records.append({
            "summary": True, "source": self.source, "timeframe": self.timeframe,
            "final_equity": round(self.equity, 2),
            "net_usd": round(self.equity - PROP_CONFIG.BASE_EQUITY, 2),
            "trades": len(self.trades),
            "wins": sum(1 for t in self.trades if t.pnl_usd > 0),
            "max_daily_dd_pct": round(100 * self.max_daily_dd_pct, 2),
            "dd_breach_days": self.breach_days,
            "rejected_signals": dict(self.rejections),
            "universe_skipped": self.skipped,
        })
        return records


def main() -> int:
    parser = argparse.ArgumentParser(description="PHANTOM-TRADERS historical replay")
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--source", default="csv")
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()

    runner = BacktestRunner(args.data_dir, args.timeframe, source=args.source)
    records = runner.run(args.symbols, days=args.days)

    for record in records:
        if record.get("summary"):
            print(f"SUMMARY equity={record['final_equity']} net_usd={record['net_usd']} "
                  f"trades={record['trades']} wins={record['wins']} "
                  f"max_daily_dd={record['max_daily_dd_pct']}% breach_days={len(record['dd_breach_days'])}")
            if record["rejected_signals"]:
                print("rejections:", record["rejected_signals"])
            if record["universe_skipped"]:
                print("skipped (outside authorized universe):", record["universe_skipped"])
        else:
            print(f"{record['symbol']:8} {record['strategy']:8} signals={record['signals']:5} "
                  f"trades={record['trades']:3} win={record['win_rate_pct']:5.1f}% "
                  f"net_r={record['net_r']:7.2f} net_usd={record['net_usd']:8.2f} "
                  f"SL={record['sl_hits']} TP={record['tp_hits']}")

    if not args.no_write:
        path = Path(args.results_dir) / "backtest_results.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, indent=2))
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
