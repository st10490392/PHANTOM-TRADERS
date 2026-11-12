"""
PHANTOM-TRADERS Unit Test Suite
Covers Killzones, Risk Math, Correlation Caps, and Formatting
"""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List, Optional
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config.settings import (ACTIVE_SYMBOLS, MT5_MAGIC, PROP_CONFIG, SYNTHETIC_UNIVERSE,
                             broker_symbol, internal_symbol)
from core.backtest import BacktestRunner
from core.data_feed import CsvBarFeed, available_symbols, csv_path, load_candles
from core.engine import LiveEngine
from core.engine_synthetics import SyntheticScanner
from core.mt5_bridge import ExecutionBridge, Fill, MT5FileBridge, build_execution
from core.mt5_feed import MT5BarFeed, build_feed
from core.risk_manager import RiskManager
from strategies import sessions as sessions_module
from strategies.base import Direction, Signal
from strategies.sessions import active_session
from strategies.smc_ict_killzone import SMCICTKillzone
from strategies.synthetic_momentum import SyntheticMomentum
from strategies.z_score_anchor import ZScoreAnchor
from telegram import dispatcher as dispatcher_module
from telegram.dispatcher import TelegramDispatcher

UTC = ZoneInfo("UTC")


def candles_from(closes: List[float], start: datetime, step=timedelta(minutes=15),
                 spread: float = 0.005) -> List[Dict]:
    """Build OHLC bars with deterministic wicks around each close."""
    candles: List[Dict] = []
    for index, close in enumerate(closes):
        open_ = closes[index - 1] if index else close
        candles.append({
            "time": start + step * index,
            "open": open_,
            "high": max(open_, close) + spread / 2,
            "low": min(open_, close) - spread / 2,
            "close": close,
            "volume": 40 + index,
        })
    return candles


