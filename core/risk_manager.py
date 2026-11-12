"""
Prop Firm Risk Manager
Enforces correlation caps, daily DD limits, and precise lot sizing
"""
from typing import Dict, List, Optional
from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG, PropFirmConfig
from strategies.base import Signal, Direction

# Simplified currency correlation mapping.
# Expand quarterly as the universe grows (cross-pairs share two currencies).
CURRENCY_MAP = {
    "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"], "EURGBP": ["EUR", "GBP"],
    "XAUUSD": ["XAU", "USD"], "US30": ["USD", "IDX"],
    "VOL75": ["SYNTH"], "VOL100": ["SYNTH"], "JUMP100": ["SYNTH"],
}


class RiskManager:
    EQUITY_SANITY_BAND = 0.25  # +/-25% of base equity before trusting live feeds

    def __init__(self, equity: float = PROP_CONFIG.BASE_EQUITY,
                 day_start_equity: Optional[float] = None):
        self.equity = equity
        self.day_start_equity = day_start_equity if day_start_equity is not None else equity
        self.config: PropFirmConfig = PROP_CONFIG
        self.risk_amount = self._risk_budget(equity)

    def _risk_budget(self, equity: float) -> float:
        """0.25%-0.50% band applied to equity, never wider than the base $15 model."""
        percent = min(self.config.MAX_RISK_PERCENT,
                      max(self.config.MIN_RISK_PERCENT, self.config.RISK_PERCENT))
        return round(equity * percent, 2)

    def set_equity(self, equity: float) -> bool:
        """
        Sanity wrapper for live balance feeds. Prop firm APIs can return stale or
        news-skewed balances; anything outside the band keeps the base $5k model.
        """
        if equity <= 0:
            return False
        drift = abs(equity - self.config.BASE_EQUITY) / self.config.BASE_EQUITY
        if drift > self.EQUITY_SANITY_BAND:
            return False
        self.equity = equity
        self.risk_amount = self._risk_budget(equity)
        return True

    def daily_dd_breach(self) -> bool:
        """True once realized+floating drawdown reaches the 4% hard breach buffer."""
        if self.day_start_equity <= 0:
            return True
        drawdown = (self.day_start_equity - self.equity) / self.day_start_equity
        return drawdown >= self.config.MAX_DAILY_DD_PERCENT

    def calculate_lot_size(self, signal: Signal) -> float:
        """Calculate exact lot size based on the risk budget and SL distance."""
        cfg = ACTIVE_SYMBOLS.get(signal.symbol)
        if not cfg:
            return 0.0

        sl_distance = abs(signal.entry_price - signal.stop_loss)
        if sl_distance == 0:
            return 0.0

        # Value per point: point_value * contract_size, scaled by stop distance.
        tick_value = cfg.point_value * cfg.contract_size
        points = sl_distance / cfg.point_value
        risk_per_lot = points * tick_value
        if risk_per_lot <= 0:
            return 0.0

        raw_lots = self.risk_amount / risk_per_lot
        step = cfg.min_lot
        lots = round(raw_lots / step) * step  # snap to broker lot step
        return max(cfg.min_lot, min(cfg.max_lot, round(lots, 2)))

    def validate_correlation_cap(self, signal: Signal, open_positions: List[Dict]) -> bool:
        """
        Enforce max 2 positions sharing the same underlying currency.
        Prevents overexposure to USD/EUR/etc. during prop firm evaluation.
        """
        signal_currencies = set(CURRENCY_MAP.get(signal.symbol, []))
        currency_exposure: Dict[str, int] = {}

        for pos in open_positions:
            for curr in CURRENCY_MAP.get(pos["symbol"], []):
                currency_exposure[curr] = currency_exposure.get(curr, 0) + 1

        for curr in signal_currencies:
            if currency_exposure.get(curr, 0) + 1 > self.config.MAX_CORRELATED_POSITIONS:
                return False

        return True

    def validate_signal(self, signal: Signal, open_positions: List[Dict]) -> Dict:
        """Master validation gate before execution/dispatch."""
        result = {"approved": False, "reason": "", "lot_size": 0.0, "risk_usd": self.risk_amount}

        if self.daily_dd_breach():
            result["reason"] = f"Daily drawdown limit hit ({self.config.MAX_DAILY_DD_PERCENT:.0%} buffer)"
            return result

        if len(open_positions) >= self.config.MAX_OPEN_POSITIONS:
            result["reason"] = f"Max open positions reached ({self.config.MAX_OPEN_POSITIONS})"
            return result

        if not signal.geometry_valid():
            result["reason"] = "Invalid SL/TP geometry for direction"
            return result

        if not self.validate_correlation_cap(signal, open_positions):
            result["reason"] = "Correlation cap breached (max 2 shared currency)"
            return result

        if signal.risk_reward < 2.0:
            result["reason"] = "Signal R:R below 1:2.0 floor"
            return result

        lot_size = self.calculate_lot_size(signal)
        if lot_size <= 0:
            result["reason"] = "Invalid lot size calculation"
            return result

        if signal.symbol not in ACTIVE_SYMBOLS:
            result["reason"] = "Symbol outside active authorized universe"
            return result

        strategies = ACTIVE_SYMBOLS[signal.symbol].authorized_strategies
        if signal.strategy_tag not in strategies:
            result["reason"] = f"Strategy '{signal.strategy_tag}' not authorized for {signal.symbol}"
            return result

        result.update({"approved": True, "lot_size": lot_size, "risk_usd": self.risk_amount})
        return result
