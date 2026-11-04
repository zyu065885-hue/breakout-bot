"""Unit tests for breakout signals and the offline paper backtest."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import eth_breakout_bot as bot  # noqa: E402


def kline(high, low, close, volume, open_=None, when=0):
    if open_ is None:
        open_ = close
    return [when, open_, high, low, close, volume]


def flat_bars(count, *, high=100.0, low=99.0, close=99.5, volume=10.0):
    return [kline(high, low, close, volume, when=i) for i in range(count)]


class BreakoutSignalTests(unittest.TestCase):
    def test_not_enough_bars(self):
        view = bot.evaluate_breakout(
            flat_bars(10),
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertFalse(view.ready)
        self.assertIsNone(view.signal)

    def test_long_breakout_with_volume(self):
        candles = flat_bars(20) + [kline(110.0, 99.2, 105.0, 100.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertTrue(view.ready)
        self.assertEqual(view.signal, "LONG")
        self.assertTrue(view.vol_ok)
        self.assertGreater(view.last_close, view.recent_high)

    def test_short_breakout_with_volume(self):
        candles = flat_bars(20) + [kline(99.8, 90.0, 94.0, 100.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertEqual(view.signal, "SHORT")

    def test_volume_must_exceed_average(self):
        candles = flat_bars(20) + [kline(110.0, 99.2, 105.0, 10.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertIsNone(view.signal)
        self.assertFalse(view.vol_ok)

    def test_equal_volume_threshold_is_not_enough(self):
        # Average includes the signal bar, so equal volume is not a confirmation.
        candles = flat_bars(20, volume=15.0) + [kline(110.0, 99.2, 105.0, 15.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.0,
            drop_forming=False,
        )
        self.assertTrue(view.ready)
        self.assertIsNone(view.signal)
        self.assertFalse(view.vol_ok)

    def test_no_signal_inside_the_range(self):
        candles = flat_bars(20) + [kline(100.0, 99.0, 99.6, 100.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertIsNone(view.signal)
        self.assertTrue(view.vol_ok)

    def test_live_mode_ignores_the_forming_candle(self):
        breakout = kline(110.0, 99.2, 105.0, 100.0, when=21)
        forming = kline(90.0, 80.0, 85.0, 500.0, when=22)
        view = bot.evaluate_breakout(
            flat_bars(21) + [breakout, forming],
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=True,
        )
        self.assertEqual(view.signal, "LONG")

        hidden = bot.evaluate_breakout(
            flat_bars(21) + [breakout],
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=True,
        )
        self.assertIsNone(hidden.signal)

    def test_close_above_range_is_long_even_if_wick_breaks_low(self):
        candles = flat_bars(20) + [kline(120.0, 80.0, 115.0, 100.0, when=20)]
        view = bot.evaluate_breakout(
            candles,
            lookback_bars=20,
            volume_ma_bars=20,
            volume_multiplier=1.5,
            drop_forming=False,
        )
        self.assertEqual(view.signal, "LONG")
        self.assertLess(candles[-1][3], view.recent_low)


class OrderHelperTests(unittest.TestCase):
    def test_round_step_floors(self):
        self.assertEqual(bot.round_step(1.23456789, 0.0001), 1.2345)
        self.assertEqual(bot.BreakoutBot.round_step(1.23456789, 0.0001), 1.2345)
        self.assertEqual(bot.round_step(0.5, 0), 0.5)

    def test_brackets_and_exit_reason(self):
        stop, take = bot.brackets("LONG", 100.0, 0.8, 1.6)
        self.assertAlmostEqual(stop, 99.2)
        self.assertAlmostEqual(take, 101.6)
        position = bot.Position("LONG", 100.0, 0.5, stop, take, "t0")
        self.assertIsNone(bot.exit_reason(position, 100.0))
        self.assertEqual(bot.exit_reason(position, 99.2), "STOP")
        self.assertEqual(bot.exit_reason(position, 101.6), "TAKE")

        stop, take = bot.brackets("SHORT", 100.0, 0.8, 1.6)
        self.assertAlmostEqual(stop, 100.8)
        self.assertAlmostEqual(take, 98.4)
        position = bot.Position("SHORT", 100.0, 0.5, stop, take, "t0")
        self.assertEqual(bot.exit_reason(position, 100.8), "STOP")
        self.assertEqual(bot.exit_reason(position, 98.4), "TAKE")

    def test_mainnet_latch_blocks_without_env(self):
        previous = bot.CONFIG["USE_TESTNET"]
        removed = os.environ.pop("ALLOW_MAINNET", None)
        bot.CONFIG["USE_TESTNET"] = False
        try:
            with self.assertRaises(RuntimeError):
                bot.assert_network_mode()
            os.environ["ALLOW_MAINNET"] = "1"
            bot.assert_network_mode()
        finally:
            bot.CONFIG["USE_TESTNET"] = previous
            if removed is None:
                os.environ.pop("ALLOW_MAINNET", None)
            else:
                os.environ["ALLOW_MAINNET"] = removed


class BacktestTests(unittest.TestCase):
    def test_long_take_profit_pnl(self):
        entry = 101.0
        exit_price = 102.7
        candles = flat_bars(20)
        candles.append(kline(101.2, 99.4, entry, 100.0, when=20))
        candles.append(kline(101.5, 100.5, 101.2, 10.0, when=21))
        candles.append(kline(exit_price, 101.2, exit_price, 10.0, when=22))

        summary = bot.run_backtest(candles)
        self.assertEqual(summary.trade_count, 1)
        self.assertEqual(summary.wins, 1)
        self.assertEqual(summary.losses, 0)
        trade = summary.trades[0]
        self.assertEqual(trade.side, "LONG")
        self.assertEqual(trade.reason, "TAKE")
        self.assertAlmostEqual(trade.pnl, (exit_price - entry) * (50.0 / entry))
        self.assertAlmostEqual(summary.win_rate, 100.0)
        text = bot.format_summary(summary)
        self.assertIn("trades=1", text)
        self.assertIn("win_rate=100.00%", text)
        self.assertIn("pnl_usdt=", text)

    def test_short_stop_is_a_loss(self):
        entry = 98.0
        exit_price = 99.0  # stop is entry * 1.008 = 98.784
        candles = flat_bars(20)
        candles.append(kline(99.2, entry, entry, 100.0, when=20))
        candles.append(kline(exit_price, 98.2, exit_price, 10.0, when=21))

        summary = bot.run_backtest(candles)
        self.assertEqual(summary.trade_count, 1)
        trade = summary.trades[0]
        self.assertEqual(trade.side, "SHORT")
        self.assertEqual(trade.reason, "STOP")
        self.assertLess(trade.pnl, 0)
        self.assertAlmostEqual(trade.pnl, (entry - exit_price) * (50.0 / entry))
        self.assertEqual(summary.wins, 0)
        self.assertEqual(summary.losses, 1)

    def test_open_position_marks_to_market_at_end(self):
        entry = 101.0
        candles = flat_bars(20)
        candles.append(kline(101.2, 99.4, entry, 100.0, when=20))
        candles.append(kline(101.4, 100.8, 101.1, 10.0, when=21))
        summary = bot.run_backtest(candles)
        self.assertEqual(summary.trades[0].reason, "EOD")
        self.assertAlmostEqual(summary.trades[0].exit, 101.1)

    def test_csv_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.csv"
            path.write_text(
                "# comment\n"
                "open_time,open,high,low,close,volume\n"
                "2024-01-01T00:00:00Z,1,2,0.5,1.5,10\n",
                encoding="utf-8",
            )
            candles = bot.load_ohlcv_csv(path)
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0][0], "2024-01-01T00:00:00Z")
        self.assertEqual(candles[0][4], 1.5)

    def test_bundled_sample_backtest(self):
        candles = bot.load_ohlcv_csv(bot.DEFAULT_DATA)
        summary = bot.run_backtest(candles)
        self.assertGreaterEqual(summary.bars, 100)
        self.assertEqual(summary.trade_count, 4)
        self.assertEqual(summary.wins, 2)
        self.assertEqual(summary.losses, 2)
        self.assertAlmostEqual(summary.win_rate, 50.0)
        self.assertAlmostEqual(summary.pnl, 0.8, places=4)
        self.assertNotIn("binance", sys.modules)


class CliTests(unittest.TestCase):
    def test_backtest_command(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "eth_breakout_bot.py"), "--backtest"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("BACKTEST_SUMMARY", proc.stdout)
        self.assertIn("trades=4", proc.stdout)
        self.assertIn("win_rate=50.00%", proc.stdout)
        self.assertIn("pnl_usdt=", proc.stdout)
        self.assertIn("mode=local paper-sim", proc.stdout)

    def test_mainnet_flag_refuses_without_latch(self):
        env = os.environ.copy()
        env.pop("ALLOW_MAINNET", None)
        env.pop("BINANCE_API_KEY", None)
        env.pop("BINANCE_API_SECRET", None)
        proc = subprocess.run(
            [sys.executable, str(ROOT / "eth_breakout_bot.py"), "--mainnet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, combined)
        self.assertIn("ALLOW_MAINNET", combined)
        self.assertNotIn("Connected.", combined)

    def test_mixed_flags_rejected(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "eth_breakout_bot.py"), "--backtest", "--testnet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Choose only one", proc.stderr)


if __name__ == "__main__":
    unittest.main()
