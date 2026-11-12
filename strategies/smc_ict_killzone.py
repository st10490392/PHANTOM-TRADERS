"""
SMC/ICT Killzone Engine  (Forex, Gold & Indices)
DST-Aware London & NY Killzones | Prior-Session Liquidity Sweeps
Judas Swing + MSS Displacement | OTE 0.618-0.786 Retracements
ATR-buffered stops behind the sweep extreme, hard 1:2.0 R:R floor.
"""
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
from .base import Direction, Signal, Strategy
from config.settings import PROP_CONFIG, SessionBounds, SYNTHETIC_PREFIXES


class SMCICTKillzone(Strategy):
    NAME = "SMC_FIB"
    MIN_RR = 2.0
    OTE_LEVELS = (0.618, 0.786)
    OTE_TOLERANCE = 1e-9  # float noise must not reject an exact 0.618 leg
    ATR_PERIOD = 14
    MIN_DISPLACEMENT_ATR = 0.5   # MSS leg must be a genuine displacement
    MIN_BODY_RATIO = 0.6         # displacement candle must be a true body, not a wick
    FALLBACK_LOOKBACK = 20

    @property
    def name(self) -> str:
        return self.NAME

    # ---------------- Session / Killzone Filters ----------------
    @staticmethod
    def _bounds_to_times(bounds: SessionBounds) -> Tuple[time, time]:
        return time(*bounds[0]), time(*bounds[1])

    @staticmethod
    def _within(bounds: SessionBounds, local: time) -> bool:
        start, end = SMCICTKillzone._bounds_to_times(bounds)
        return start <= local <= end

    def _active_session(self, dt_utc: datetime, symbol: str) -> Optional[str]:
        """DST-aware killzone gate. Synthetics run 24/7, so they report 'GLOBAL'."""
        if symbol.startswith(SYNTHETIC_PREFIXES):
            return "GLOBAL"

        london_local = dt_utc.astimezone(PROP_CONFIG.TIMEZONE_LONDON).time()
        ny_local = dt_utc.astimezone(PROP_CONFIG.TIMEZONE_NY).time()

        if self._within(PROP_CONFIG.LONDON_KILLZONE, london_local):
            return "LDN"
        if self._within(PROP_CONFIG.NY_KILLZONE, ny_local):
            return "NY"
        return None

    def _prior_session_extremes(self, candles: List[Dict], dt_utc: datetime,
                                 session: str) -> Optional[Tuple[float, float]]:
        """
        Liquidity pool targets: London sweeps the Asia range, NY sweeps the London range.
        Falls back to a lookback window when the backend omits session candles.
        """
        london_local = dt_utc.astimezone(PROP_CONFIG.TIMEZONE_LONDON).time()
        window: Optional[SessionBounds] = None
        if session == "LDN":
            window = PROP_CONFIG.ASIA_SESSION
        elif session == "NY":
            window = PROP_CONFIG.LONDON_SESSION

        pool: List[Dict] = []
        if window is not None:
            start, end = self._bounds_to_times(window)
            for candle in candles[:-2]:
                local = candle["time"].astimezone(PROP_CONFIG.TIMEZONE_LONDON).time()
                if start <= local <= end:
                    pool.append(candle)

        if not pool:
            pool = candles[:-2][-self.FALLBACK_LOOKBACK:]

        if len(pool) < 2:
            return None
        return (max(c["high"] for c in pool), min(c["low"] for c in pool))


    # ---------------- Structure Engines ----------------
    @staticmethod
    def _atr(candles: List[Dict], period: int = ATR_PERIOD) -> float:
        """Simple ATR over closed bars (backend-agnostic, no ta-lib dependency)."""
        window = candles[-period:]
        trs: List[float] = []
        for index, candle in enumerate(window):
            prev_close = window[index - 1]["close"] if index else candle["open"]
            trs.append(max(candle["high"] - candle["low"],
                           abs(candle["high"] - prev_close),
                           abs(candle["low"] - prev_close)))
        return sum(trs) / len(trs) if trs else 0.0

    def _detect_sweep_and_mss(self, candles: List[Dict],
                              extremes: Tuple[float, float]) -> Optional[Dict]:
        """Judas swing sweep of the prior pool, then Market Structure Shift displacement."""
        if len(candles) < self.FALLBACK_LOOKBACK + 2:
            return None

        prior_high, prior_low = extremes
        sweep = candles[-2]
        displacement = candles[-1]
        atr = self._atr(candles)
        if atr <= 0:
            return None

        body = abs(displacement["close"] - displacement["open"])
        span = displacement["high"] - displacement["low"]
        leg = abs(displacement["close"] - sweep["close"])

        displaced = (leg >= atr * self.MIN_DISPLACEMENT_ATR
                     and body >= span * self.MIN_BODY_RATIO)

        if (displaced and sweep["low"] < prior_low and sweep["close"] > prior_low
                and displacement["close"] > displacement["open"]
                and displacement["close"] > sweep["high"]):
            return {"type": "BULLISH_SWEEP", "extreme": prior_low,
                    "leg_end": displacement["close"], "atr": atr}

        if (displaced and sweep["high"] > prior_high and sweep["close"] < prior_high
                and displacement["close"] < displacement["open"]
                and displacement["close"] < sweep["low"]):
            return {"type": "BEARISH_SWEEP", "extreme": prior_high,
                    "leg_end": displacement["open"], "atr": atr}

        return None

    def analyze(self, candles: List[Dict], symbol: str, current_time_utc: datetime) -> Optional[Signal]:
        session = self._active_session(current_time_utc, symbol)
        if session is None:
            return None

        extremes = self._prior_session_extremes(candles, current_time_utc, session)
        if not extremes:
            return None

        structure = self._detect_sweep_and_mss(candles, extremes)
        if not structure:
            return None

        extreme = structure["extreme"]
        leg_end = structure["leg_end"]
        atr_buffer = structure["atr"]
        leg = abs(leg_end - extreme)
        if leg <= 0:
            return None

        # Optimal Trade Entry: 0.618-0.786 retracement of the displacement leg.
        if structure["type"] == "BULLISH_SWEEP":
            direction = Direction.BUY
            entry = leg_end - leg * self.OTE_LEVELS[0]
            stop_loss = extreme - atr_buffer
            take_profit = entry + (entry - stop_loss) * self.MIN_RR
        else:
            direction = Direction.SELL
            entry = leg_end + leg * self.OTE_LEVELS[0]
            stop_loss = extreme + atr_buffer
            take_profit = entry - (stop_loss - entry) * self.MIN_RR

        retracement = abs(leg_end - entry) / leg
        if not (self.OTE_LEVELS[0] - self.OTE_TOLERANCE <= retracement
                <= self.OTE_LEVELS[1] + self.OTE_TOLERANCE):
            return None

        risk = abs(entry - stop_loss)
        if risk <= 0:
            return None

        rr = round(abs(take_profit - entry) / risk, 2)
        if rr < self.MIN_RR:
            return None

        return Signal(
            symbol=symbol,
            direction=direction,
            strategy_tag=self.NAME,
            entry_price=round(entry, 5),
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            risk_reward=rr,
            metadata={
                "session": session,
                "structure": structure["type"],
                "sweep_extreme": round(extreme, 5),
                "ote_retracement": round(retracement, 3),
                "atr_buffer": round(atr_buffer, 5),
            },
        )
