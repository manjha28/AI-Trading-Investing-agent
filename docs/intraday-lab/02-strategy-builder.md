# 02 — Intraday Strategy Builder (DESIGN ONLY)

Separate module (`/intraday`), independent of Research scoring. A strategy is **data**, never code: a versioned JSON document
validated against a closed schema. No `eval`, no Python, no free-text expressions; every value is an enum, a number with bounds, or a
reference to a named indicator instance.

## 1. Definition schema (`schema_version: "intraday-strategy/1"`)

```
Strategy
  schema_version, name, description
  universe:   { exchange, instruments[] | watchlist_id, max_instruments }
  timeframes: { base: "1m|3m|5m|15m", higher: ["15m","1h"...] }   # base is the evaluation clock
  indicators: [ { id, type, timeframe, params, source } ]       # named instances, e.g. "ema_fast"
  entry:      { long?: ConditionGroup, short?: ConditionGroup }
  exits:      { stop_loss?, target?, trailing_stop?, indicator_exit?: ConditionGroup, time_exit?, session_square_off }
  sizing:     { method, ... }
  risk:       { daily_loss_limit, max_trades_per_day, max_open_positions, max_exposure_pct, max_loss_per_trade }
  session:    { start, end, no_entry_after, square_off_at, timezone, skip_first_minutes }
  rules:      { direction: "long|short|both", cooldown_bars, reentry: "none|after_exit|after_stop", one_position_per_instrument }
  costs_profile: "nse_equity_intraday/1"        # reference; costs are never inside the strategy
```

### ConditionGroup
`{ "op": "all"|"any", "items": [ Condition | ConditionGroup ] }` (max depth 4, max 20 leaf conditions).

### Condition (closed set)
| type | fields |
|---|---|
| `compare` | `left`, `cmp` ∈ `> >= < <= ==`, `right` (operands below) |
| `cross` | `left`, `direction` ∈ `above|below`, `right` — true only on the bar where the sign changes |
| `between` | `value`, `low`, `high` |
| `rising` / `falling` | `value`, `bars` (1–20) |
| `time_window` | `from`, `to` (session-local) |

**Operand** = `{indicator: "<name>", output?: "<series>"}` · `{field: "open|high|low|close|volume"}` · `{prior: {…operand}, bars: n}` ·
`{constant: number}` · `{expr: "mul|add|sub", args:[operand, operand]}` (only these three arithmetic ops, depth ≤ 2) ·
`{session: "prev_close|prev_high|prev_low|open_range_high|open_range_low"}`.
A higher-timeframe operand carries `timeframe`; it resolves to the **last completed** higher bar (see look-ahead rules in doc 03).

### Indicators (catalogue is generated from `indicator_library.INDICATORS`; new ones registered the same way)
Existing: SMA, EMA, RMA, WMA, VWMA, HMA, ZLEMA, RSI, ATR, session VWAP, volume expansion. To add before launch: ADX(+DI), Bollinger
(upper/mid/lower/%B/width), MACD, SuperTrend, opening range, previous-day levels, volume SMA. Each declares `minimum_bars`, params with bounds and
output series — the existing `Parameter(…bounds…)` structure is the validation source.

### Exits (simulation parameters only)
* `stop_loss`: `{type: "percent|atr|fixed_points", value}`; `target`: `{type: "percent|atr|r_multiple", value}`;
* `trailing_stop`: `{type: "percent|atr", value, activate_after_r?}` ratchets only in the favourable direction, updated on completed bars;
* `indicator_exit`: a ConditionGroup evaluated on completed bars → exits at the **next** bar open;
* `time_exit`: max bars in trade; `session_square_off`: **mandatory** (cannot be disabled) — every position is closed at `square_off_at`.

### Sizing and risk
`sizing.method` ∈ `fixed_quantity | fixed_notional | risk_per_trade_pct | percent_of_equity`; all clamp to integer lot/share quantity ≥ 0 (0 ⇒ skip,
recorded as `size_zero`). `risk.*` limits are hard gates: daily loss limit halts new entries for the rest of the session; trade count cap;
exposure cap. Money fields are strings parsed to `Decimal`.

## 2. Validation (backend is authoritative; the frontend only mirrors it for UX)
Layered, returning a list of `{path, code, message}`:
1. **Structural** — Pydantic discriminated unions, `extra="forbid"`, unknown types rejected.
2. **Referential** — every indicator/operand name exists; no cycles; higher timeframes are multiples of base and ≥ base.
3. **Bounds** — params within indicator bounds; stop must exist whenever `sizing.method = risk_per_trade_pct`.
4. **Semantic** — at least one entry group; exits include `session_square_off`; `cross` operands are numeric series; `direction` matches provided entry groups; no
   contradictory always-true/false constants.
5. **Data feasibility** (`/validate?instrument=`) — warm-up bars needed (`minimum_bars_for` × timeframe ratio) vs stored 1m coverage; reports the first
   date a backtest can start.
