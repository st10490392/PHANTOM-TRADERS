"""PHANTOM-TRADERS Core Engines (lazy imports so `python -m core.backtest` stays clean)."""
import importlib

__all__ = ["RiskManager", "BacktestRunner", "LiveEngine", "SyntheticScanner", "CsvBarFeed",
           "available_symbols", "csv_path", "load_candles", "window_at"]

_EXPORTS = {
    "RiskManager": "core.risk_manager",
    "BacktestRunner": "core.backtest",
    "LiveEngine": "core.engine",
    "SyntheticScanner": "core.engine_synthetics",
    "CsvBarFeed": "core.data_feed",
    "available_symbols": "core.data_feed",
    "csv_path": "core.data_feed",
    "load_candles": "core.data_feed",
    "window_at": "core.data_feed",
}


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'core' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
