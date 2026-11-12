"""
Deriv Synthetic Momentum / Breakout Engine
Continuous tick behaviour, no session or market-close restrictions.
Rolling-range breakout confirmed by momentum slope, ATR-scaled SL/TP at 1:2 R:R.
Edge-triggered with a per-symbol cooldown so one breakout cannot re-alert every bar.
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .base import Direction, Signal, Strategy
from .sessions import active_session, is_synthetic
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG


class SyntheticMomentum(Strategy):
    NAME = "SYNTH_MOM"
    LOOKBACK = 60          # rolling breakout range (bars)
    ATR_PERIOD = 14
    MOMENTUM_WINDOW = 20
    MIN_BREAKOUT_ATR = 0.6  # distance beyond the rolling extreme
    MIN_SLOPE_ATR = 0.15    # slope must show genuine directional drift
    SL_ATR = 1.5
    TP_ATR = 3.0            # TP_ATR / SL_ATR == 1:2 floor
    COOLDOWN_BARS = 48      # ~4h of M5 ticks

    def __init__(self) -> None:
        self._bars: Dict[str, int] = {}
        self._last_fire: Dict[str, int] = {}

    @property
    def name(self) -> str:
        return self.NAME

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._bars.clear()
            self._last_fire.clear()
        else:
            self._bars.pop(symbol, None)
            self._last_fire.pop(symbol, None)

    @staticmethod
    def _atr(candles: List[Dict], period: int = ATR_PERIOD) -> float:
        window = candles[-period:]
        trs: List[float] = []
        for index, candle in enumerate(window):
            prev_close = window[index - 1]["close"] if index else candle["open"]
            trs.append(max(candle["high"] - candle["low"],
                           abs(candle["high"] - prev_close),
                           abs(candle["low"] - prev_close)))
        return sum(trs) / len(trs) if trs else 0.0

    @staticmethod
    def _slope(closes: List[float], window: int = MOMENTUM_WINDOW) -> float:
        """Least-squares drift per bar (price units / bar)."""
        series = closes[-window:]
        if len(series) < 2:
            return 0.0
        x = list(range(len(series)))
        mean_x = sum(x) / len(x)
        mean_y = sum(series) / len(series)
        numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, series))
        denominator = sum((xi - mean_x) ** 2 for xi in x)
        return numerator / denominator if denominator else 0.0

    def analyze(self, candles: List[Dict], symbol: str, current_time_utc: datetime) -> Optional[Signal]:
        cfg = ACTIVE_SYMBOLS.get(symbol)
        if cfg is None or cfg.asset_class != "synthetic":
            return None      # engine is synthetic-only; forex/gold is handled by SMC/Z-score
        if not is_synthetic(symbol) or self.NAME not in cfg.authorized_strategies:
            return None
        if len(candles) < self.LOOKBACK + 1:
            return None

        session = active_session(current_time_utc, symbol)   # synthetics are always 'GLOBAL'
        if session is None:
            return None

        prior = candles[:-1][-self.LOOKBACK:]
        range_high = max(candle["high"] for candle in prior)
        range_low = min(candle["low"] for candle in prior)
        atr = self._atr(candles)
        if atr <= 0:
            return None

        price = candles[-1]["close"]
        slope = self._slope([candle["close"] for candle in candles])
        bar = self._bars.get(symbol, 0) + 1
        self._bars[symbol] = bar
        last_fire = self._last_fire.get(symbol)
        if last_fire is not None and bar - last_fire < self.COOLDOWN_BARS:
            return None

        up_break = price - range_high >= atr * self.MIN_BREAKOUT_ATR and slope >= atr * self.MIN_SLOPE_ATR
        down_break = range_low - price >= atr * self.MIN_BREAKOUT_ATR and slope <= -atr * self.MIN_SLOPE_ATR

        if up_break:
            direction = Direction.BUY
            stop_loss = price - atr * self.SL_ATR
            take_profit = price + atr * self.TP_ATR
            extreme = range_high
        elif down_break:
            direction = Direction.SELL
            stop_loss = price + atr * self.SL_ATR
            take_profit = price - atr * self.TP_ATR
            extreme = range_low
        else:
            return None

        risk = abs(price - stop_loss)
        if risk <= 0:
            return None
        rr = round(abs(take_profit - price) / risk, 2)
        if rr < 2.0:
            return None

        self._last_fire[symbol] = bar

        return Signal(
            symbol=symbol,
            direction=direction,
            strategy_tag=self.NAME,
            entry_price=round(price, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            risk_reward=rr,
            metadata={
                "engine": "DERIV_SYNTHETICS",
                "session": session,
                "structure": "BREAKOUT_UP" if up_break else "BREAKOUT_DOWN",
                "range_extreme": round(extreme, 5),
                "atr": round(atr, 5),
                "slope_per_bar": round(slope, 6),
                "lookback": self.LOOKBACK,
                "cooldown_bars": self.COOLDOWN_BARS,
                "daily_risk_usd": PROP_CONFIG.BASE_RISK_AMOUNT,
            },
        )
