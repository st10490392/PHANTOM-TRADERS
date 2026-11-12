"""
Statistical Mean-Reversion Anchor
Verified 2+ standard deviation stretch alert (high-liquidity assets + Deriv synthetics).
Gating: forex alerts fire only inside London/NY killzones; Deriv synthetics run 24/7.
Edge-triggered with a per-symbol cooldown so a single stretch cannot re-fire every bar.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import Direction, Signal, Strategy
from .sessions import active_session, is_synthetic
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG


class ZScoreAnchor(Strategy):
    NAME = "Z_SCORE"
    LOOKBACK = 100
    ENTRY_Z = 2.0
    EXIT_Z = 0.0
    COOLDOWN_BARS = 96       # one 8h M5 stretch may not re-alert every bar
    MOMENTUM_WINDOW = 20

    def __init__(self) -> None:
        self._bars: Dict[str, int] = {}
        self._armed: Dict[str, bool] = {}
        self._last_fire: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return self.NAME

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear edge-trigger state (call at session/day boundaries or on resync)."""
        if symbol is None:
            self._bars.clear()
            self._armed.clear()
            self._last_fire.clear()
        else:
            self._bars.pop(symbol, None)
            self._armed.pop(symbol, None)
            self._last_fire.pop(symbol, None)

    def _is_synthetic(self, symbol: str) -> bool:
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if cfg is not None and cfg.asset_class == "synthetic":
            return True
        return is_synthetic(symbol)

    def _momentum(self, closes: np.ndarray) -> Tuple[float, float]:
        """Least-squares slope (price/bar) plus residual volatility of the window."""
        window = closes[-self.MOMENTUM_WINDOW:]
        x = np.arange(window.size, dtype=float)
        slope = float(np.polyfit(x, window, 1)[0]) if window.size > 1 else 0.0
        residual = float(np.std(window - (slope * x + window.mean() - slope * x.mean())))
        return slope, residual

    def analyze(self, candles: List[Dict], symbol: str, current_time_utc: datetime) -> Optional[Signal]:
        if len(candles) < self.LOOKBACK:
            return None

        synthetic = self._is_synthetic(symbol)

        # Forex/Gold/Indices bonus alerts are killzone-scoped; synthetics are 24/7.
        session = active_session(current_time_utc, symbol)
        if session is None:
            return None

        closes = np.array([c["close"] for c in candles[-self.LOOKBACK:]], dtype=float)
        mean = float(np.mean(closes))
        std = float(np.std(closes))
        if std == 0:
            return None

        price = float(closes[-1])
        current_z = (price - mean) / std

        bar = self._bars.get(symbol, 0) + 1
        self._bars[symbol] = bar

        # Edge trigger: re-arm only once the stretch has mean-reverted back inside
        # the entry band (|z| < ENTRY_Z); a persistent stretch must not re-alert.
        if abs(current_z) < self.ENTRY_Z:
            self._armed[symbol] = True
        if not self._armed.get(symbol, True):
            return None
        last_fire = self._last_fire.get(symbol)
        if last_fire is not None and bar - last_fire < self.COOLDOWN_BARS:
            return None

        # Mean reversion setup
        if current_z <= -self.ENTRY_Z:
            direction = Direction.BUY
            stop_loss = price - (std * 3.0)  # 3-sigma catastrophic stop
            take_profit = mean               # target is the statistical mean
        elif current_z >= self.ENTRY_Z:
            direction = Direction.SELL
            stop_loss = price + (std * 3.0)
            take_profit = mean
        else:
            return None

        risk = abs(price - stop_loss)
        reward = abs(take_profit - price)
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        self._armed[symbol] = False
        self._last_fire[symbol] = bar

        slope, residual = self._momentum(closes)
        momentum_sigmas = abs(slope) * self.MOMENTUM_WINDOW / std if std else 0.0

        return Signal(
            symbol=symbol,
            direction=direction,
            strategy_tag=self.NAME,
            entry_price=round(price, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            risk_reward=rr,
            metadata={
                "z_score": round(current_z, 3),
                "lookback": self.LOOKBACK,
                "sigma": round(std, 5),
                "momentum_slope": round(slope, 6),
                "momentum_sigmas": round(momentum_sigmas, 2),
                "residual_sigma": round(residual, 5),
                "engine": "DERIV_SYNTHETICS" if synthetic else "FOREX_CORE",
                "session": session,
                "exit_z": self.EXIT_Z,
                "cooldown_bars": self.COOLDOWN_BARS,
                "daily_risk_usd": PROP_CONFIG.BASE_RISK_AMOUNT,
            },
        )