class TestPhantomInfrastructure(unittest.TestCase):

    def setUp(self):
        self.risk_mgr = RiskManager(equity=5000.00)
        self.smc = SMCICTKillzone()
        self.zscore = ZScoreAnchor()

    # ---------------- Risk Manager ----------------
    def test_base_risk_budget(self):
        """Verify 0.30% of $5k equals $15.00 inside the 0.25%-0.50% band"""
        self.assertAlmostEqual(self.risk_mgr.risk_amount, 15.00, places=2)
        self.assertGreaterEqual(self.risk_mgr.risk_amount,
                                PROP_CONFIG.BASE_EQUITY * PROP_CONFIG.MIN_RISK_PERCENT)
        self.assertLessEqual(self.risk_mgr.risk_amount,
                             PROP_CONFIG.BASE_EQUITY * PROP_CONFIG.MAX_RISK_PERCENT)

    def test_lot_size_calculation_eurusd(self):
        """Test precise lot sizing for a 10-pip SL on EURUSD"""
        signal = Signal(
            symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
            entry_price=1.10000, stop_loss=1.09900, take_profit=1.10200, risk_reward=2.0
        )
        lots = self.risk_mgr.calculate_lot_size(signal)
        # $15 risk / (10 pips * $10/pip standard) = 0.15 lots
        self.assertEqual(lots, 0.15)
        # Realized risk at the traded lot must never exceed the risk budget
        # (epsilon absorbs float noise: 0.15 * 100000 * 0.001 == 15.0000000000017).
        realized = lots * ACTIVE_SYMBOLS["EURUSD"].contract_size * (signal.entry_price - signal.stop_loss)
        self.assertLessEqual(realized, self.risk_mgr.risk_amount + 1e-6)

    def test_lot_size_gold_index_and_max_lot_cap(self):
        """Metals/indices size correctly and synthetics respect broker max_lot"""
        gold = Signal(symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                      entry_price=2650.00, stop_loss=2640.00, take_profit=2670.00, risk_reward=2.0)
        # $1000 risk per lot => raw 0.015, floored to the 0.01 broker step (never rounded up)
        self.assertEqual(self.risk_mgr.calculate_lot_size(gold), 0.01)
        # Realized risk at the floored lot must stay inside the $15 budget
        self.assertLessEqual(0.01 * ACTIVE_SYMBOLS["XAUUSD"].contract_size * 10.0,
                             self.risk_mgr.risk_amount)

        index = Signal(symbol="US30", direction=Direction.SELL, strategy_tag="SMC_FIB",
                       entry_price=23000.0, stop_loss=23100.0, take_profit=22800.0, risk_reward=2.0)
        self.assertEqual(self.risk_mgr.calculate_lot_size(index), 0.15)

        synthetic = Signal(symbol="VOL75", direction=Direction.BUY, strategy_tag="Z_SCORE",
                           entry_price=10.0, stop_loss=8.0, take_profit=14.0, risk_reward=2.0)
        # $2/lot risk => 7.5 raw lots, capped at max_lot 1.0 (cap only ever reduces risk)
        self.assertEqual(self.risk_mgr.calculate_lot_size(synthetic), 1.0)

    def test_wide_stop_is_rejected_instead_of_over_leveraged(self):
        """A stop that needs less than broker min_lot must be skipped, not upsized"""
        wide = Signal(symbol="USDJPY", direction=Direction.SELL, strategy_tag="SMC_FIB",
                      entry_price=157.000, stop_loss=157.500, take_profit=156.000, risk_reward=2.0)
        # $5000 risk per lot -> raw 0.003 lots, below the 0.01 broker step
        self.assertEqual(self.risk_mgr.calculate_lot_size(wide), 0.0)
        verdict = self.risk_mgr.validate_signal(wide, [])
        self.assertFalse(verdict["approved"])
        self.assertIn("exceeds the risk budget", verdict["reason"])

    def test_correlation_cap_enforcement(self):
        """Ensure 3rd USD position is rejected"""
        open_pos = [{"symbol": "EURUSD"}, {"symbol": "XAUUSD"}]
        new_signal = Signal(
            symbol="GBPUSD", direction=Direction.SELL, strategy_tag="Z_SCORE",
            entry_price=1.25000, stop_loss=1.25200, take_profit=1.24600, risk_reward=2.0
        )
        self.assertFalse(self.risk_mgr.validate_correlation_cap(new_signal, open_pos))
        self.assertTrue(self.risk_mgr.validate_correlation_cap(new_signal, [{"symbol": "EURUSD"}]))

    def test_daily_drawdown_gate_blocks_new_trades(self):
        """4% daily breach buffer halts the pipeline before a trade is taken"""
        halted = RiskManager(equity=4800.00, day_start_equity=5000.00)
        self.assertTrue(halted.daily_dd_breach())

        signal = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                        entry_price=1.10000, stop_loss=1.09900, take_profit=1.10200, risk_reward=2.0)
        verdict = halted.validate_signal(signal, [])
        self.assertFalse(verdict["approved"])
        self.assertIn("drawdown", verdict["reason"])

        live = RiskManager(equity=4850.00, day_start_equity=5000.00)
        self.assertTrue(live.validate_signal(signal, [])["approved"])

    def test_position_count_and_geometry_gates(self):
        """Max 2 concurrent setups and SL/TP must bracket entry by direction"""
        signal = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                        entry_price=1.10000, stop_loss=1.09900, take_profit=1.10200, risk_reward=2.0)
        two_open = [{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}]
        verdict = self.risk_mgr.validate_signal(signal, two_open)
        self.assertFalse(verdict["approved"])
        self.assertIn("Max open positions", verdict["reason"])

        broken = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                        entry_price=1.10000, stop_loss=1.10100, take_profit=1.10200, risk_reward=2.0)
        self.assertFalse(self.risk_mgr.validate_signal(broken, [])["approved"])

    def test_equity_sanity_wrapper_rejects_stale_feed(self):
        """News spikes / stale balances must never re-size the risk model"""
        self.assertFalse(self.risk_mgr.set_equity(12000.00))
        self.assertAlmostEqual(self.risk_mgr.risk_amount, 15.00, places=2)
        self.assertTrue(self.risk_mgr.set_equity(5100.00))
        self.assertAlmostEqual(self.risk_mgr.risk_amount, 15.30, places=2)

    def test_strategy_authorization_gate(self):
        """VOL75 only accepts Z_SCORE; SMC_FIB must be rejected"""
        synthetic = Signal(symbol="VOL75", direction=Direction.BUY, strategy_tag="SMC_FIB",
                           entry_price=100.0, stop_loss=99.0, take_profit=102.0, risk_reward=2.0)
        verdict = self.risk_mgr.validate_signal(synthetic, [])
        self.assertFalse(verdict["approved"])
        self.assertIn("not authorized", verdict["reason"])

    # ---------------- SMC/ICT Killzone Engine ----------------
    def test_killzone_dst_awareness(self):
        """London killzone accepts 08:00 UTC in Winter and 08:00 UTC in Summer"""
        winter = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)  # UTC+0 -> 08:00 London
        summer = datetime(2025, 7, 15, 8, 0, tzinfo=UTC)  # UTC+1 -> 09:00 London
        self.assertEqual(self.smc._active_session(winter, "EURUSD"), "LDN")
        self.assertEqual(self.smc._active_session(summer, "EURUSD"), "LDN")

        self.assertIsNone(self.smc._active_session(datetime(2025, 1, 15, 6, 59, tzinfo=UTC), "EURUSD"))
        self.assertIsNone(self.smc._active_session(datetime(2025, 1, 15, 10, 1, tzinfo=UTC), "EURUSD"))
        self.assertEqual(self.smc._active_session(datetime(2025, 1, 15, 7, 0, tzinfo=UTC), "EURUSD"), "LDN")

    def test_ny_killzone_dst_shift(self):
        """NY 13:30-16:30 local shifts with DST: 18:30 UTC winter, 17:30 UTC summer"""
        winter = datetime(2025, 1, 15, 18, 30, tzinfo=UTC)  # 13:30 New York
        summer = datetime(2025, 7, 15, 17, 30, tzinfo=UTC)  # 13:30 New York
        self.assertEqual(self.smc._active_session(winter, "XAUUSD"), "NY")
        self.assertEqual(self.smc._active_session(summer, "XAUUSD"), "NY")
        self.assertIsNone(self.smc._active_session(datetime(2025, 1, 15, 14, 30, tzinfo=UTC), "XAUUSD"))

    def test_synthetic_killzone_bypass(self):
        """Deriv synthetics run 24/7 with no session filter"""
        self.assertEqual(self.smc._active_session(datetime(2025, 1, 15, 0, 30, tzinfo=UTC), "VOL75"), "GLOBAL")

    def _structure_candles(self, start: datetime) -> List[Dict]:
        """Asia session range swept by a Judas wick, then a bullish MSS displacement."""
        base = candles_from([1.1000 + 0.0004 * (i % 4) for i in range(20)], start, spread=0.0010)
        sweep = {"time": start + timedelta(hours=1), "open": 1.1010, "high": 1.1012,
                 "low": 1.0950, "close": 1.1000, "volume": 90}
        displacement = {"time": start + timedelta(hours=2), "open": 1.1000, "high": 1.1045,
                        "low": 1.0997, "close": 1.1040, "volume": 120}
        return base + [sweep, displacement]

    def test_smc_signal_generation_ote_and_rr(self):
        """Sweep + MSS produces an OTE entry at 0.618 with exactly 1:2 R:R"""
        london_morning = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)  # winter 08:00 London
        candles = self._structure_candles(datetime(2025, 1, 15, 2, 0, tzinfo=UTC))

        signal = self.smc.analyze(candles, "EURUSD", london_morning)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, Direction.BUY)
        self.assertEqual(signal.strategy_tag, "SMC_FIB")
        self.assertEqual(signal.risk_reward, 2.0)
        self.assertEqual(signal.metadata["session"], "LDN")
        self.assertEqual(signal.metadata["structure"], "BULLISH_SWEEP")
        self.assertAlmostEqual(signal.metadata["ote_retracement"], 0.618, places=3)
        self.assertTrue(signal.geometry_valid())
        # ATR-buffered stop sits behind the swept low
        self.assertLess(signal.stop_loss, signal.metadata["sweep_extreme"])
        self.assertLess(signal.entry_price, signal.take_profit)
        self.assertGreater(signal.metadata["atr_buffer"], 0.0)

    def test_smc_rejects_out_of_killzone(self):
        london_afternoon = datetime(2025, 1, 15, 12, 0, tzinfo=UTC)  # 12:00 London -> outside
        candles = self._structure_candles(datetime(2025, 1, 15, 2, 0, tzinfo=UTC))
        self.assertIsNone(self.smc.analyze(candles, "EURUSD", london_afternoon))

    def test_smc_rejects_unconfirmed_structure(self):
        """No Judas sweep / no displacement => no signal"""
        london_morning = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)
        quiet = candles_from([1.1000 + 0.0002 * (i % 3) for i in range(22)],
                             london_morning - timedelta(hours=6), spread=0.0006)
        self.assertIsNone(self.smc.analyze(quiet, "EURUSD", london_morning))

    def test_smc_sell_side_structure(self):
        """Bearish sweep of prior highs mirrors the OTE logic"""
        london_morning = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)
        base = candles_from([1.1000 - 0.0004 * (i % 4) for i in range(20)],
                            datetime(2025, 1, 15, 2, 0, tzinfo=UTC), spread=0.0010)
        sweep = {"time": datetime(2025, 1, 15, 3, 0, tzinfo=UTC), "open": 1.0990, "high": 1.1050,
                 "low": 1.0988, "close": 1.0990, "volume": 90}
        displacement = {"time": datetime(2025, 1, 15, 4, 0, tzinfo=UTC), "open": 1.0990, "high": 1.0993,
                        "low": 1.0945, "close": 1.0950, "volume": 120}
        signal = self.smc.analyze(base + [sweep, displacement], "GBPUSD", london_morning)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, Direction.SELL)
        self.assertEqual(signal.metadata["structure"], "BEARISH_SWEEP")
        self.assertEqual(signal.risk_reward, 2.0)
        self.assertGreater(signal.stop_loss, signal.metadata["sweep_extreme"])

    def test_universe_authorization_map(self):
        """Symbol universe declares which engines may trade each asset class"""
        self.assertEqual(ACTIVE_SYMBOLS["VOL75"].authorized_strategies, ["Z_SCORE", "SYNTH_MOM"])
        self.assertEqual(ACTIVE_SYMBOLS["VOL75"].asset_class, "synthetic")
        self.assertEqual(ACTIVE_SYMBOLS["US30"].authorized_strategies, ["SMC_FIB"])
        self.assertEqual(ACTIVE_SYMBOLS["XAUUSD"].asset_class, "metal")
        self.assertIn("SMC_FIB", ACTIVE_SYMBOLS["EURUSD"].authorized_strategies)
        # Continuous-tick indices are momentum-only (no forex session structure).
        for symbol in ("STEP100", "BOOM300", "CRASH300"):
            self.assertEqual(ACTIVE_SYMBOLS[symbol].authorized_strategies, ["SYNTH_MOM"])
        self.assertEqual(set(SYNTHETIC_UNIVERSE),
                         {symbol for symbol, cfg in ACTIVE_SYMBOLS.items()
                          if cfg.asset_class == "synthetic"})

    # ---------------- Seasonal (DST) shift regression ----------------
    def test_killzone_seasonal_utc_shift(self):
        """The same local bounds land on different UTC hours in BST/EDT seasons"""
        london_open = PROP_CONFIG.LONDON_KILLZONE[0]   # 07:00 local
        ny_open = PROP_CONFIG.NY_KILLZONE[0]           # 13:30 local

        london_winter = datetime(2026, 1, 15, london_open[0], london_open[1],
                                 tzinfo=PROP_CONFIG.TIMEZONE_LONDON).astimezone(UTC)
        london_summer = datetime(2026, 7, 15, london_open[0], london_open[1],
                                 tzinfo=PROP_CONFIG.TIMEZONE_LONDON).astimezone(UTC)
        ny_winter = datetime(2026, 1, 15, ny_open[0], ny_open[1],
                             tzinfo=PROP_CONFIG.TIMEZONE_NY).astimezone(UTC)
        ny_summer = datetime(2026, 7, 15, ny_open[0], ny_open[1],
                             tzinfo=PROP_CONFIG.TIMEZONE_NY).astimezone(UTC)

        self.assertEqual(london_winter.hour, 7)   # UTC+0 season: 07:00 UTC
        self.assertEqual(london_summer.hour, 6)   # BST season: 06:00 UTC
        self.assertEqual(ny_winter.hour, 18)      # UTC-5 season: 18:30 UTC
        self.assertEqual(ny_summer.hour, 17)      # EDT season: 17:30 UTC

        self.assertEqual(self.smc._active_session(london_winter, "EURUSD"), "LDN")
        self.assertEqual(self.smc._active_session(london_summer, "EURUSD"), "LDN")
        self.assertEqual(self.smc._active_session(ny_winter, "XAUUSD"), "NY")
        self.assertEqual(self.smc._active_session(ny_summer, "XAUUSD"), "NY")

        # The mirrored UTC instant must flip with the season, not stay fixed.
        self.assertIsNone(self.smc._active_session(datetime(2026, 1, 15, 6, 0, tzinfo=UTC), "EURUSD"))
        self.assertEqual(self.smc._active_session(datetime(2026, 7, 15, 6, 0, tzinfo=UTC), "EURUSD"), "LDN")
        self.assertIsNone(self.smc._active_session(datetime(2026, 7, 15, 11, 0, tzinfo=UTC), "EURUSD"))
        self.assertIsNone(self.smc._active_session(datetime(2026, 7, 15, 16, 30, tzinfo=UTC), "XAUUSD"))
        self.assertIsNone(self.smc._active_session(datetime(2026, 1, 15, 17, 30, tzinfo=UTC), "XAUUSD"))

    def test_zoneinfo_offsets_across_dst_transitions(self):
        """zoneinfo carries the seasonal offsets, so no biannual code edit is needed"""
        eu_before = datetime(2026, 3, 28, 12, 0, tzinfo=UTC).astimezone(PROP_CONFIG.TIMEZONE_LONDON)
        eu_after = datetime(2026, 3, 29, 12, 0, tzinfo=UTC).astimezone(PROP_CONFIG.TIMEZONE_LONDON)
        us_before = datetime(2026, 3, 7, 12, 0, tzinfo=UTC).astimezone(PROP_CONFIG.TIMEZONE_NY)
        us_after = datetime(2026, 3, 8, 12, 0, tzinfo=UTC).astimezone(PROP_CONFIG.TIMEZONE_NY)

        self.assertEqual(eu_before.utcoffset(), timedelta(hours=0))
        self.assertEqual(eu_after.utcoffset(), timedelta(hours=1))
        self.assertEqual(us_before.utcoffset(), timedelta(hours=-5))
        self.assertEqual(us_after.utcoffset(), timedelta(hours=-4))

    def test_killzone_fixed_utc_mode(self):
        """UTC_FIXED mode pins London to 07:00-10:00 UTC and NY to 13:30-16:30 UTC all year"""
        fixed = replace(PROP_CONFIG, KILLZONE_MODE="UTC_FIXED")
        cases = [
            (datetime(2026, 1, 15, 7, 0, tzinfo=UTC), "LDN"),      # winter boundary
            (datetime(2026, 7, 15, 7, 0, tzinfo=UTC), "LDN"),      # BST season stays pinned
            (datetime(2026, 7, 15, 6, 0, tzinfo=UTC), None),       # DST mode would call this LDN
            (datetime(2026, 7, 15, 10, 1, tzinfo=UTC), None),
            (datetime(2026, 1, 15, 13, 30, tzinfo=UTC), "NY"),     # EST season
            (datetime(2026, 7, 15, 13, 30, tzinfo=UTC), "NY"),     # EDT season
            (datetime(2026, 7, 15, 16, 30, tzinfo=UTC), "NY"),     # inclusive close
            (datetime(2026, 7, 15, 16, 31, tzinfo=UTC), None),
        ]
        with patch.object(sessions_module, "PROP_CONFIG", fixed):
            for instant, expected in cases:
                self.assertEqual(active_session(instant, "EURUSD"), expected, msg=f"{instant:%H:%M} UTC")

        # Default mode shifts with the season instead of staying pinned.
        self.assertEqual(active_session(datetime(2026, 7, 15, 6, 0, tzinfo=UTC), "EURUSD"), "LDN")

    def test_sessions_module_matches_engine_gate(self):
        """Shared clock is the single source of truth for every engine"""
        instants = [datetime(2026, 1, 15, 8, 0, tzinfo=UTC), datetime(2026, 7, 15, 6, 0, tzinfo=UTC),
                    datetime(2026, 1, 15, 18, 30, tzinfo=UTC), datetime(2026, 1, 15, 12, 0, tzinfo=UTC)]
        for instant in instants:
            self.assertEqual(active_session(instant, "EURUSD"), self.smc._active_session(instant, "EURUSD"))

    # ---------------- Z-Score Anchor ----------------
    def test_z_score_edge_trigger_and_cooldown(self):
        """A single stretch must not re-alert on every following bar"""
        anchor = ZScoreAnchor()
        anchor.COOLDOWN_BARS = 2  # shrink the 8h M5 cooldown for a fast deterministic test
        start = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)  # winter -> bars land inside LDN
        closes = [1.0 + 0.001 * (i % 5) for i in range(99)] + [1.150] * 6 + [1.003] * 11 + [1.200]
        candles = candles_from(closes, start, spread=0.001)
        london_morning = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)

        first = anchor.analyze(candles[:100], "EURUSD", london_morning)
        self.assertIsNotNone(first)

        # Stretch persists: edge-trigger must suppress the follow-up bars.
        for index in range(101, 105):
            self.assertIsNone(anchor.analyze(candles[:index + 1], "EURUSD", london_morning),
                              msg=f"re-alerted at bar {index} without mean reversion")

        # Mean reversion (|z| back inside the entry band) re-arms the trigger.
        for index in range(105, 116):
            self.assertIsNone(anchor.analyze(candles[:index + 1], "EURUSD", london_morning))

        second = anchor.analyze(candles[:len(candles)], "EURUSD", london_morning)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.direction, Direction.SELL)
        self.assertEqual(second.direction, Direction.SELL)
        self.assertGreater(second.metadata["z_score"], ZScoreAnchor.ENTRY_Z)
        self.assertEqual(ZScoreAnchor.COOLDOWN_BARS, 96)

        anchor.reset("EURUSD")
        self.assertIsNotNone(anchor.analyze(candles[:100], "EURUSD", london_morning))

    def test_z_score_forex_alerts_are_killzone_scoped(self):
        """Forex bonus alerts need London/NY; Deriv synthetics stay 24/7"""
        anchor = ZScoreAnchor()
        closes = [1.0 + 0.001 * (i % 5) for i in range(99)] + [1.150]
        candles = candles_from(closes, datetime(2026, 1, 15, 4, 0, tzinfo=UTC), spread=0.001)

        outside = datetime(2026, 1, 15, 2, 30, tzinfo=UTC)  # 02:30 London -> outside both zones
        self.assertIsNone(anchor.analyze(candles, "EURUSD", outside))
        anchor.reset()   # fresh edge-trigger state per independent gate probe
        self.assertIsNotNone(anchor.analyze(candles, "EURUSD", datetime(2026, 1, 15, 8, 0, tzinfo=UTC)))
        anchor.reset()
        self.assertIsNotNone(anchor.analyze(candles, "EURUSD", datetime(2026, 1, 15, 18, 30, tzinfo=UTC)))

        synthetic = [100.0 - 0.15 * (i % 5) for i in range(99)] + [84.0]
        synth_candles = candles_from(synthetic, datetime(2026, 1, 15, 0, 0, tzinfo=UTC),
                                     step=timedelta(minutes=1), spread=0.4)
        midnight = anchor.analyze(synth_candles, "VOL75", datetime(2026, 1, 15, 0, 30, tzinfo=UTC))
        self.assertIsNotNone(midnight)
        assert midnight is not None
        self.assertEqual(midnight.metadata["session"], "GLOBAL")
        self.assertEqual(midnight.metadata["engine"], "DERIV_SYNTHETICS")

    def test_daily_setup_budget_caps(self):
        """1-2 setups/day per asset and 3/day portfolio-wide, reset at UTC midnight"""
        day = "2026-01-15"
        signal = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                        entry_price=1.10000, stop_loss=1.09900, take_profit=1.10200, risk_reward=2.0)
        self.assertTrue(self.risk_mgr.validate_signal(signal, [], day_key=day)["approved"])
        self.assertTrue(self.risk_mgr.validate_signal(signal, [], day_key=day)["approved"])

        third = self.risk_mgr.validate_signal(signal, [], day_key=day)
        self.assertFalse(third["approved"])
        self.assertIn("Daily setup cap reached", third["reason"])

        gold = Signal(symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                      entry_price=2650.00, stop_loss=2640.00, take_profit=2670.00, risk_reward=2.0)
        self.assertTrue(self.risk_mgr.validate_signal(gold, [], day_key=day)["approved"])
        fourth = self.risk_mgr.validate_signal(gold, [], day_key=day)
        self.assertFalse(fourth["approved"])
        self.assertIn("Daily setup budget exhausted", fourth["reason"])

        self.assertEqual(self.risk_mgr.setups_today(day), PROP_CONFIG.MAX_DAILY_SETUPS_TOTAL)
        self.assertEqual(self.risk_mgr.setups_today(day, "EURUSD"), PROP_CONFIG.MAX_DAILY_SETUPS_PER_SYMBOL)
        self.assertTrue(self.risk_mgr.validate_signal(signal, [], day_key="2026-01-16")["approved"])

    # ---------------- Telegram Dispatcher ----------------
    def test_z_score_bearish_stretch(self):
        closes = [1.0 + 0.001 * (i % 5) for i in range(99)] + [1.150]
        candles = candles_from(closes, datetime(2025, 1, 15, 4, 0, tzinfo=UTC), spread=0.001)
        signal = self.zscore.analyze(candles, "EURUSD", datetime(2025, 1, 15, 8, 0, tzinfo=UTC))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, Direction.SELL)
        self.assertEqual(signal.strategy_tag, "Z_SCORE")
        self.assertGreaterEqual(signal.metadata["z_score"], 2.0)
        self.assertGreater(signal.entry_price, signal.take_profit)
        self.assertGreater(signal.stop_loss, signal.entry_price)
        self.assertTrue(signal.geometry_valid())

    def test_z_score_requires_full_lookback(self):
        candles = candles_from([1.1 + 0.001 * (i % 4) for i in range(50)],
                               datetime(2025, 1, 15, 4, 0, tzinfo=UTC))
        self.assertIsNone(self.zscore.analyze(candles, "EURUSD", datetime(2025, 1, 15, 8, 0, tzinfo=UTC)))

    def test_z_score_neutral_window_is_flat(self):
        candles = candles_from([1.1 + 0.001 * (i % 4) for i in range(100)],
                               datetime(2025, 1, 15, 4, 0, tzinfo=UTC), spread=0.001)
        self.assertIsNone(self.zscore.analyze(candles, "EURUSD", datetime(2025, 1, 15, 8, 0, tzinfo=UTC)))

    def test_z_score_synthetic_engine_label(self):
        closes = [100.0 - 0.15 * (i % 5) for i in range(99)] + [84.0]
        candles = candles_from(closes, datetime(2025, 1, 15, 0, 0, tzinfo=UTC), step=timedelta(minutes=1),
                               spread=0.4)
        signal = self.zscore.analyze(candles, "VOL75", datetime(2025, 1, 15, 0, 30, tzinfo=UTC))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.metadata["engine"], "DERIV_SYNTHETICS")
        self.assertEqual(signal.metadata["session"], "GLOBAL")
        self.assertEqual(signal.direction, Direction.BUY)
        self.assertLess(signal.metadata["z_score"], -2.0)
        self.assertGreater(signal.risk_reward, 0.0)
        self.assertGreater(signal.take_profit, signal.entry_price)
        self.assertGreater(signal.stop_loss, 0.0)

    # ---------------- Telegram Dispatcher ----------------
    def test_telegram_card_formatting(self):
        """Ensure markdown card contains all required fields"""
        signal = Signal(
            symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
            entry_price=2650.00, stop_loss=2640.00, take_profit=2670.00, risk_reward=2.0,
            metadata={"session": "LDN"}
        )
        card = TelegramDispatcher.format_signal_card(signal, {"lot_size": 0.15, "risk_usd": 15.0})
        self.assertIn("🟢", card)
        self.assertIn("`SMC_FIB`", card)
        self.assertIn("`0.15`", card)
        self.assertIn("PHANTOM SIGNAL | XAUUSD", card)
        self.assertIn("`BUY`", card)
        self.assertIn("`2640.0`", card)
        self.assertIn("`2670.0`", card)
        self.assertIn("`2.0`", card)
        self.assertIn("$15.0", card)

    def test_sell_direction_icon_and_lifecycle_card(self):
        signal = Signal(symbol="GBPUSD", direction=Direction.SELL, strategy_tag="Z_SCORE",
                        entry_price=1.2500, stop_loss=1.2520, take_profit=1.2460, risk_reward=2.0)
        card = TelegramDispatcher.format_signal_card(signal, {"lot_size": 0.12, "risk_usd": 15.0})
        self.assertIn("🔴", card)
        self.assertIn("`SELL`", card)

        lifecycle = TelegramDispatcher.format_lifecycle_card("TP", signal, 1.2500, 30.0, "OTE target filled")
        self.assertIn("💰", lifecycle)
        self.assertIn("`TP`", lifecycle)
        self.assertIn("$30.00", lifecycle)
        self.assertIn("`1.25`", lifecycle)

        closed = TelegramDispatcher.format_lifecycle_card("SL", signal, note="Judas sweep invalidated")
        self.assertIn("🛑", closed)
        self.assertIn("Judas sweep invalidated", closed)

    def test_dispatcher_fails_safe_without_credentials(self):
        """Unconfigured credentials must no-op, never hit the network"""
        signal = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                        entry_price=1.10, stop_loss=1.099, take_profit=1.102, risk_reward=2.0)
        with patch.object(dispatcher_module, "TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN"), \
             patch.object(dispatcher_module, "TELEGRAM_CHAT_ID", "YOUR_PHANTOM_ALERTS_CHANNEL_ID"), \
             patch("requests.post", side_effect=AssertionError("network call attempted")):
            self.assertFalse(TelegramDispatcher.send_alert(signal, {"lot_size": 0.15, "risk_usd": 15.0}))
            self.assertFalse(TelegramDispatcher.send_lifecycle("ENTRY", signal))

    def test_dispatcher_strategy_icons(self):
        """Cards must visually separate the two engines: ⚡ SMC_FIB vs 🧠 Z_SCORE"""
        smc = Signal(symbol="EURUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                     entry_price=1.10122, stop_loss=1.09734, take_profit=1.10897, risk_reward=2.0,
                     metadata={"session": "LDN"})
        anchor = Signal(symbol="XAUUSD", direction=Direction.SELL, strategy_tag="Z_SCORE",
                        entry_price=2650.0, stop_loss=2680.0, take_profit=2620.0, risk_reward=2.0,
                        metadata={"session": "NY"})
        risk = {"lot_size": 0.04, "risk_usd": 15.0}

        smc_card = TelegramDispatcher.format_signal_card(smc, risk)
        anchor_card = TelegramDispatcher.format_signal_card(anchor, risk)

        self.assertIn("*⚡ Strategy:* `SMC_FIB`", smc_card)
        self.assertIn("*🧠 Strategy:* `Z_SCORE`", anchor_card)
        self.assertIn("🟢", smc_card)          # BUY
        self.assertIn("🔴", anchor_card)       # SELL
        for card, price in ((smc_card, "`1.10122`"), (anchor_card, "`2650.0`")):
            self.assertIn(price, card)
            self.assertIn("`2.0`", card)       # 1:2+ R:R shown
        self.assertIn("`1.09734`", smc_card)
        self.assertIn("`1.10897`", smc_card)
        self.assertIn("`0.04`", smc_card)      # sized lot
        self.assertIn("$15.0", smc_card)

    def test_dispatcher_marks_deriv_synthetic_cards(self):
        synth = Signal(symbol="VOL75", direction=Direction.BUY, strategy_tag="SYNTH_MOM",
                       entry_price=106.0, stop_loss=103.9, take_profit=110.2, risk_reward=2.0,
                       metadata={"engine": "DERIV_SYNTHETICS", "session": "GLOBAL"})
        card = TelegramDispatcher.format_signal_card(synth, {"lot_size": 1.0, "risk_usd": 15.0})
        self.assertIn(TelegramDispatcher.SYNTHETIC_TAG, card)
        self.assertIn("*📈 Strategy:* `SYNTH_MOM`", card)

        lifecycle = TelegramDispatcher.format_lifecycle_card("TP", synth, 110.2, 15.0)
        self.assertIn(TelegramDispatcher.SYNTHETIC_TAG, lifecycle)
        self.assertIn("💰", lifecycle)



