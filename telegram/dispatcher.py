"""
Telegram Alert Dispatcher
Structured Markdown Cards for PHANTOM-TRADERS Lifecycle
"""
import requests
from typing import Dict, Optional
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from strategies.base import Signal


class TelegramDispatcher:
    BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    LIFECYCLE_ICONS = {
        "SIGNAL": "📡", "ENTRY": "🟢", "FILLED": "✅",
        "BREAK_EVEN": "🛡", "TP": "💰", "SL": "🛑", "FLAT": "⚖",
    }
    # Strategy identity markers so the channel reads at a glance.
    STRATEGY_ICONS = {"SMC_FIB": "⚡", "Z_SCORE": "🧠", "SYNTH_MOM": "📈"}
    SYNTHETIC_TAG = "[DERIV SYNTHETICS]"

    @classmethod
    def strategy_icon(cls, strategy_tag: str) -> str:
        return cls.STRATEGY_ICONS.get(strategy_tag, "📡")

    @staticmethod
    def _is_synthetic(signal: Signal) -> bool:
        return signal.metadata.get("engine") == "DERIV_SYNTHETICS"

    @classmethod
    def format_signal_card(cls, signal: Signal, risk_data: Dict) -> str:
        icon = "🟢" if signal.direction.value == "BUY" else "🔴"
        badge = cls.strategy_icon(signal.strategy_tag)
        engine = f"\n{cls.SYNTHETIC_TAG}" if cls._is_synthetic(signal) else ""

        text = f"""
*{icon} PHANTOM SIGNAL | {signal.symbol}*
━━━━━━━━━━━━━━━━━━━━
*{badge} Strategy:* `{signal.strategy_tag}`
*Direction:* `{signal.direction.value}`
*R:R Ratio:* `{signal.risk_reward}`

*Entry:* `{signal.entry_price}`
*Stop Loss:* `{signal.stop_loss}`
*Take Profit:* `{signal.take_profit}`
━━━━━━━━━━━━━━━━━━━━
*Lot Size:* `{risk_data.get('lot_size', 0.0)}`
*Risk Budget:* `${risk_data.get('risk_usd', 0.0)}`
_Session:_ `{signal.metadata.get('session', 'N/A')}`
```{signal.metadata}`{engine}
        """.strip()
        return text

    @classmethod
    def format_lifecycle_card(cls, event: str, signal: Signal, fill_price: Optional[float] = None,
                              pnl: Optional[float] = None, note: str = "") -> str:
        """Lifecycle updates: ENTRY -> FILLED -> BREAK_EVEN -> TP/SL."""
        icon = cls.LIFECYCLE_ICONS.get(event, "ℹ")
        badge = cls.strategy_icon(signal.strategy_tag)
        engine = f" {cls.SYNTHETIC_TAG}" if cls._is_synthetic(signal) else ""
        lines = [
            f"{icon} PHANTOM LIFECYCLE | `{event}` | {signal.symbol}{engine}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"*{badge} Strategy:* `{signal.strategy_tag}`  *Direction:* `{signal.direction.value}`",
            f"*Signal Entry:* `{signal.entry_price}`",
        ]
        if fill_price is not None:
            lines.append(f"*Filled At:* `{fill_price}`")
        if pnl is not None:
            lines.append(f"*Closed PnL:* `${pnl:.2f}`")
        if note:
            lines.append(f"*Note:* {note}")
        lines.append(f"*SL/TP:* `{signal.stop_loss}` / `{signal.take_profit}`")
        return "\n".join(lines)

    @classmethod
    def _post(cls, text: str) -> bool:
        if (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID
                or TELEGRAM_BOT_TOKEN.startswith("YOUR_") or TELEGRAM_CHAT_ID.startswith("YOUR_")):
            print("[TELEGRAM DISPATCH SKIPPED] bot token / chat id not configured")
            return False

        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "MARKDOWN"}
        try:
            resp = requests.post(f"{cls.BASE_URL}/sendMessage", json=payload, timeout=10)
            return resp.status_code == 200
        except Exception as exc:  # never let an alert outage stall the pipeline
            print(f"[TELEGRAM DISPATCH ERROR] {exc}")
            return False

    @classmethod
    def send_alert(cls, signal: Signal, risk_data: Dict) -> bool:
        return cls._post(cls.format_signal_card(signal, risk_data))

    @classmethod
    def send_lifecycle(cls, event: str, signal: Signal, fill_price: Optional[float] = None,
                       pnl: Optional[float] = None, note: str = "") -> bool:
        return cls._post(cls.format_lifecycle_card(event, signal, fill_price, pnl, note))
