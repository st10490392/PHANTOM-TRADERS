"""
Atomic file-queue execution bridge between the Python engines and MT5.

Outbound:  <files_dir>/orders/<signal_id>.json   written as .tmp then os.replace()
Inbound:   <files_dir>/fills/<signal_id>.json    written by PhantomExecutioner.mq5

Both sides speak the same small JSON contract, and the EA's FILE_COMMON root is the
terminal's MQL5/Files folder, so PHANTOM_MT5_FILES_DIR must point at that folder.
"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config.settings import ACTIVE_SYMBOLS, MT5_FILES_DIR, MT5_MAGIC, PROP_CONFIG, broker_symbol, internal_symbol
from strategies.base import Signal

UTC = ZoneInfo("UTC")
LIFECYCLE_EVENTS = {"ENTRY", "FILLED", "BREAK_EVEN", "TP", "SL", "REJECTED"}


@dataclass
class Fill:
    signal_id: str
    event: str
    symbol: str
    price: Optional[float] = None
    volume: Optional[float] = None
    pnl: Optional[float] = None
    note: str = ""


class MT5FileBridge:
    def __init__(self, root: Path = MT5_FILES_DIR, magic: int = MT5_MAGIC):
        self.root = Path(root)
        self.orders_dir = self.root / "orders"
        self.fills_dir = self.root / "fills"
        self.magic = magic
        self.orders_dir.mkdir(parents=True, exist_ok=True)
        self.fills_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- Outbound orders ----------------
    @staticmethod
    def points(distance: float, symbol: str) -> int:
        """Price distance -> broker points, using the symbol's point size."""
        cfg = ACTIVE_SYMBOLS.get(symbol)
        point = cfg.point_value if cfg else 0.00001
        return int(round(abs(distance) / point))

    def order_payload(self, signal: Signal, verdict: Dict) -> Dict:
        entry, stop, target = signal.entry_price, signal.stop_loss, signal.take_profit
        return {
            "signal_id": signal.signal_id,
            "symbol": broker_symbol(signal.symbol),
            "internal_symbol": signal.symbol,
            "action": signal.direction.value,
            "volume": round(float(verdict["lot_size"]), 2),
            "sl": self.points(entry - stop, signal.symbol),
            "tp": self.points(target - entry, signal.symbol),
            "magic": self.magic,
            "strategy": signal.strategy_tag,
            "entry": entry,
            "risk_usd": round(float(verdict.get("risk_usd", PROP_CONFIG.BASE_RISK_AMOUNT)), 2),
            "created_at": datetime.now(UTC).isoformat(),
        }

    def write_order(self, signal: Signal, verdict: Dict) -> Path:
        """Atomic write: temp file + os.replace, so the EA never reads a partial file."""
        payload = self.order_payload(signal, verdict)
        final = self.orders_dir / f"{payload['signal_id']}.json"
        temp = self.orders_dir / f"{payload['signal_id']}.tmp"
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, final)          # atomic on the same volume
        return final

    # ---------------- Inbound execution reports ----------------
    def poll_fills(self) -> List[Fill]:
        """Drain fills/ and consume each receipt exactly once."""
        fills: List[Fill] = []
        for path in sorted(self.fills_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                path.rename(path.with_suffix(".bad"))   # quarantine, don't poison-loop
                print(f"[BRIDGE] unreadable fill {path.name}: {exc}")
                continue
            path.unlink()
            event = str(raw.get("event", "")).upper()
            if event not in LIFECYCLE_EVENTS:
                print(f"[BRIDGE] ignoring unknown fill event {event!r} ({path.name})")
                continue
            fills.append(Fill(
                signal_id=str(raw.get("signal_id", "")),
                event=event,
                symbol=str(raw.get("symbol", "")),
                price=raw.get("price"),
                volume=raw.get("volume"),
                pnl=raw.get("pnl"),
                note=str(raw.get("note", "")),
            ))
        return fills

    def write_fill(self, fill: Fill) -> Path:      # test/emergency parity with the EA
        payload = {"signal_id": fill.signal_id, "event": fill.event, "symbol": fill.symbol,
                   "price": fill.price, "volume": fill.volume, "pnl": fill.pnl,
                   "note": fill.note, "created_at": datetime.now(UTC).isoformat()}
        final = self.fills_dir / f"{fill.signal_id}-{fill.event}.json"
        temp = final.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temp, final)
        return final


def build_bridge(enabled: bool, root: Optional[Path] = None) -> Optional[MT5FileBridge]:
    return MT5FileBridge(root or MT5_FILES_DIR) if enabled else None


class ExecutionBridge:
    """
    Shared execution plumbing for LiveEngine and SyntheticScanner: submit approved
    signals as order files, then drain receipts and emit lifecycle cards. Engines keep
    their own equity accounting; closes are handed back as (fill, position) pairs.
    """

    CLOSE_EVENTS = {"TP", "SL"}

    def __init__(self, bridge: MT5FileBridge, dispatcher):
        self.bridge = bridge
        self.dispatch = dispatcher
        self.by_signal: Dict[str, str] = {}      # signal_id -> internal symbol tag
        self.rejected: List[str] = []            # drained by the runner each tick

    def submit(self, signal: Signal, verdict: Dict) -> Optional[Path]:
        path = self.bridge.write_order(signal, verdict)
        self.by_signal[signal.signal_id] = signal.symbol
        return path

    def ingest(self, positions: Dict[str, object]) -> List[Dict]:
        """Returns closed positions: {'fill': Fill, 'position': Position, 'pnl': float}."""
        closes: List[Dict] = []
        self.rejected = []
        for fill in self.bridge.poll_fills():
            tag = self.by_signal.get(fill.signal_id) or internal_symbol(fill.symbol)
            position = positions.get(tag)
            if position is None:
                print(f"[BRIDGE] fill {fill.event} for unknown position {tag} "
                      f"({fill.signal_id}); card skipped")
                continue

            if fill.event == "REJECTED":
                positions.pop(tag, None)
                self.by_signal.pop(fill.signal_id, None)
                reason = fill.note or "terminal rejected order"
                self.rejected.append(f"{tag}: {reason}")
                self.dispatch.send_lifecycle("REJECTED", position.signal, fill.price,
                                             None, reason)
                continue

            if fill.event in self.CLOSE_EVENTS:
                pnl = float(fill.pnl) if fill.pnl is not None else 0.0
                positions.pop(tag, None)
                self.by_signal.pop(fill.signal_id, None)
                self.dispatch.send_lifecycle(fill.event, position.signal, fill.price,
                                             round(pnl, 2), fill.note or f"{tag} closed on {fill.event}")
                closes.append({"fill": fill, "position": position, "pnl": pnl})
                continue

            self.dispatch.send_lifecycle(fill.event, position.signal, fill.price, None,
                                         fill.note or f"{tag} {fill.event}")
        return closes


def build_execution(enabled: bool, dispatcher, root: Optional[Path] = None) -> Optional[ExecutionBridge]:
    bridge = build_bridge(enabled, root)
    return ExecutionBridge(bridge, dispatcher) if bridge else None