class TestPhantomSyntheticsEngine(unittest.TestCase):
    """24/7 Deriv scanner, momentum breakout, and engine dispatch wiring."""

    def setUp(self):
        self.mom = SyntheticMomentum()
        self.risk = RiskManager(equity=5000.00)
        self.night = datetime(2025, 1, 15, 22, 30, tzinfo=UTC)   # outside every killzone

    class StubFeed:
        """Feed adapter implementing the candles(symbol, timeframe) contract."""
        def __init__(self, candles: Dict[str, List[Dict]]):
            self.candles_map = candles

        def candles(self, symbol: str, timeframe: Optional[str] = None,
                    count: Optional[int] = None) -> List[Dict]:
            return self.candles_map.get(symbol, [])

    def _ramp(self, direction: str = "up") -> List[Dict]:
        closes = ([100.0] * 40
                  + [100.0 + 0.3 * i * (1 if direction == "up" else -1) for i in range(1, 21)]
                  + [107.2 if direction == "up" else 92.8])
        return candles_from(closes, self.night - timedelta(minutes=60),
                            step=timedelta(minutes=1), spread=0.5)

    def test_momentum_breakout_long(self):
        signal = self.mom.analyze(self._ramp("up"), "VOL75", self.night)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, Direction.BUY)
        self.assertEqual(signal.strategy_tag, "SYNTH_MOM")
        self.assertEqual(signal.risk_reward, 2.0)                 # 1:2 floor enforced
        self.assertEqual(signal.metadata["engine"], "DERIV_SYNTHETICS")
        self.assertEqual(signal.metadata["session"], "GLOBAL")    # no session filter
        self.assertGreater(signal.entry_price, signal.metadata["range_extreme"])
        self.assertGreater(signal.take_profit, signal.entry_price)
        self.assertLess(signal.stop_loss, signal.entry_price)
        self.assertTrue(signal.geometry_valid())

    def test_momentum_breakout_short(self):
        signal = self.mom.analyze(self._ramp("down"), "VOL100", self.night)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.direction, Direction.SELL)
        self.assertEqual(signal.metadata["structure"], "BREAKOUT_DOWN")
        self.assertLess(signal.take_profit, signal.entry_price)

    def test_momentum_cooldown_blocks_repeats(self):
        candles = self._ramp("up")
        self.assertIsNotNone(self.mom.analyze(candles, "VOL75", self.night))
        self.assertIsNone(self.mom.analyze(candles, "VOL75", self.night))   # cooldown latch
        self.mom.reset("VOL75")
        self.assertIsNotNone(self.mom.analyze(candles, "VOL75", self.night))

    def test_momentum_ignores_forex_symbols(self):
        self.assertIsNone(self.mom.analyze(self._ramp("up"), "EURUSD", self.night))
        self.assertIsNone(self.mom.analyze(self._ramp("up"), "UNKNOWN123", self.night))

    def test_synthetic_currency_cap_shared(self):
        """All synthetics share one synthetic bucket, so a 3rd one is rejected"""
        open_pos = [{"symbol": "BOOM300"}, {"symbol": "CRASH300"}]
        signal = Signal(symbol="STEP100", direction=Direction.BUY, strategy_tag="SYNTH_MOM",
                        entry_price=100.0, stop_loss=98.0, take_profit=104.0, risk_reward=2.0)
        self.assertFalse(self.risk.validate_correlation_cap(signal, open_pos))

    def test_scanner_runs_247_and_dispatches_tagged_card(self):
        posts: List[str] = []
        scanner = SyntheticScanner(self.StubFeed({"VOL75": self._ramp("up")}), timeframe="M1")
        with patch.object(TelegramDispatcher, "_post",
                          side_effect=lambda text: posts.append(text) or True):
            summary = scanner.scan_once(now=self.night, symbols=["VOL75"])

        self.assertEqual(summary["alerts"], 1)          # fired outside every killzone
        self.assertEqual(summary["open_positions"], 1)
        self.assertEqual(len(posts), 1)
        card = posts[0]
        self.assertIn(TelegramDispatcher.SYNTHETIC_TAG, card)
        self.assertIn("*📈 Strategy:* `SYNTH_MOM`", card)
        self.assertIn("🟢", card)
        self.assertIn("`107.2`", card)
        position = scanner.positions["VOL75"]
        self.assertLessEqual(position.lots, ACTIVE_SYMBOLS["VOL75"].max_lot)
        self.assertLessEqual(position.risk_usd, PROP_CONFIG.BASE_RISK_AMOUNT)

    def test_engine_dispatches_only_after_risk_gates(self):
        # Asia pool (00:00-07:00 UTC winter) swept at 08:40, MSS displacement at 08:45.
        start = datetime(2025, 1, 15, 4, 0, tzinfo=UTC)
        base = candles_from([1.1000 + 0.0004 * (i % 4) for i in range(20)], start,
                            step=timedelta(minutes=5), spread=0.0010)
        sweep = {"time": datetime(2025, 1, 15, 8, 40, tzinfo=UTC), "open": 1.1010,
                 "high": 1.1012, "low": 1.0950, "close": 1.1000, "volume": 90}
        displacement = {"time": datetime(2025, 1, 15, 8, 45, tzinfo=UTC), "open": 1.1000,
                        "high": 1.1045, "low": 1.0997, "close": 1.1040, "volume": 120}
        candles = base + [sweep, displacement]
        feed = self.StubFeed({"EURUSD": candles})

        posts: List[str] = []
        engine = LiveEngine(feed, timeframe="M5")
        with patch.object(TelegramDispatcher, "_post",
                          side_effect=lambda text: posts.append(text) or True):
            summary = engine.tick(now=displacement["time"], symbols=["EURUSD"])

        self.assertEqual(summary["alerts"], 1)
        self.assertEqual(len(posts), 1)
        self.assertIn("*⚡ Strategy:* `SMC_FIB`", posts[0])
        self.assertIn("`2.0`", posts[0])                       # R:R shown on the card
        self.assertLessEqual(engine.positions["EURUSD"].lots, ACTIVE_SYMBOLS["EURUSD"].max_lot)

        # The same structure outside the killzone must never reach the channel.
        posts.clear()
        engine_out = LiveEngine(feed, timeframe="M5")
        with patch.object(TelegramDispatcher, "_post",
                          side_effect=lambda text: posts.append(text) or True):
            outside = engine_out.tick(now=datetime(2025, 1, 15, 12, 0, tzinfo=UTC),
                                      symbols=["EURUSD"])
        self.assertEqual(outside["alerts"], 0)
        self.assertEqual(posts, [])


