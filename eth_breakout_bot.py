#!/usr/bin/env python3
"""
eth_breakout_bot.py — ETH/USDT breakout strategy with a local paper backtest.

Default mode is offline. It reads OHLC from a CSV and never contacts Binance.
Live testnet / mainnet trading stays behind explicit flags and environment keys.

Quick start (no API keys):

    python eth_breakout_bot.py --backtest

See README.md for the bilingual walkthrough. Testnet keys, if you use them,
belong in BINANCE_API_KEY / BINANCE_API_SECRET — never in this file.
Mainnet additionally requires ALLOW_MAINNET=1.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional, Sequence

# ---------------------------------------------------------------------------
# CONFIG — edit these, not secrets
# ---------------------------------------------------------------------------
CONFIG = {
    "symbol": "ETHUSDT",
    "timeframe": "1m",  # Binance kline interval: 1m, 3m, 5m, 15m, ...
    "lookback_bars": 20,  # recent high/low window (excluding the signal bar)
    "volume_ma_bars": 20,  # average volume window (includes the signal bar)
    "volume_multiplier": 1.5,  # breakout volume must be > avg * this
    "stop_loss_pct": 0.8,  # percent from entry
    "take_profit_pct": 1.6,  # percent from entry
    "quote_order_size": 50.0,  # USDT notional per entry
    "poll_seconds": 15,  # main loop sleep between evaluations
    "max_retries": 5,
    "retry_base_seconds": 2.0,
    "recv_window": 5000,
    "log_file": "eth_breakout_bot.log",
    "USE_TESTNET": True,  # live path only; ignored by --backtest
}

# Binance Spot Testnet REST base (python-binance testnet flag uses this)
TESTNET_API = "https://testnet.binance.vision/api"

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "data" / "ethusdt_sample.csv"

log = logging.getLogger("eth_breakout")
log.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("eth_breakout")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.INFO)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@dataclass
class Position:
    side: str  # "LONG" or "SHORT"
    entry: float
    qty: float
    stop: float
    take: float
    opened_at: str


@dataclass(frozen=True)
class BreakoutView:
    """Diagnostics for one breakout evaluation. `signal` is LONG, SHORT, or None."""

    signal: Optional[str]
    ready: bool
    bar_count: int
    needed: int
    last_close: float = 0.0
    recent_high: float = 0.0
    recent_low: float = 0.0
    last_vol: float = 0.0
    avg_vol: float = 0.0
    vol_ok: bool = False


@dataclass(frozen=True)
class ClosedTrade:
    side: str
    entry: float
    exit: float
    qty: float
    pnl: float
    reason: str
    opened_at: str
    closed_at: str


@dataclass
class BacktestSummary:
    bars: int
    trades: list[ClosedTrade]
    quote_order_size: float

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for trade in self.trades if trade.pnl < 0)

    @property
    def pnl(self) -> float:
        return sum(trade.pnl for trade in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return 100.0 * self.wins / len(self.trades)


def require_env_keys() -> tuple[str, str]:
    key = os.environ.get("BINANCE_API_KEY", "").strip()
    secret = os.environ.get("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError(
            "Missing BINANCE_API_KEY or BINANCE_API_SECRET in environment. "
            "Live mode is optional; use --backtest for the offline paper sim. "
            "Testnet keys come from https://testnet.binance.vision/."
        )
    return key, secret


def assert_network_mode() -> None:
    if CONFIG["USE_TESTNET"]:
        log.info("Network mode: BINANCE SPOT TESTNET")
        return
    if os.environ.get("ALLOW_MAINNET", "").strip() != "1":
        raise RuntimeError(
            "USE_TESTNET is False but ALLOW_MAINNET is not set to 1. "
            "Refusing to start on mainnet without the safety latch."
        )
    log.warning("Network mode: MAINNET — real funds at risk")


def with_retries(fn, *, what: str, retries: int, base: float):
    last_exc: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — broker/network errors vary
            last_exc = exc
            sleep_for = base * (2 ** (attempt - 1))
            log.warning(
                "%s failed (attempt %s/%s): %s; retry in %.1fs",
                what,
                attempt,
                retries,
                exc,
                sleep_for,
            )
            time.sleep(sleep_for)
    assert last_exc is not None
    raise last_exc


def round_step(qty: float, step: float) -> float:
    """Floor quantity to the exchange LOT_SIZE step."""
    if step <= 0:
        return qty
    precision = max(0, len(str(step).rstrip("0").split(".")[-1]))
    floored = (int(qty / step)) * step
    return float(f"{floored:.{precision}f}")


def brackets(side: str, entry: float, stop_loss_pct: float, take_profit_pct: float) -> tuple[float, float]:
    """Stop and take-profit prices for a filled entry."""
    if side == "LONG":
        stop = entry * (1 - stop_loss_pct / 100.0)
        take = entry * (1 + take_profit_pct / 100.0)
    elif side == "SHORT":
        stop = entry * (1 + stop_loss_pct / 100.0)
        take = entry * (1 - take_profit_pct / 100.0)
    else:
        raise ValueError(f"unknown side: {side}")
    return stop, take


def trade_pnl(side: str, entry: float, exit_price: float, qty: float) -> float:
    if side == "LONG":
        return (exit_price - entry) * qty
    if side == "SHORT":
        return (entry - exit_price) * qty
    raise ValueError(f"unknown side: {side}")


def exit_reason(position: Position, last_close: float) -> Optional[str]:
    """Close-only stop/take check, matching the live loop (no intrabar fills)."""
    if position.side == "LONG":
        hit_stop = last_close <= position.stop
        hit_take = last_close >= position.take
    elif position.side == "SHORT":
        hit_stop = last_close >= position.stop
        hit_take = last_close <= position.take
    else:
        raise ValueError(f"unknown side: {position.side}")
    if not (hit_stop or hit_take):
        return None
    return "STOP" if hit_stop else "TAKE"


def evaluate_breakout(
    klines: Sequence[Sequence[Any]],
    *,
    lookback_bars: int,
    volume_ma_bars: int,
    volume_multiplier: float,
    drop_forming: bool = True,
) -> BreakoutView:
    """
    Breakout + volume confirmation.

    Kline layout: open_time, open, high, low, close, volume, ...
    Live mode passes Binance klines and drops the still-forming last candle
    (`drop_forming=True`). The backtest passes closed bars only
    (`drop_forming=False`) and scores the last row.

    The volume average includes the signal bar, same as the original bot.
    LONG is preferred when one candle somehow satisfies both sides.
    """
    raw_count = len(klines)
    needed = max(int(lookback_bars), int(volume_ma_bars)) + (2 if drop_forming else 1)
    if raw_count < needed:
        return BreakoutView(signal=None, ready=False, bar_count=raw_count, needed=needed)

    closed = list(klines[:-1] if drop_forming else klines)
    lookback = closed[-(int(lookback_bars) + 1) : -1]
    last = closed[-1]
    vols = [float(candle[5]) for candle in closed[-int(volume_ma_bars) :]]
    avg_vol = sum(vols) / len(vols) if vols else 0.0

    last_high = float(last[2])
    last_low = float(last[3])
    last_close = float(last[4])
    last_vol = float(last[5])
    recent_high = max(float(candle[2]) for candle in lookback)
    recent_low = min(float(candle[3]) for candle in lookback)
    vol_ok = last_vol > avg_vol * float(volume_multiplier)

    signal: Optional[str] = None
    if vol_ok and last_close > recent_high and last_high >= recent_high:
        signal = "LONG"
    elif vol_ok and last_close < recent_low and last_low <= recent_low:
        signal = "SHORT"

    return BreakoutView(
        signal=signal,
        ready=True,
        bar_count=raw_count,
        needed=needed,
        last_close=last_close,
        recent_high=recent_high,
        recent_low=recent_low,
        last_vol=last_vol,
        avg_vol=avg_vol,
        vol_ok=vol_ok,
    )


def load_ohlcv_csv(path: Path) -> list[list[Any]]:
    """Load open_time,open,high,low,close,volume. Lines starting with # are ignored."""
    if not path.is_file():
        raise FileNotFoundError(
            f"OHLC file not found: {path}. "
            f"Pass --data PATH or use the bundled sample {DEFAULT_DATA}."
        )

    candles: list[list[Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(_skip_comment_lines(handle))
        for line_no, row in enumerate(reader, start=1):
            if not row or all(not cell.strip() for cell in row):
                continue
            if line_no == 1 and row[0].strip().lower() in {"open_time", "timestamp", "time"}:
                continue
            if len(row) < 6:
                raise ValueError(f"{path}:{line_no}: expected 6 columns, got {len(row)}")
            try:
                candles.append(
                    [
                        row[0].strip(),
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                        float(row[5]),
                    ]
                )
            except ValueError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
    if not candles:
        raise ValueError(f"No OHLC rows in {path}")
    return candles


def _skip_comment_lines(handle):
    for line in handle:
        if line.lstrip().startswith("#"):
            continue
        yield line


def run_backtest(
    candles: Sequence[Sequence[Any]],
    config: Optional[dict[str, Any]] = None,
) -> BacktestSummary:
    """
    Walk closed candles once. At most one position is open.

    Fills are paper-only: entry at the signal bar close, exit at a later
    close through stop or take-profit. No fees, slippage, or lot-size rounding.
    A position still open on the last bar is marked at that close (reason EOD).
    """
    cfg = CONFIG if config is None else config
    lookback = int(cfg["lookback_bars"])
    volume_ma = int(cfg["volume_ma_bars"])
    volume_mult = float(cfg["volume_multiplier"])
    stop_pct = float(cfg["stop_loss_pct"])
    take_pct = float(cfg["take_profit_pct"])
    notional = float(cfg["quote_order_size"])

    position: Optional[Position] = None
    closed_trades: list[ClosedTrade] = []

    for i in range(len(candles)):
        window = candles[: i + 1]
        last = window[-1]
        last_close = float(last[4])
        stamp = str(last[0])

        if position is not None:
            reason = exit_reason(position, last_close)
            if reason is None:
                continue
            closed_trades.append(_close_position(position, last_close, reason, stamp))
            position = None
            continue

        view = evaluate_breakout(
            window,
            lookback_bars=lookback,
            volume_ma_bars=volume_ma,
            volume_multiplier=volume_mult,
            drop_forming=False,
        )
        if not view.ready or not view.signal:
            continue
        if last_close <= 0:
            continue
        qty = notional / last_close
        stop, take = brackets(view.signal, last_close, stop_pct, take_pct)
        position = Position(
            side=view.signal,
            entry=last_close,
            qty=qty,
            stop=stop,
            take=take,
            opened_at=stamp,
        )

    if position is not None and candles:
        last = candles[-1]
        closed_trades.append(
            _close_position(position, float(last[4]), "EOD", str(last[0]))
        )

    return BacktestSummary(
        bars=len(candles),
        trades=closed_trades,
        quote_order_size=notional,
    )


def _close_position(position: Position, exit_price: float, reason: str, stamp: str) -> ClosedTrade:
    return ClosedTrade(
        side=position.side,
        entry=position.entry,
        exit=exit_price,
        qty=position.qty,
        pnl=trade_pnl(position.side, position.entry, exit_price, position.qty),
        reason=reason,
        opened_at=position.opened_at,
        closed_at=stamp,
    )


def format_summary(summary: BacktestSummary) -> str:
    """Single-line plus per-trade text for stdout and the log file."""
    lines = [
        (
            f"BACKTEST_TRADE n={index} side={trade.side} entry={trade.entry:.4f} "
            f"exit={trade.exit:.4f} qty={trade.qty:.6f} reason={trade.reason} "
            f"pnl_usdt={trade.pnl:.4f}"
        )
        for index, trade in enumerate(summary.trades, start=1)
    ]
    lines.append(
        "BACKTEST_SUMMARY "
        f"bars={summary.bars} trades={summary.trade_count} "
        f"wins={summary.wins} losses={summary.losses} "
        f"win_rate={summary.win_rate:.2f}% pnl_usdt={summary.pnl:.4f}"
    )
    return "\n".join(lines)


def run_backtest_cli(data_path: Path) -> BacktestSummary:
    candles = load_ohlcv_csv(data_path)
    summary = run_backtest(candles)
    log.info(
        "BACKTEST mode=local paper-sim exchange=none symbol=%s data=%s",
        CONFIG["symbol"],
        data_path,
    )
    for line in format_summary(summary).splitlines():
        log.info("%s", line)
    return summary


# ---------------------------------------------------------------------------
# Exchange client (live flags only)
# ---------------------------------------------------------------------------
def build_client():
    try:
        from binance.client import Client
        from binance.exceptions import BinanceAPIException, BinanceRequestException
    except ImportError as exc:
        raise RuntimeError(
            "python-binance is not installed. Run: pip install -r requirements.txt"
        ) from exc

    api_key, api_secret = require_env_keys()
    client = Client(
        api_key,
        api_secret,
        testnet=bool(CONFIG["USE_TESTNET"]),
        requests_params={"timeout": 20},
    )
    # Explicit testnet URL when on testnet (belt and suspenders)
    if CONFIG["USE_TESTNET"]:
        client.API_URL = TESTNET_API
    return client, BinanceAPIException, BinanceRequestException


class BreakoutBot:
    def __init__(self) -> None:
        assert_network_mode()
        self.client = None
        self.BinanceAPIException = Exception
        self.BinanceRequestException = Exception
        self.position: Optional[Position] = None
        self._connect()

    def _connect(self) -> None:
        def _make():
            client, api_exc, req_exc = build_client()
            # ping + account to validate keys / connectivity
            client.ping()
            client.get_account()
            return client, api_exc, req_exc

        client, api_exc, req_exc = with_retries(
            _make,
            what="connect/ping",
            retries=int(CONFIG["max_retries"]),
            base=float(CONFIG["retry_base_seconds"]),
        )
        self.client = client
        self.BinanceAPIException = api_exc
        self.BinanceRequestException = req_exc
        log.info("Connected. symbol=%s interval=%s", CONFIG["symbol"], CONFIG["timeframe"])

    def reconnect(self) -> None:
        log.warning("Reconnecting to Binance...")
        self.client = None
        time.sleep(float(CONFIG["retry_base_seconds"]))
        self._connect()

    def _call(self, what: str, fn):
        try:
            return with_retries(
                fn,
                what=what,
                retries=int(CONFIG["max_retries"]),
                base=float(CONFIG["retry_base_seconds"]),
            )
        except (self.BinanceAPIException, self.BinanceRequestException, OSError) as exc:
            log.error("%s hard-failed: %s — attempting reconnect", what, exc)
            self.reconnect()
            return with_retries(
                fn,
                what=what + " (after reconnect)",
                retries=int(CONFIG["max_retries"]),
                base=float(CONFIG["retry_base_seconds"]),
            )

    def fetch_klines(self) -> list[list[Any]]:
        limit = max(CONFIG["lookback_bars"], CONFIG["volume_ma_bars"]) + 5

        def _fn():
            return self.client.get_klines(
                symbol=CONFIG["symbol"],
                interval=CONFIG["timeframe"],
                limit=limit,
            )

        return self._call("get_klines", _fn)

    def symbol_filters(self) -> tuple[float, float]:
        """Return (step_size, min_qty) for LOT_SIZE."""

        def _fn():
            return self.client.get_symbol_info(CONFIG["symbol"])

        info = self._call("get_symbol_info", _fn)
        if not info:
            raise RuntimeError(f"No symbol info for {CONFIG['symbol']}")
        step = 0.0001
        min_qty = 0.0
        for filt in info.get("filters", []):
            if filt.get("filterType") == "LOT_SIZE":
                step = float(filt["stepSize"])
                min_qty = float(filt["minQty"])
        return step, min_qty

    # Live orders still floor to the symbol LOT_SIZE step.
    round_step = staticmethod(round_step)

    def evaluate_signal(self, klines: list[list[Any]]) -> Optional[str]:
        view = evaluate_breakout(
            klines,
            lookback_bars=int(CONFIG["lookback_bars"]),
            volume_ma_bars=int(CONFIG["volume_ma_bars"]),
            volume_multiplier=float(CONFIG["volume_multiplier"]),
            drop_forming=True,
        )
        if not view.ready:
            log.info("Not enough candles yet (%s < %s)", view.bar_count, view.needed)
            return None

        log.info(
            "SIGNAL_CHECK close=%.4f recent_high=%.4f recent_low=%.4f "
            "vol=%.2f avg_vol=%.2f vol_ok=%s",
            view.last_close,
            view.recent_high,
            view.recent_low,
            view.last_vol,
            view.avg_vol,
            view.vol_ok,
        )
        return view.signal

    def place_market(self, side: str, quantity: float) -> dict:
        def _fn():
            return self.client.create_order(
                symbol=CONFIG["symbol"],
                side=side,
                type="MARKET",
                quantity=quantity,
                recvWindow=CONFIG["recv_window"],
            )

        order = self._call(f"create_order {side}", _fn)
        log.info("TRADE %s qty=%s resp=%s", side, quantity, order)
        return order

    def open_position(self, signal: str, last_close: float) -> None:
        step, min_qty = self.symbol_filters()
        notional = float(CONFIG["quote_order_size"])
        raw_qty = notional / last_close
        qty = self.round_step(raw_qty, step)
        if qty < min_qty:
            log.error("Qty %.8f below min_qty %.8f — skip", qty, min_qty)
            return

        if signal == "LONG":
            self.place_market("BUY", qty)
        else:
            self.place_market("SELL", qty)
        stop, take = brackets(
            signal,
            last_close,
            float(CONFIG["stop_loss_pct"]),
            float(CONFIG["take_profit_pct"]),
        )

        self.position = Position(
            side=signal,
            entry=last_close,
            qty=qty,
            stop=stop,
            take=take,
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "POSITION_OPEN %s entry=%.4f qty=%s stop=%.4f take=%.4f",
            self.position.side,
            self.position.entry,
            self.position.qty,
            self.position.stop,
            self.position.take,
        )

    def manage_position(self, last_close: float) -> None:
        pos = self.position
        if not pos:
            return

        reason = exit_reason(pos, last_close)
        if reason is None:
            return

        exit_side = "SELL" if pos.side == "LONG" else "BUY"
        log.info(
            "POSITION_EXIT signal=%s reason=%s price=%.4f",
            pos.side,
            reason,
            last_close,
        )
        self.place_market(exit_side, pos.qty)
        log.info("TRADE_CLOSED %s @ %.4f (%s)", pos.side, last_close, reason)
        self.position = None

    def run_forever(self) -> None:
        log.info(
            "Bot starting | strategy=breakout+volume | SL=%.2f%% TP=%.2f%% "
            "vol_x=%.2f lookback=%s",
            CONFIG["stop_loss_pct"],
            CONFIG["take_profit_pct"],
            CONFIG["volume_multiplier"],
            CONFIG["lookback_bars"],
        )
        while True:
            try:
                klines = self.fetch_klines()
                last_close = float(klines[-2][4])  # last closed close
                if self.position:
                    self.manage_position(last_close)
                else:
                    signal = self.evaluate_signal(klines)
                    if signal:
                        log.info("SIGNAL %s at close=%.4f", signal, last_close)
                        self.open_position(signal, last_close)
                time.sleep(float(CONFIG["poll_seconds"]))
            except KeyboardInterrupt:
                log.info("Stopped by user")
                break
            except Exception as exc:  # noqa: BLE001
                log.exception("Main loop error: %s", exc)
                try:
                    self.reconnect()
                except Exception as rexc:  # noqa: BLE001
                    log.exception("Reconnect failed: %s", rexc)
                    time.sleep(float(CONFIG["retry_base_seconds"]) * 3)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "ETH/USDT breakout bot. Default is an offline CSV backtest "
            "(no API keys, no exchange). Testnet and mainnet are opt-in."
        )
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Run the local paper backtest (this is also the default).",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help=f"OHLC CSV for --backtest (default: {DEFAULT_DATA}).",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Trade on Binance Spot testnet. Requires API key environment variables.",
    )
    parser.add_argument(
        "--mainnet",
        action="store_true",
        help="Trade on Binance mainnet. Requires keys and ALLOW_MAINNET=1.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    setup_logger(str(CONFIG["log_file"]))

    live_flags = int(bool(args.testnet)) + int(bool(args.mainnet))
    if live_flags > 1 or (live_flags and (args.backtest or args.data is not None)):
        parser.error("Choose only one of --backtest/--data, --testnet, or --mainnet.")

    if args.mainnet:
        CONFIG["USE_TESTNET"] = False
        log.warning("CONFIG.USE_TESTNET is False")
        assert_network_mode()
        BreakoutBot().run_forever()
        return 0

    if args.testnet:
        CONFIG["USE_TESTNET"] = True
        assert_network_mode()
        BreakoutBot().run_forever()
        return 0

    data_path = args.data if args.data is not None else DEFAULT_DATA
    run_backtest_cli(data_path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
