# Signals workspace — intraday strategies, backtests and signals

Decision support, not investment advice. Nothing here places orders; stop/target/trailing exist only inside historical simulation.

## Data and timeframes
- Source of truth: stored non-synthetic **1-minute** bars (`market_bars.interval='1m'`, open-time stamped). Derived timeframes: 3m/5m/10m/15m/30m/60m (`services/timeframes.py`; add one with `register_timeframe`).
- Indian equity candles are anchored to the 09:15 IST open and truncated at 15:30 (a 60m grid is 09:15…14:15 plus a 15-minute 15:15 candle); they never cross sessions. Crypto is continuous, UTC-epoch anchored.
- A derived candle is exposed only when it has **completed** (`bar_end <= base bar close`). Forming candles are never returned. Missing 1m rows are flagged (`minutes_present < minutes_expected`), never invented.
- `services/bar_quality.py` validates ordering, duplicates, OHLC/volume, out-of-session rows and gaps. Backtests refuse an instrument whose missing share exceeds the configured limit (default 5%). The repository holds no exchange-holiday calendar: weekdays with no data are reported as `empty_weekdays`, not as missing minutes. Prices are not adjusted for splits/bonus; large overnight gaps are flagged.

## Strategy definition (`intraday-strategy/1`)
Versioned JSON produced by the visual builder, the Pine importer and the Python importer: indicators (alias, id, params, timeframe), condition trees (AND/OR/NOT, compare, crossover, time windows, arithmetic operands, "within N bars"), sizing, stop/target/trail, session rules, constraints, and source provenance (original file text + SHA-256). Evaluation is data-driven numpy; there is no `eval`. Saved versions are immutable (DB guard); editing creates a new version; a backtest records the exact definition snapshot and hash.

## Backtest execution model
Signals exist at the close of a base bar; entries fill at the **next bar's open** (skipped if that bar is not contiguous). Slippage is adverse and rounded to the tick; limit (target) fills get none. Gap through a stop fills at the open. Stop and target in the same 1-minute bar → **stop first**, flagged. Trailing uses extremes through the previous bar. Equity intraday is forced flat at square-off (default 15:15 IST); crypto has no session rules. Charges come from the central cost engine (`services/transaction_costs.py`: Dhan NSE/BSE intraday and delivery schedules, brokerage once per order, STT/stamp to the rupee, IPFT, GST on brokerage+exchange+SEBI), computed once per trade on executed prices. Slippage is a separate model (bps of price, unrounded, or N ticks). Income tax is never included (`services/indian_tax.py`). Rates are assumptions to verify against a Dhan contract note. Every result echoes its assumptions, engine/indicator versions and a dataset SHA-256.

## Python strategy interface
See the template in the Import tab (`GET /signals-lab/meta`). A `Strategy` class declares `INDICATORS`, `DECISION_TIMEFRAME`, `PARAMS`, `ALLOW_SHORT`, and implements `on_bar(ctx)`; `ctx` exposes read-only history up to the current completed candle, indicator series, and the strategy's own intended position. Allowed imports: math, statistics, itertools, collections, functools, datetime, decimal, fractions, typing, dataclasses, enum.
Isolation (best-effort, **not** a container boundary): AST screen, separate `python -I -S` process, scrubbed environment, empty temp cwd, CPU/memory/file/fork rlimits, wall-clock kill, whitelisted builtins and module proxies.

## Pine Script
Only `//@version=5` and the documented subset (`GET /signals-lab/meta` → `pine`). Unsupported constructs are reported with line numbers and block the import; display-only calls are listed as ignored. Imports are never claimed to match TradingView.