class TestPhantomDataPipeline(unittest.TestCase):
    """Historical CSV ingestion and offline portfolio replay."""

    def _candles(self) -> List[Dict]:
        """Asia pool (00:00-07:00 London) swept at 07:45, MSS bar at 08:00 inside the LDN killzone."""
        start = datetime(2026, 1, 15, 2, 0, tzinfo=UTC)  # winter: London == UTC
        base = candles_from([1.1000 + 0.0004 * (i % 4) for i in range(20)], start, spread=0.0010)
        sweep = {"time": datetime(2026, 1, 15, 7, 45, tzinfo=UTC), "open": 1.1010, "high": 1.1012,
                 "low": 1.0950, "close": 1.1000, "volume": 90}
        displacement = {"time": datetime(2026, 1, 15, 8, 0, tzinfo=UTC), "open": 1.1000, "high": 1.1045,
                        "low": 1.0997, "close": 1.1040, "volume": 120}
        return base + [sweep, displacement]

    def _write_csv(self, root: Path, symbol: str, candles: List[Dict]) -> Path:
        path = root / f"{symbol}_M5.csv"
        lines = ["time,open,high,low,close,volume"]
        for candle in candles:
            lines.append(f"{int(candle['time'].timestamp())},{candle['open']},{candle['high']},"
                         f"{candle['low']},{candle['close']},{candle['volume']}")
        path.write_text("\n".join(lines))
        return path

    def test_csv_ingestion(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = self._candles()
            self._write_csv(root, "EURUSD", candles)
            (root / "GBPUSD_M5.csv").write_text("time,open,high,low,close\nnot,a,number,row\n")

            self.assertEqual(available_symbols(root), ["EURUSD", "GBPUSD"])  # lists files on disk
            self.assertEqual(load_candles("GBPUSD", "M5", root), [])         # malformed rows skipped
            self.assertEqual(csv_path("EURUSD", "M5", root).name, "EURUSD_M5.csv")

            loaded = load_candles("EURUSD", "M5", root)
            self.assertEqual(len(loaded), len(candles))
            self.assertEqual(loaded[0]["time"], candles[0]["time"])
            self.assertEqual(loaded[0]["time"].tzinfo, UTC)          # DST clock needs tz-aware bars
            self.assertEqual(load_candles("NOPE", "M5", root), [])

            trimmed = load_candles("EURUSD", "M5", root, start=candles[10]["time"])
            self.assertEqual(len(trimmed), len(candles) - 10)

    def test_replay_runs_the_live_gates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_csv(root, "EURUSD", self._candles())
            records = BacktestRunner(root, "M5", source="tmp").run(["EURUSD"])

            summary = next(record for record in records if record.get("summary"))
            self.assertEqual(summary["source"], "tmp")
            self.assertGreater(summary["trades"], 0)
            self.assertLessEqual(summary["max_daily_dd_pct"], PROP_CONFIG.MAX_DAILY_DD_PERCENT * 100)
            self.assertEqual(summary["dd_breach_days"], [])

            record = next(r for r in records if not r.get("summary"))
            self.assertEqual(record["strategy"], "SMC_FIB")
            self.assertEqual(record["signals"], 1)      # one sweep, one alert
            self.assertEqual(record["trades"], 1)
            self.assertLessEqual(record["avg_lot"], ACTIVE_SYMBOLS["EURUSD"].max_lot)
            self.assertGreater(record["avg_lot"], 0.0)
            # R multiple must equal PnL / the $15 risk budget, i.e. sizing really is $15/trade.
            self.assertAlmostEqual(record["net_r"], record["net_usd"] / PROP_CONFIG.BASE_RISK_AMOUNT, places=2)


class TestPhantomMt5Feed(unittest.TestCase):
    """MT5 bar payload conversion with no terminal attached."""

    @staticmethod
    def _rates(count: int = 60, base: float = 100.0, start: int = 1_735_000_000) -> List[Dict]:
        return [{"time": start + i, "open": base, "high": base + 0.5, "low": base - 0.5,
                 "close": base + 0.1 * i, "tick_volume": 12} for i in range(count)]

    def test_maps_broker_symbol_and_utc_bars(self):
        seen: List[tuple] = []

        def fetch(broker, code, offset, count):
            seen.append((broker, code, offset, count))
            return self._rates(count=count)

        feed = MT5BarFeed(timeframe="M1", count=30, initialize=False, rates_fetch=fetch)
        bars = feed.candles("VOL75", "M1", 30)

        self.assertEqual(seen[0][0], "Volatility 75 Index")   # internal tag mapped
        self.assertEqual(seen[0][2], 0)                       # copy_rates_from_pos offset
        self.assertEqual(seen[0][3], 30)                      # count forwarded
        self.assertEqual(len(bars), 30)
        self.assertEqual(bars[0]["time"].tzinfo, UTC)         # tz-aware UTC, required by sessions
        self.assertEqual(bars[0]["volume"], 12)              # tick_volume -> volume
        self.assertEqual(broker_symbol("VOL75"), "Volatility 75 Index")
        self.assertEqual(internal_symbol("Volatility 75 Index"), "VOL75")

    def test_degrades_on_disconnect_and_dry_run(self):
        def empty(*_args):
            return None
        disconnected = MT5BarFeed(initialize=False, rates_fetch=empty)
        self.assertEqual(disconnected.candles("EURUSD", "M1"), [])

        def boom(*_args):
            raise RuntimeError("terminal disconnected")
        broken = MT5BarFeed(initialize=False, rates_fetch=boom)
        self.assertEqual(broken.candles("EURUSD", "M1"), [])   # engine loop must not stall

        self.assertEqual(MT5BarFeed(dry_run=True).candles("EURUSD"), [])
        self.assertEqual(build_feed("mt5", "M5", 200, dry_run=True).candles("EURUSD"), [])
        self.assertIsInstance(build_feed("csv"), CsvBarFeed)   # CSV dry-run mode intact
        with self.assertRaises(ValueError):
            build_feed("zmq")

    def test_feed_feeds_the_session_engine(self):
        """MT5-shaped payloads flow through the DST clock and produce a sized signal."""
        base = datetime(2025, 1, 15, 4, 0, tzinfo=UTC)
        bars = candles_from([1.1000 + 0.0004 * (i % 4) for i in range(20)], base,
                            step=timedelta(minutes=5), spread=0.0010)
        bars += [{"time": datetime(2025, 1, 15, 8, 40, tzinfo=UTC), "open": 1.1010,
                  "high": 1.1012, "low": 1.0950, "close": 1.1000, "volume": 90},
                 {"time": datetime(2025, 1, 15, 8, 45, tzinfo=UTC), "open": 1.1000,
                  "high": 1.1045, "low": 1.0997, "close": 1.1040, "volume": 120}]
        rates = [{"time": int(bar["time"].timestamp()), "open": bar["open"], "high": bar["high"],
                  "low": bar["low"], "close": bar["close"], "tick_volume": 40} for bar in bars]

        def fetch(_broker, _code, _offset, count):
            return rates[-count:]

        feed = MT5BarFeed(timeframe="M5", count=40, initialize=False, rates_fetch=fetch)
        signal = SMCICTKillzone().analyze(feed.candles("EURUSD", "M5", 40), "EURUSD",
                                         datetime(2025, 1, 15, 8, 45, tzinfo=UTC))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.metadata["session"], "LDN")


