# 03 — Event-driven Backtesting Engine (DESIGN ONLY)

Purpose: replay a saved strategy definition over **stored, completed historical 1m bars** and report statistics about the past. It never
represents a current position, an order, or money at risk, and has **no paper-trading or live mode** (AGENTS.md). Extends the verified
conventions of `services/backtest_engine.py` (next-bar fills, stop-first ambiguity, Decimal costs, synthetic-bar rejection).

## 1. Components and data flow
```
BarStore (Postgres 1m, Dhan)  →  HistoricalDataAdapter  →  SessionCalendar  →  EventClock
        →  Aggregator (1m→base/higher TF, completed only)  →  IndicatorState (incremental, per instrument/TF)
        →  StrategyEvaluator.on_bar  →  SignalIntent  →  RiskGate  →  OrderSimulator  →  FillModel (costs, slippage)
        →  PositionBook / PortfolioLedger  →  Recorder (trades, equity, diagnostics)  →  Analytics
```
* **HistoricalDataAdapter** streams 1m rows per session in timestamp order, per instrument; rejects `is_synthetic`, non-positive or inconsistent OHLC, and
  records duplicates (unique constraint makes them impossible in storage; the adapter still asserts it).
* **SessionCalendar** — single source for session start/end, holidays, special sessions (Budget Saturday, Muhurat). Must be extended beyond 2026
  (audit gap 1) or the run is refused for dates without a verified calendar.
* **EventClock** advances by 1m bar close time. Events: `BarClosed(1m)`, derived `TimeframeBarClosed(tf)`, `SessionOpen`, `SessionSquareOff`, `SessionClose`.
  Wall-clock time is never read.
* **Aggregator** reuses the audited bucket rules (venue-session-aligned, bucket complete only when all constituent 1m bars exist) from a shared
  service extracted from `api/market_data.py`.
* **IndicatorState** is incremental and consumes only bars closed ≤ now. Warm-up uses prior sessions' bars for non-session indicators;
  session indicators (VWAP, opening range, day high/low) reset each session.
* **RiskGate** applies the definition's `risk` limits and `session` windows; **OrderSimulator** turns an accepted intent into a *simulated fill request*
  (internal to the run, never serialised as an order); **FillModel** prices it.
* **Recorder** writes append-only rows; the run is reproducible from `(definition_hash, data window, bar-set fingerprint, assumptions, engine version)`.

## 2. Timing rules (no look-ahead)
1. A decision at the close of bar *t* uses only bars with `close_time ≤ t`. `completed_frame()` semantics from `strategy_contract.py` are the template.
2. **Entries and indicator exits fill at the open of bar *t+1*** (next-bar execution) plus slippage. If bar *t+1* does not exist (gap, or session ended) the signal
   is **cancelled and counted** (`unfilled_signals`), never carried over a session.
3. Higher-timeframe values come from the **last completed** higher bar; the in-progress higher bar is invisible (verified by a shift test, §8).
4. The incomplete final bucket of a derived timeframe is never emitted. Data end-of-window truncates to the last complete bar.
5. Stops/targets set from an entry are active from the bar *after* the fill bar's fill moment; within the fill bar itself they are evaluated using that bar's
   range **after** the open fill (conservatively, see §3).
6. Trailing stops update from completed bars only and take effect from the next bar.
7. Position sizing uses equity as of the decision bar close.

## 3. Intra-bar ambiguity (stop and target in one candle)
OHLC cannot say which came first. Policy, in order:
1. If the bar opens through a level (gap): fill at the **open**, not at the level (existing `stop_gap`/`target_gap` behaviour).
2. If only one level is touched: fill at that level ± slippage.
3. If both are touched: **resolve with finer data** — step through the 1m bars of that candle (only when base TF > 1m and all constituent 1m bars exist);
   first level reached wins. On a 1m base, or if 1m bars are missing, assume **stop first** (adverse). Each case is tagged `resolved_by_1m` or
   `ambiguous_stop_first` and the count is reported; a run with ambiguity above a threshold (default 5% of trades) is flagged as unreliable.
4. If a single 1m bar itself touches both, use stop first.
5. Favourable assumptions (target first, mid-bar fills, touch-equals-fill for limit orders at the level without a tick) are never used. A target fill requires
   price **trading through** the level by ≥ 1 tick (config), stops fill on touch.

## 4. Fills, costs and constraints
* **Slippage**: bps of price, plus an ATR-scaled component for fast bars; applied adversely on every fill (buy higher, sell lower); stop fills get extra
  slippage. Default values come from a stated profile and are echoed in results — none are tuned silently.
* **Costs** (`nse_equity_intraday/1`, Decimal, all parameters versioned and shown): brokerage (flat/percentage cap), exchange transaction charges, SEBI fee, STT on
  sell side (intraday), stamp duty on buy side, GST on brokerage + exchange charges. Rates are config values with an `effective_from` date; a run before the earliest
  configured rate date is refused. DP charges do not apply to intraday.
* **Liquidity/partial fills**: order size is capped at a fraction of the bar's volume (default 10%). Excess is either partially filled (remaining carried to the next
  bar only for exits, never for entries) or cancelled; entries on a zero-volume bar are not filled. Partial fills are recorded as separate fill rows.
