# ETH Breakout Bot / ETH 突破策略机器人

短周期 ETH/USDT 突破 + 成交量确认。**默认是本地纸面回测，不连接币安，也不需要 API Key。**

Short-term ETH/USDT breakout with volume confirmation. **The default is a local paper backtest: no Binance connection and no API keys.**

## 快速开始 / Quick start

需要 Python 3.10+。/ Python 3.10+ is required.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python eth_breakout_bot.py --backtest
```

不带参数时同样走本地回测（读取仓库里的样本 CSV）。/ With no flags, the script does the same local backtest on the bundled CSV.

```bash
python eth_breakout_bot.py
python eth_breakout_bot.py --backtest --data data/ethusdt_sample.csv
```

标准输出和 `eth_breakout_bot.log` 都会有汇总行，包含成交笔数、胜率、盈亏。/ Stdout and `eth_breakout_bot.log` both include a summary line with trade count, win rate, and PnL:

```text
2026-11-04 03:28:15 | INFO | BACKTEST_SUMMARY bars=144 trades=4 wins=2 losses=2 win_rate=50.00% pnl_usdt=0.8000
```

样本结果是固定的：4 笔，胜率 50%，纸面盈亏 +0.80 USDT（每笔名义本金 50 USDT）。这只说明回测路径跑通了，不是实盘收益。

The bundled sample is deterministic: 4 trades, 50% win rate, +0.80 USDT paper PnL on a 50 USDT notional. That only shows the backtest path works.

## 样本数据 / Sample data

`data/ethusdt_sample.csv` 是**合成的 1 分钟 OHLC**（144 根），专门用来离线演示。它不是交易所历史行情。文件头有说明；列格式为：

`data/ethusdt_sample.csv` is a **synthetic 1-minute OHLC** file (144 bars) so the demo works with no network. It is not exchange history. Columns:

```text
open_time,open,high,low,close,volume
```

以 `#` 开头的行会被忽略。/ Lines starting with `#` are ignored.

换成自己的 CSV 时保持这 6 列即可，例如从其他公开行情导出的 K 线。回测命令不访问网络。/ To use your own export, keep these 6 columns (a public OHLC dump is fine). The backtest command does not use the network.

## 策略 / Strategy

与原先脚本一致。/ Same rules as the original script.

- 用最近 `lookback_bars`（默认 20）根已收盘 K 线的最高价 / 最低价做区间。/ The range is the high and low of the previous `lookback_bars` closed candles (default 20).
- 信号 K 线的成交量必须 **大于** 近 `volume_ma_bars` 根均量 × `volume_multiplier`（默认 1.5）。均量包含信号 K 线自己。/ Signal-bar volume must be **greater than** the `volume_ma_bars` average times `volume_multiplier` (default 1.5). The average includes the signal bar.
- 收盘价向上突破区间最高价 → 做多；向下跌破区间最低价 → 做空。同一根若收盘在区间上方，记为做多。/ Close above the range high → long; close below the range low → short. A close above the range is a long.
- 止损 0.8%，止盈 1.6%（相对成交价）。回测只看收盘价，不看影线是否先碰到止损/止盈。/ Stop 0.8% and take-profit 1.6% from the fill. The backtest uses closes only, not intrabar wicks.
- 同一时间只持有一笔。回测在信号 K 线收盘价成交，之后的收盘价触发平仓。手续费、滑点、交易所最小下单量都不计入。未平仓位在最后一根按收盘价盯市（`EOD`）。/ One position at a time. Paper fills are at the signal close; later closes hit stop or target. Fees, slippage, and lot-size filters are not applied. An open trade is marked at the last close (`EOD`).

参数在 `eth_breakout_bot.py` 的 `CONFIG` 里。/ Tune `CONFIG` in `eth_breakout_bot.py`.

## 测试网与主网 / Testnet and mainnet

默认**不会**下单。实盘路径必须显式打开。/ The default path does not trade. Live paths are explicit.

```bash
# 币安现货测试网。需要环境变量里的 Key，不要写进代码。
# Binance Spot testnet. Keys stay in the environment, never in the source.
export BINANCE_API_KEY="your_testnet_key"
export BINANCE_API_SECRET="your_testnet_secret"
python eth_breakout_bot.py --testnet
```

测试网 Key：<https://testnet.binance.vision/>（用 GitHub 登录，与主网账户无关）。部分网络访问不了测试网，所以才把本地回测做成默认路径。

Testnet keys: <https://testnet.binance.vision/> (GitHub login; separate from mainnet). This repo defaults to local mode because that host is blocked in some regions.

主网还有一道保险，默认关闭。/ Mainnet has an extra latch and stays off:

```bash
export BINANCE_API_KEY="your_mainnet_key"
export BINANCE_API_SECRET="your_mainnet_secret"
export ALLOW_MAINNET=1
python eth_breakout_bot.py --mainnet
```

没有 `ALLOW_MAINNET=1` 时，`--mainnet` 会直接退出，不会连上交易所。建议关掉 API 的提现权限。主网会亏钱。

Without `ALLOW_MAINNET=1`, `--mainnet` exits before any connection. Keep withdraw permission off. Mainnet can lose money.

`--backtest`、`--testnet`、`--mainnet` 不能同时用。/ Those flags are mutually exclusive.

## 测试 / Tests

```bash
python -m unittest discover -s tests -v
```

覆盖突破信号、止损止盈、样本回测汇总，以及「不加保险就不能进主网」。/ Covers breakout signals, stops and targets, the sample summary, and the mainnet latch.

## 依赖 / Dependencies

`requirements.txt` 固定 `python-binance==1.0.37`，只给 `--testnet` / `--mainnet` 用。回测只用标准库。/ `python-binance==1.0.37` is pinned for the live flags. The backtest uses the standard library only.
