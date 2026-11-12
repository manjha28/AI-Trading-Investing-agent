# Intraday Strategy Lab — architecture (DESIGN ONLY)

Status: **specifications. Nothing in this directory is implemented.** Implemented today: the daily-bar strategy/backtest
stack described in `01-infrastructure-audit.md`.

| Doc | Content |
|---|---|
| `01-infrastructure-audit.md` | Verified capability matrix of existing market-data, candle, indicator, strategy and backtest code, with measured data quality |
| `02-strategy-builder.md` | Versioned strategy-definition schema, validation, persistence, API contracts, no-code builder, JSON examples |
| `03-backtest-engine.md` | Event-driven backtester design, execution assumptions, analytics and anti-overfitting protocol |

## Scope guardrails (AGENTS.md)
* Backtesting over stored bars is in scope. **Paper trading, live execution, order adapters and simulated live positions are
  permanently out of scope** and are not designed here. Earlier wording about "the same definitions used by paper trading" is
  therefore *not* adopted: the strategy definition is a pure description of signals and simulated-exit rules whose only consumer is
  the historical evaluator (and, separately, read-only signal display).
* Stops, targets and trailing stops appear only as **parameters of a historical simulation** (the existing daily backtester already
  does this). AGENTS.md also lists "stop-losses, targets, trailing stops" among excluded *execution* features; the owner should
  confirm this reading before the build milestone. Nothing here can place or route an order.
* Intraday is a separate module; no Research horizon, weight or score is added.
* Money is Decimal; indicators may use float/pandas internally and are converted at the boundary (AGENTS.md).