6. **Hash** — canonical JSON → SHA-256 `definition_hash`, so identical definitions are identical versions.

## 3. Persistence (additive migration, next number 0048)
* `intraday_strategies(id, owner_id, name, status, created_at, updated_at, archived_at)`.
* `intraday_strategy_versions(id, strategy_id, version_no, schema_version, definition JSONB, definition_hash, created_at, note)` — **immutable**; edit ⇒ new
  version; duplicate ⇒ new strategy whose v1 copies the definition; unique `(strategy_id, version_no)`.
* `backtest_runs` / `backtest_trades` / `backtest_equity` are specified in doc 03.
* Alembic head single, revision id ≤32 chars (`0048_intraday_strategies`); model modules imported in `app/models/__init__.py`. The existing daily
  `Strategy` tables are left untouched.

## 4. API contract (all routes: authenticated + email-verified, CSRF on mutations, owner-scoped)
```
GET    /api/v1/intraday/catalog                  indicators, condition types, timeframes, sessions, schema JSON
POST   /api/v1/intraday/strategies               create (validates; returns version 1)
GET    /api/v1/intraday/strategies               list;  GET /{id}  detail incl. latest version
POST   /api/v1/intraday/strategies/{id}/versions new immutable version
POST   /api/v1/intraday/strategies/{id}/duplicate
POST   /api/v1/intraday/validate                 body: definition [+instrument_id] → issues[], warm-up, earliest start
POST   /api/v1/intraday/strategies/{id}/backtests  body: version_no, instruments, window, cost profile, assumptions → 202 run id
GET    /api/v1/intraday/backtests/{run}          status, summary, assumptions, data coverage
GET    /api/v1/intraday/backtests/{run}/trades | /equity | /diagnostics
```
No route submits orders, and none exists for "activate"/"run live".

## 5. Frontend / backend responsibilities
Backend: schema, validation, indicator math, evaluation, persistence, all numbers. Frontend (`pages/Intraday*`, reusing `CandleChart`, watchlist
picker, `formatMoney`): a **form-driven builder** — (1) Universe & session, (2) Indicators (picker generated from `/catalog`), (3) Entry rules as a
nested all/any list of dropdown-built conditions with a plain-English preview ("EMA 9 crosses above EMA 21 AND RSI(14) > 55"), (4) Exits & risk,
(5) Validate & save. A read-only JSON view is shown for transparency; free-form JSON editing is an "advanced" import that still goes through
`/validate`. The UI never computes indicators or results; chart overlays and trade markers come from the API.

## 6. Representative definitions

### A. EMA trend pullback, long only (5m with 15m trend filter)
```json
{
  "schema_version": "intraday-strategy/1",
  "name": "EMA trend pullback",
  "universe": {"exchange": "NSE", "watchlist_id": null, "instruments": ["SBIN", "RELIANCE"], "max_instruments": 10},
  "timeframes": {"base": "5m", "higher": ["15m"]},
  "indicators": [
    {"id": "ema_fast", "type": "ema", "timeframe": "5m", "params": {"length": 9}},
    {"id": "ema_slow", "type": "ema", "timeframe": "5m", "params": {"length": 21}},
    {"id": "ema_trend", "type": "ema", "timeframe": "15m", "params": {"length": 50}},
    {"id": "rsi", "type": "rsi", "timeframe": "5m", "params": {"period": 14}},
    {"id": "atr", "type": "atr", "timeframe": "5m", "params": {"period": 14}}
  ],
  "entry": {"long": {"op": "all", "items": [
    {"type": "cross", "left": {"indicator": "ema_fast"}, "direction": "above", "right": {"indicator": "ema_slow"}},
    {"type": "compare", "left": {"field": "close", "timeframe": "15m"}, "cmp": ">", "right": {"indicator": "ema_trend"}},
    {"type": "between", "value": {"indicator": "rsi"}, "low": {"constant": 50}, "high": {"constant": 70}}
  ]}},
  "exits": {
    "stop_loss": {"type": "atr", "value": "1.5"},
    "target": {"type": "r_multiple", "value": "2"},
    "trailing_stop": {"type": "atr", "value": "1.5", "activate_after_r": "1"},
    "indicator_exit": {"op": "all", "items": [{"type": "cross", "left": {"indicator": "ema_fast"}, "direction": "below", "right": {"indicator": "ema_slow"}}]},
    "session_square_off": true
  },
  "sizing": {"method": "risk_per_trade_pct", "risk_pct": "0.5", "starting_equity": "500000"},
  "risk": {"daily_loss_limit": "10000", "max_trades_per_day": 4, "max_open_positions": 2, "max_exposure_pct": "40", "max_loss_per_trade": "2500"},
  "session": {"start": "09:15", "end": "15:30", "skip_first_minutes": 5, "no_entry_after": "14:45", "square_off_at": "15:15", "timezone": "Asia/Kolkata"},
  "rules": {"direction": "long", "cooldown_bars": 3, "reentry": "after_exit", "one_position_per_instrument": true},
  "costs_profile": "nse_equity_intraday/1"
}
```

