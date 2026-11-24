"""Trade-list statistics (floats; research only). Starting balance is fixed per run, sizing does not compound."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

START_BALANCE = 1000.0


def month_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def day_of(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def monthly(trades: list[dict]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        out[month_of(t["entry_time"])] += t["net"]
    return dict(sorted(out.items()))


def summarize(trades: list[dict], months: list[str] | None = None) -> dict:
    n = len(trades)
    base = {"trades": n, "net": 0.0, "gross": 0.0, "fees": 0.0, "gst": 0.0, "funding": 0.0, "slippage": 0.0, "win_rate": None, "avg_win": None, "avg_loss": None, "profit_factor": None,
            "gross_bps_per_trade": None, "net_bps_per_trade": None, "max_drawdown": 0.0, "max_drawdown_pct_of_start": 0.0, "worst_day": 0.0, "sharpe_daily": None,
            "months": 0, "positive_months": 0, "positive_month_share": None, "monthly": {}, "liquidations": 0, "funding_missing": 0}
    if n == 0:
        return base
    net = np.array([t["net"] for t in trades]); gross = np.array([t["gross"] for t in trades]); notional = np.array([t["notional"] for t in trades])
    wins, losses = net[net > 0], net[net <= 0]
    equity = START_BALANCE + np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate([[START_BALANCE], equity]))[1:]
    dd = float((peak - equity).max())
    days: dict[str, float] = defaultdict(float)
    for t in trades:
        days[day_of(t["exit_time"])] += t["net"]
    d = np.array(list(days.values()))
    mon = monthly(trades)
    if months:
        mon = {m: mon.get(m, 0.0) for m in months}
    base.update({
        "net": float(net.sum()), "gross": float(gross.sum()), "fees": float(sum(t["fees"] for t in trades)), "gst": float(sum(t["gst"] for t in trades)),
        "funding": float(sum(t["funding"] for t in trades)), "slippage": float(sum(t["slippage"] for t in trades)),
        "win_rate": float((net > 0).mean()), "avg_win": float(wins.mean()) if len(wins) else 0.0, "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else None,
        "gross_bps_per_trade": float((gross / notional).mean() * 1e4), "net_bps_per_trade": float((net / notional).mean() * 1e4),
        "max_drawdown": dd, "max_drawdown_pct_of_start": dd / START_BALANCE * 100, "worst_day": float(d.min()),
        "sharpe_daily": float(d.mean() / d.std(ddof=1) * np.sqrt(365)) if len(d) > 2 and d.std(ddof=1) > 0 else None,
        "months": len(mon), "positive_months": int(sum(1 for v in mon.values() if v > 0)), "monthly": mon,
        "liquidations": sum(1 for t in trades if t["exit_reason"] == "liquidated_or_gap"), "funding_missing": int(sum(t["funding_missing"] for t in trades)),
    })
    base["positive_month_share"] = base["positive_months"] / base["months"] if base["months"] else None
    return base


def median_month_score(trades: list[dict], months: list[str]) -> float:
    mon = monthly(trades)
    return float(np.median([mon.get(m, 0.0) for m in months])) if months else float("-inf")
