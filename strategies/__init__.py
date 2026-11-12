"""PHANTOM-TRADERS Strategy Registry"""
from .base import Direction, Signal, Strategy
from .sessions import active_session, in_forex_killzone, is_synthetic
from .smc_ict_killzone import SMCICTKillzone
from .synthetic_momentum import SyntheticMomentum
from .z_score_anchor import ZScoreAnchor

STRATEGY_REGISTRY = {
    SMCICTKillzone.NAME: SMCICTKillzone,
    ZScoreAnchor.NAME: ZScoreAnchor,
    SyntheticMomentum.NAME: SyntheticMomentum,
}

# Engines allowed on the 24/7 Deriv synthetic universe.
SYNTHETIC_STRATEGIES = [SyntheticMomentum.NAME, ZScoreAnchor.NAME]

__all__ = ["Direction", "Signal", "Strategy", "SMCICTKillzone", "ZScoreAnchor",
           "SyntheticMomentum", "STRATEGY_REGISTRY", "SYNTHETIC_STRATEGIES",
           "active_session", "in_forex_killzone", "is_synthetic"]