### B. VWAP mean reversion, long and short (1m base)
```json
{
  "schema_version": "intraday-strategy/1",
  "name": "VWAP reversion",
  "universe": {"exchange": "NSE", "instruments": ["SBIN"], "max_instruments": 1},
  "timeframes": {"base": "1m", "higher": []},
  "indicators": [
    {"id": "vwap", "type": "session_vwap", "timeframe": "1m", "params": {}},
    {"id": "bb", "type": "bollinger", "timeframe": "1m", "params": {"length": 20, "stdev": "2"}},
    {"id": "vol", "type": "volume_expansion", "timeframe": "1m", "params": {"lookback": 5}}
  ],
  "entry": {
    "long":  {"op": "all", "items": [{"type": "compare", "left": {"field": "close"}, "cmp": "<", "right": {"indicator": "bb", "output": "lower"}},
                                     {"type": "compare", "left": {"indicator": "vol"}, "cmp": ">", "right": {"constant": 1.5}}]},
    "short": {"op": "all", "items": [{"type": "compare", "left": {"field": "close"}, "cmp": ">", "right": {"indicator": "bb", "output": "upper"}},
                                     {"type": "compare", "left": {"indicator": "vol"}, "cmp": ">", "right": {"constant": 1.5}}]}
  },
  "exits": {"stop_loss": {"type": "percent", "value": "0.4"}, "target": {"type": "percent", "value": "0.6"}, "time_exit": {"max_bars": 30}, "session_square_off": true},
  "sizing": {"method": "fixed_quantity", "quantity": 100},
  "risk": {"daily_loss_limit": "5000", "max_trades_per_day": 6, "max_open_positions": 1, "max_exposure_pct": "25", "max_loss_per_trade": "1000"},
  "session": {"start": "09:15", "end": "15:30", "skip_first_minutes": 15, "no_entry_after": "14:30", "square_off_at": "15:15", "timezone": "Asia/Kolkata"},
  "rules": {"direction": "both", "cooldown_bars": 5, "reentry": "none", "one_position_per_instrument": true},
  "costs_profile": "nse_equity_intraday/1"
}
```
(Short selling is intraday-only and subject to the engine's constraints in doc 03.)

### C. Opening-range breakout with ADX filter (5m)
```json
{
  "schema_version": "intraday-strategy/1",
  "name": "Opening range breakout",
  "universe": {"exchange": "NSE", "instruments": ["RELIANCE"], "max_instruments": 1},
  "timeframes": {"base": "5m", "higher": []},
  "indicators": [{"id": "adx", "type": "adx", "timeframe": "5m", "params": {"period": 14}}],
  "entry": {
    "long":  {"op": "all", "items": [{"type": "compare", "left": {"field": "close"}, "cmp": ">", "right": {"session": "open_range_high"}},
                                     {"type": "compare", "left": {"indicator": "adx"}, "cmp": ">=", "right": {"constant": 20}},
                                     {"type": "time_window", "from": "09:45", "to": "13:00"}]},
    "short": {"op": "all", "items": [{"type": "compare", "left": {"field": "close"}, "cmp": "<", "right": {"session": "open_range_low"}},
                                     {"type": "compare", "left": {"indicator": "adx"}, "cmp": ">=", "right": {"constant": 20}},
                                     {"type": "time_window", "from": "09:45", "to": "13:00"}]}
  },
  "exits": {"stop_loss": {"type": "percent", "value": "0.5"}, "target": {"type": "r_multiple", "value": "1.5"}, "session_square_off": true},
  "sizing": {"method": "fixed_notional", "notional": "100000"},
  "risk": {"daily_loss_limit": "4000", "max_trades_per_day": 2, "max_open_positions": 1, "max_exposure_pct": "30", "max_loss_per_trade": "1500"},
  "session": {"start": "09:15", "end": "15:30", "skip_first_minutes": 0, "no_entry_after": "13:00", "square_off_at": "15:15", "timezone": "Asia/Kolkata", "opening_range_minutes": 30},
  "rules": {"direction": "both", "cooldown_bars": 0, "reentry": "none", "one_position_per_instrument": true},
  "costs_profile": "nse_equity_intraday/1"
}
```

## 7. Execution interface (internal, pure)
```python
class StrategyEvaluator(Protocol):
    def warmup_bars(self, definition) -> dict[str, int]: ...
    def on_bar(self, state: EvaluatorState, bar: CompletedBar) -> list[SignalIntent]: ...   # no I/O, no clock, no broker
```
`SignalIntent{instrument, side, reason, bar_ts}` is a *statement about the past* consumed by the backtest simulator. There is no order object,
adapter or submit path in this module.

## 8. Open questions for the owner
Confirm (a) the stop/target/trailing reading in README scope guardrails; (b) cost profile fields (brokerage plan); (c) whether shorting is wanted at all (NSE
cash intraday shorting is allowed but must square off same day; delivery shorting is not).
