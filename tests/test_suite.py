"""
PHANTOM-TRADERS Unit Test Suite
Covers Killzones, Risk Math, Correlation Caps, and Formatting
"""
import unittest
from datetime import datetime, timedelta
from typing import Dict, List
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config.settings import ACTIVE_SYMBOLS, PROP_CONFIG
from core.risk_manager import RiskManager
from strategies.base import Direction, Signal
from strategies.smc_ict_killzone import SMCICTKillzone
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

    def test_lot_size_gold_index_and_max_lot_cap(self):
        """Metals/indices size correctly and synthetics respect broker max_lot"""
        gold = Signal(symbol="XAUUSD", direction=Direction.BUY, strategy_tag="SMC_FIB",
                      entry_price=2650.00, stop_loss=2640.00, take_profit=2670.00, risk_reward=2.0)
        # $1000 risk per lot => 0.015 raw, snapped to the 0.01 broker lot step
        self.assertIn(self.risk_mgr.calculate_lot_size(gold), [0.01, 0.02])

        index = Signal(symbol="US30", direction=Direction.SELL, strategy_tag="SMC_FIB",
                       entry_price=23000.0, stop_loss=23100.0, take_profit=22800.0, risk_reward=2.0)
        self.assertEqual(self.risk_mgr.calculate_lot_size(index), 0.15)

        synthetic = Signal(symbol="VOL75", direction=Direction.BUY, strategy_tag="Z_SCORE",
                           entry_price=10.0, stop_loss=8.0, take_profit=14.0, risk_reward=2.0)
        # $2/lot risk => 7.5 raw lots, capped at max_lot 1.0
        self.assertEqual(self.risk_mgr.calculate_lot_size(synthetic), 1.0)

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
        self.assertEqual(ACTIVE_SYMBOLS["VOL75"].authorized_strategies, ["Z_SCORE"])
        self.assertEqual(ACTIVE_SYMBOLS["VOL75"].asset_class, "synthetic")
        self.assertEqual(ACTIVE_SYMBOLS["US30"].authorized_strategies, ["SMC_FIB"])
        self.assertEqual(ACTIVE_SYMBOLS["XAUUSD"].asset_class, "metal")
        self.assertIn("SMC_FIB", ACTIVE_SYMBOLS["EURUSD"].authorized_strategies)

    # ---------------- Z-Score Anchor ----------------
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


if __name__ == "__main__":
    unittest.main(verbosity=2)



