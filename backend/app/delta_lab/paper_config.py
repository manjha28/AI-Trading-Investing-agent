"""Frozen paper-test parameters. The hash of this dictionary is written on every paper-trade row, so a row can always be traced to the exact rules that produced it.
Changing anything here changes the hash and (by the pre-registration) restarts the test."""
from __future__ import annotations

import hashlib
import json

FROZEN = {
    "R3": {"bars": "1h", "range_bars": 24, "rank_window_bars": 1440, "rank_max_pct": 0.10, "entry": "stop-entry both sides, valid 60 min", "stop": "other side of the range", "target_r": 3, "time_stop_h": 48},
    "R1_shadow": {"variant": "R1|pct=10|atr4h=2|hold=72h|N=2m"},
    "risk": {"leverage": 10, "risk_pct": 0.005, "wallet_usd": 1000, "in_use_mode": "strict", "mode": "paper"},
    "cost": {"taker_fee": 0.0005, "maker_fee": 0.0002, "gst": 0.18, "slippage_bps": 1.0, "latency_bars": 0},
}
PARAM_HASH = hashlib.sha256(json.dumps(FROZEN, sort_keys=True).encode()).hexdigest()[:12]

# Holdout reference for the checkpoints (round 2, maker/maker baseline costs, Binance signals, the last 25% of 2020-2026)
HOLDOUT = {"BTCUSD": {"net_bps_per_trade": 38.94, "trades": 101, "profit_factor": 1.54}, "ETHUSD": {"net_bps_per_trade": 51.71, "trades": 94, "profit_factor": 1.45}}
SYMBOLS = ("BTCUSD", "ETHUSD")
