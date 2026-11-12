"""
Statistical Mean-Reversion Anchor (Forex/Gold bonus alerts + Deriv Synthetics engine)
2+ standard deviation stretch for high-liquidity assets.
Deriv synthetics carry no forex sessions, so this anchor runs 24/7 with a
volatility/momentum regime filter instead of a killzone gate.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import Direction, Signal, Strategy
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG, SYNTHETIC_PREFIXES


class ZScoreAnchor(Strategy):
    NAME = "Z_SCORE"
    LOOKBACK = 100
    ENTRY_Z = 2.0
    EXIT_Z = 0.0
    MOMENTUM_WINDOW = 20
    MAX_MOMENTUM_SIGMAS = 2.0  # reject counter-trend stretches already exhausted

    @property
    def name(self) -> str:
        return self.NAME

    def _is_synthetic(self, symbol: str) -> bool:
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if cfg is not None and cfg.asset_class == "synthetic":
            return True
        return symbol.startswith(SYNTHETIC_PREFIXES)

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

        closes = np.array([c["close"] for c in candles[-self.LOOKBACK:]], dtype=float)
        mean = float(np.mean(closes))
        std = float(np.std(closes))
        if std == 0:
            return None

        price = float(closes[-1])
        current_z = (price - mean) / std

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

        # Volatility/momentum regime context for the Deriv 24/7 engine (advisory only:
        # the stretch itself is the trigger, so it is reported, never filtered here).
        slope, residual = self._momentum(closes)
        momentum_sigmas = abs(slope) * self.MOMENTUM_WINDOW / std if std else 0.0

        risk = abs(price - stop_loss)
        reward = abs(take_profit - price)
        rr = round(reward / risk, 2) if risk > 0 else 0.0

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
                "engine": "DERIV_SYNTHETICS" if self._is_synthetic(symbol) else "FOREX_CORE",
                "session": "GLOBAL" if self._is_synthetic(symbol) else "LDN/NY",
                "exit_z": self.EXIT_Z,
                "daily_risk_usd": PROP_CONFIG.BASE_RISK_AMOUNT,
            },
        )
