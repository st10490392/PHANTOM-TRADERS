"""PHANTOM-TRADERS Strategy Registry"""
from .base import Direction, Signal, Strategy
from .sessions import active_session, in_forex_killzone, is_synthetic
from .smc_ict_killzone import SMCICTKillzone
from .z_score_anchor import ZScoreAnchor

STRATEGY_REGISTRY = {
    SMCICTKillzone.NAME: SMCICTKillzone,
    ZScoreAnchor.NAME: ZScoreAnchor,
}

__all__ = ["Direction", "Signal", "Strategy", "SMCICTKillzone", "ZScoreAnchor",
           "STRATEGY_REGISTRY", "active_session", "in_forex_killzone", "is_synthetic"]