class TestPhantomMt5Bridge(unittest.TestCase):
    """Atomic order serialization and inbound fill handling with no live terminal."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.bridge = MT5FileBridge(Path(self.tmp.name), magic=MT5_MAGIC)
        self.posts: List[str] = []
        self.patcher = patch.object(TelegramDispatcher, "_post",
                                    side_effect=lambda text: self.posts.append(text) or True)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _signal(symbol: str = "EURUSD", tag: str = "SMC_FIB") -> Signal:
        return Signal(symbol=symbol, direction=Direction.BUY, strategy_tag=tag,
                      entry_price=107.2 if symbol == "VOL75" else 1.10122,
                      stop_loss=105.9 if symbol == "VOL75" else 1.09734,
                      take_profit=110.2 if symbol == "VOL75" else 1.10897,
                      risk_reward=2.0, metadata={"session": "LDN"})

    def _structure_feed(self):
        base = datetime(2025, 1, 15, 4, 0, tzinfo=UTC)
        bars = candles_from([1.1000 + 0.0004 * (i % 4) for i in range(20)], base,
                            step=timedelta(minutes=5), spread=0.0010)
        bars += [{"time": datetime(2025, 1, 15, 8, 40, tzinfo=UTC), "open": 1.1010,
                  "high": 1.1012, "low": 1.0950, "close": 1.1000, "volume": 90},
                 {"time": datetime(2025, 1, 15, 8, 45, tzinfo=UTC), "open": 1.1000,
                  "high": 1.1045, "low": 1.0997, "close": 1.1040, "volume": 120}]

        class Feed:
            def __init__(self, bars):
                self.bars = bars

            def candles(self, symbol, timeframe=None, count=None):
                return self.bars

        return Feed(bars)

    def test_order_file_is_atomic_and_mapped(self):
        signal = self._signal("VOL75", "SYNTH_MOM")
        path = self.bridge.write_order(signal, {"lot_size": 1.0, "risk_usd": 15.0})

        self.assertEqual(path.name, f"{signal.signal_id}.json")
        self.assertEqual(list(self.bridge.orders_dir.glob("*.tmp")), [])   # temp renamed away
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["symbol"], "Volatility 75 Index")         # broker mapping
        self.assertEqual(payload["internal_symbol"], "VOL75")
        self.assertEqual(payload["action"], "BUY")
        self.assertEqual(payload["strategy"], "SYNTH_MOM")
        self.assertEqual(payload["magic"], MT5_MAGIC)
        self.assertEqual(payload["volume"], 1.0)
        self.assertEqual(payload["sl"], 130)     # 1.30 / 0.01 point
        self.assertEqual(payload["tp"], 300)     # 3.00 / 0.01 point
        self.assertEqual(payload["signal_id"], signal.signal_id)

    def test_points_use_per_symbol_point_size(self):
        gold = Signal(symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                      entry_price=2650.0, stop_loss=2646.4, take_profit=2657.2, risk_reward=2.0)
        index = Signal(symbol="US30", direction=Direction.SELL, strategy_tag="SMC_FIB",
                       entry_price=43000.0, stop_loss=43012.0, take_profit=42976.0, risk_reward=2.0)
        fx = self._signal("EURUSD")
        self.assertEqual(self.bridge.points(fx.entry_price - fx.stop_loss, "EURUSD"), 388)
        self.assertEqual(self.bridge.points(gold.entry_price - gold.stop_loss, "XAUUSD"), 360)
        self.assertEqual(self.bridge.points(index.stop_loss - index.entry_price, "US30"), 12)

    def test_fill_receipts_are_drained_once_and_quarantined(self):
        signal = self._signal()
        self.bridge.write_order(signal, {"lot_size": 0.02, "risk_usd": 15.0})
        self.bridge.write_fill(Fill(signal.signal_id, "TP", "EURUSD", 1.10897, 0.02, 15.0, "closed"))
        (self.bridge.fills_dir / "junk.json").write_text("{not json", encoding="utf-8")

        with self.patcher:
            fills = self.bridge.poll_fills()
        self.assertEqual([fill.event for fill in fills], ["TP"])
        self.assertEqual(self.bridge.poll_fills(), [])               # consumed exactly once
        self.assertTrue((self.bridge.fills_dir / "junk.bad").exists())
        self.assertEqual(list(self.bridge.fills_dir.glob("*.json")), [])

    def test_unknown_events_and_missing_positions_are_skipped(self):
        signal = self._signal()
        self.bridge.write_fill(Fill(signal.signal_id, "NOPE", "EURUSD", 1.10, 0.02, 0.0, "junk"))
        self.assertEqual(self.bridge.poll_fills(), [])              # unknown event dropped
        execution = ExecutionBridge(self.bridge, TelegramDispatcher())
        with self.patcher:
            self.assertEqual(execution.ingest({}), [])             # no matching position
        self.assertEqual(self.posts, [])                            # no card for a stranger

    def test_build_execution_follows_the_cli_flag(self):
        self.assertIsNone(build_execution(False, TelegramDispatcher(), Path(self.tmp.name)))
        self.assertIsInstance(build_execution(True, TelegramDispatcher(), Path(self.tmp.name)),
                              ExecutionBridge)

    @staticmethod
    def _mq5_scalar(payload: str, key: str) -> str:
        """Mirror PhantomExecutioner's JsonScalar(): quote-delimited key, cut at , or }."""
        token = f'"{key}":'
        start = payload.find(token)
        if start < 0:
            return ""
        rest = payload[start + len(token):]
        cuts = [index for index in (rest.find(","), rest.find("}")) if index >= 0]
        value = rest[:min(cuts)] if cuts else rest
        return value.replace('"', "").strip()

    def test_order_payload_is_parsable_by_the_ea(self):
        gold = Signal(symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                      entry_price=2650.0, stop_loss=2646.4, take_profit=2657.2, risk_reward=2.0)
        path = self.bridge.write_order(gold, {"lot_size": 0.02, "risk_usd": 15.0})
        payload = path.read_text(encoding="utf-8")     # indented JSON, like the EA reads it

        self.assertEqual(self._mq5_scalar(payload, "signal_id"), gold.signal_id)
        self.assertEqual(self._mq5_scalar(payload, "symbol"), "XAUUSD")
        self.assertEqual(self._mq5_scalar(payload, "action"), "BUY")
        self.assertEqual(self._mq5_scalar(payload, "strategy"), "SMC_FIB")
        self.assertEqual(self._mq5_scalar(payload, "magic"), str(MT5_MAGIC))
        self.assertEqual(float(self._mq5_scalar(payload, "volume")), 0.02)
        # XAUUSD point size is 0.01, so $3.60 of stop risk is 360 points.
        self.assertEqual(int(self._mq5_scalar(payload, "sl")), 360)
        self.assertEqual(int(self._mq5_scalar(payload, "tp")), 720)
        self.assertEqual(self._mq5_scalar(payload, "internal_symbol"), "XAUUSD")


    def test_engine_writes_order_then_applies_receipts(self):
        engine = LiveEngine(self._structure_feed(), timeframe="M5",
                            execution=ExecutionBridge(self.bridge, TelegramDispatcher()))
        tick_at = datetime(2025, 1, 15, 8, 45, tzinfo=UTC)

        with self.patcher:
            first = engine.tick(now=tick_at, symbols=["EURUSD"])
        self.assertEqual(first["alerts"], 1)
        orders = list(self.bridge.orders_dir.glob("*.json"))
        self.assertEqual(len(orders), 1)
        payload = json.loads(orders[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["symbol"], "EURUSD")
        self.assertLessEqual(payload["volume"], ACTIVE_SYMBOLS["EURUSD"].max_lot)
        # Terminal owns SL/TP in bridge mode, so bars must not simulate exits.
        self.assertEqual(engine.positions["EURUSD"].take_profit, 1.10897)

        self.bridge.write_fill(Fill(payload["signal_id"], "FILLED", "EURUSD",
                                    payload["entry"], payload["volume"], None, "filled"))
        self.bridge.write_fill(Fill(payload["signal_id"], "TP", "EURUSD",
                                    payload["tp"], payload["volume"], 15.0, "closed on terminal"))

        with self.patcher:
            engine.tick(now=tick_at, symbols=[])            # drain receipts only

        self.assertEqual(engine.execution.by_signal, {})          # receipt consumed
        self.assertAlmostEqual(engine.equity, 5015.0, places=2)    # terminal PnL, not a guess
        self.assertIn("PHANTOM LIFECYCLE | `FILLED`", self.posts[1])
        self.assertIn("PHANTOM LIFECYCLE | `TP`", self.posts[-1])
        self.assertIn("💰", self.posts[-1])
        self.assertEqual(list(self.bridge.fills_dir.glob("*.json")), [])

    def test_rejected_receipt_drops_the_position(self):
        engine = LiveEngine(self._structure_feed(), timeframe="M5",
                            execution=ExecutionBridge(self.bridge, TelegramDispatcher()))
        tick_at = datetime(2025, 1, 15, 8, 45, tzinfo=UTC)
        with self.patcher:
            engine.tick(now=tick_at, symbols=["EURUSD"])
        signal_id = engine.positions["EURUSD"].signal.signal_id
        self.bridge.write_fill(Fill(signal_id, "REJECTED", "EURUSD", None, None, None,
                                    "guard refused (tradeable/open/DD/daily budget)"))

        with self.patcher:
            summary = engine.tick(now=tick_at, symbols=[])         # drain receipts only

        self.assertEqual(engine.execution.by_signal, {})           # position dropped
        self.assertEqual(summary["rejections"].get(
            "EURUSD: guard refused (tradeable/open/DD/daily budget)", 0), 1)
        self.assertIn("⛔", self.posts[-1])
        self.assertAlmostEqual(engine.equity, 5000.0, places=2)      # nothing filled

    def test_scanner_writes_tagged_synthetic_order(self):
        closes = [100.0] * 40 + [100.0 + 0.3 * i for i in range(1, 21)] + [107.2]
        bars = candles_from(closes, datetime(2025, 1, 15, 22, 30, tzinfo=UTC) - timedelta(minutes=60),
                            step=timedelta(minutes=1), spread=0.5)

        class Feed:
            def __init__(self, bars):
                self.bars = bars

            def candles(self, symbol, timeframe=None, count=None):
                return self.bars

        scanner = SyntheticScanner(Feed(bars), timeframe="M1",
                                   execution=ExecutionBridge(self.bridge, TelegramDispatcher()))
        with patch.object(TelegramDispatcher, "_post",
                          side_effect=lambda text: self.posts.append(text) or True):
            summary = scanner.scan_once(now=datetime(2025, 1, 15, 22, 30, tzinfo=UTC),
                                        symbols=["VOL75"])

        self.assertEqual(summary["alerts"], 1)
        orders = list(self.bridge.orders_dir.glob("*.json"))
        self.assertEqual(len(orders), 1)
        payload = json.loads(orders[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["symbol"], "Volatility 75 Index")   # broker mapping
        self.assertEqual(payload["action"], "BUY")
        self.assertEqual(payload["strategy"], "SYNTH_MOM")
        self.assertIn(TelegramDispatcher.SYNTHETIC_TAG, self.posts[0])

if __name__ == "__main__":
    unittest.main(verbosity=2)



