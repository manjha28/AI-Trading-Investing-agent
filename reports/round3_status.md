# Round 3 status: R3 paper test

*Generated 2026-11-24 07:51 UTC. Paper trades only; no order is ever sent. Decision support, not investment advice.*

Frozen parameter hash `eaea623c741f` (on every paper row). Started 2026-11-24 07:48 UTC.

## Health
- Order-book recorder: running; uptime 100.0% over 24 h; 0 gap events; reconnects 0; last snapshot 2026-11-24 07:51:19 UTC.
- Paper engine heartbeat: 1 s ago.

## Paper trades
- Closed R3 trades: 0 combined (0 BTCUSD, 0 ETHUSD). Open positions: 0.
- R1 shadow trades (logged only, no decisions): 0.

## Pre-registered checkpoints
- Holdout reference: BTCUSD 38.94 and ETHUSD 51.71 net bps/trade.
- **Week 4 execution check** (not due yet): signals 0, measured against the recorded book 0; median taker slippage —, 90th percentile —. no paper trades yet
- 30-trade checkpoint: 0 of 30 so far.
- **Go/no-go at 50 trades**: 0 of 50 combined trades so far.

## Risk-gate rejections so far (paper)
none
Reconciliation: not applicable in paper (no exchange state); a mismatch can only occur once orders exist.

## Re-costing with measured slippage
Eligible after 2 weeks of recorder data (recording since 2026-11-24): run `python -m app.delta_lab.measure_costs` for the table and then the single holdout re-run.

## Frozen rules
```
{
 "R3": {
  "bars": "1h",
  "range_bars": 24,
  "rank_window_bars": 1440,
  "rank_max_pct": 0.1,
  "entry": "stop-entry both sides, valid 60 min",
  "stop": "other side of the range",
  "target_r": 3,
  "time_stop_h": 48
 },
 "R1_shadow": {
  "variant": "R1|pct=10|atr4h=2|hold=72h|N=2m"
 },
 "risk": {
  "leverage": 10,
  "risk_pct": 0.005,
  "wallet_usd": 1000,
  "in_use_mode": "strict",
  "mode": "paper"
 },
 "cost": {
  "taker_fee": 0.0005,
  "maker_fee": 0.0002,
  "gst": 0.18,
  "slippage_bps": 1.0,
  "latency_bars": 0
 }
}
```
