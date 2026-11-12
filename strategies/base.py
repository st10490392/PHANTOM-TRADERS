"""Abstract Base Strategy Contract"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Signal:
    symbol: str
    direction: Direction
    strategy_tag: str
    entry_price: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Correlation id shared with the MT5 bridge so fill receipts map back to the signal.
    signal_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def geometry_valid(self) -> bool:
        """Stop/target must sit on the correct side of entry for the direction."""
        if self.direction is Direction.BUY:
            return self.stop_loss < self.entry_price < self.take_profit
        return self.stop_loss > self.entry_price > self.take_profit


class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def analyze(self, candles: List[Dict], symbol: str, current_time_utc) -> Optional[Signal]:
        """
        Analyze market data and return a Signal or None.
        Candles expected format: [{'time': datetime, 'open': float, 'high': float,
                                   'low': float, 'close': float, 'volume': int}]
        """
        pass