* **Short selling**: only intraday-square-off shorts in NSE cash equities are modelled; no borrow/delivery shorts; mandatory square-off; the instrument must be in an
  allowed list (config) — otherwise the intent is rejected with a reason, not silently converted.
* **Session / end of day**: all positions are flattened at `square_off_at` at the next available bar open (or the bar close if it is the last bar) with the same
  slippage and costs; no overnight positions exist in this module. Orders cannot span sessions.
* **Gaps in data**: a missing 1m bar → the affected derived buckets are dropped (existing behaviour). If a position is open and bars are missing, the position
  is marked `data_gap_open` and exited at the next available open, flagged; a day with more than N% missing minutes (default 5%) is skipped entirely for entries and listed.
  No interpolation, forward-fill or backfill from other providers.
* **Corporate actions**: a window containing a split/bonus/dividend ex-date for an instrument is refused until intraday adjustment is verified (audit gap 7).

## 5. Accounting
Decimal throughout (indicators may use floats internally; fills, P&L, costs and equity are Decimal, converted from string, never `Decimal(float)`). Per-instrument
position book (quantity, average price, side), cash ledger, realised/unrealised P&L marked on bar closes, equity curve at every closed base bar. Cross-currency
runs are refused (no FX).

## 6. Persistence (migration 0048/0049, additive)
`backtest_runs(id, owner_id, strategy_version_id, status, window, instruments, assumptions JSONB, cost_profile, engine_version, data_fingerprint, coverage JSONB,
created_at, finished_at, error)`; `backtest_trades(run_id, instrument_id, side, signal_ts, entry_ts, entry_price, exit_ts, exit_price, qty, gross_pnl, costs, net_pnl,
exit_reason, ambiguity, mae, mfe, bars_held)`; `backtest_equity(run_id, ts, equity, exposure)`; `backtest_diagnostics(run_id, kind, payload)`. Results are immutable; re-running creates a
new run. Every row owner-scoped.

## 7. Analytics (reported with window, bar count, source, costs, assumptions — mandatory per AGENTS.md)
Net return (absolute and % of starting equity), gross vs net, win rate, profit factor, expectancy (per trade and in R), average win/loss and payoff ratio, max drawdown
(depth and duration), trade count, exposure (% of session-minutes in market), average/median holding time, daily and monthly return tables, best/worst trades, distribution by
time of day, long vs short split, cost drag, ambiguity and unfilled-signal counts, data coverage and skipped-day list. Extra risk-adjusted measures (Sharpe/Sortino on
daily returns) are shown only with ≥ 60 trading days. Existing `trade_metrics.summarize_net_pnls` supplies the base definitions; no number is produced by the LLM.

## 8. Validation protocol and overfitting safeguards
* **In-sample / out-of-sample**: a chronological split fixed *before* running (default 70/30, never random); parameters are chosen on in-sample only; the
  out-of-sample result is reported separately and cannot be used to re-tune without being relabelled in-sample.
* **Walk-forward**: rolling train/test windows (e.g. 60 days train, 20 test, step 20); report each test window and the stitched out-of-sample equity.
* **Parameter sensitivity**: ±1–2 grid steps around the chosen parameters; report the surface and flag *fragile* optima (performance drop > 50% for a one-step change).
* **Regime analysis**: split results by trend/range (ADX), volatility tercile (ATR%), and gap-open vs normal days; require the edge not to come from one bucket.
* **Multiple-testing guard**: every parameter combination tried is counted and shown ("N configurations tested"); a deflated/penalised metric or minimum-trade
  threshold (default ≥ 100 out-of-sample trades) gates any "robust" label; below it the result is labelled *insufficient evidence*.
* **Data guards**: minimum days/bars per instrument, coverage report, refusal when calendar/corporate-action verification is missing.
* **Look-ahead tests (engine unit tests when built)**: (1) truncating all data after bar *t* must not change any decision before *t*; (2) shifting a higher-timeframe bar by
  one must change results only after it closes; (3) randomly permuting future bars must leave past trades identical; (4) a synthetic strategy that "buys the next bar's high" must be unable to profit.
* **Honest labelling**: results are historical statistics; no result is described as a forecast or as advice; "Decision support, not investment advice."

## 9. Execution assumptions summary (shown on every run)
Completed bars only · next-bar-open entries · adverse slippage · stop-first when ambiguity is unresolvable · 1m-resolution for collisions · volume-capped fills · mandatory
intraday square-off · statutory costs by dated profile · no interpolation of missing data · no overnight, FX or live-position concept.

## 10. Implementation sequence (next milestones)
1. Calendar 2024–2025 + shared aggregation service + missing indicators (ADX, Bollinger, MACD, SuperTrend, opening range, prior-day levels).
2. Schema, validation and strategy CRUD/versions + `/catalog` + `/validate` (+ unit tests of the schema).
3. Engine core (clock, evaluator, fills, accounting) with the look-ahead tests above.
4. Run persistence, analytics, API, then the builder UI and results UI.
5. IS/OOS, walk-forward and sensitivity tooling.
