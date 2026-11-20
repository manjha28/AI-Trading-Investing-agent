"""Shared statistical reporting for backtest results (both engines). Net P&L is primary; every figure carries its sample context.

Mean per trade, standard error (sample sd / sqrt(n)), t-statistic and a 95% CI use Student's t. Below ``MIN_TRADES`` the performance
claim is SUPPRESSED (explicit 'insufficient_sample' state); the raw numbers may still be returned under ``diagnostics_only`` for audit,
clearly labelled as not a performance claim."""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable

MIN_TRADES = 100

# two-sided 95% critical values of Student's t (0.975 quantile) by degrees of freedom
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
         16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
         40: 2.021, 60: 2.000, 80: 1.990, 100: 1.984, 120: 1.980}


def t_crit_975(df: int) -> float:
    if df < 1:
        return float("nan")
    if df in _T975:
        return _T975[df]
    keys = sorted(_T975)
    if df > keys[-1]:
        return 1.960
    lo = max(k for k in keys if k < df)
    hi = min(k for k in keys if k > df)
    return _T975[lo] + (_T975[hi] - _T975[lo]) * (df - lo) / (hi - lo)


def mean_stats(values: Iterable[Decimal | float]) -> dict:
    xs = [float(v) for v in values]
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "se": None, "t_stat": None, "ci95": None}
    mean = sum(xs) / n
    if n < 2:
        return {"n": n, "mean": mean, "se": None, "t_stat": None, "ci95": None}
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    se = sd / math.sqrt(n)
    tcrit = t_crit_975(n - 1)
    return {"n": n, "mean": mean, "sd": sd, "se": se, "t_stat": (mean / se) if se > 0 else None, "ci95": [mean - tcrit * se, mean + tcrit * se], "t_critical": tcrit}


def sample_status(n: int) -> dict:
    ok = n >= MIN_TRADES
    return {"trades": n, "min_trades_for_performance_claims": MIN_TRADES, "sufficient": ok,
            "state": "ok" if ok else "insufficient_sample",
            "message": None if ok else f"Insufficient sample: {n} trades < {MIN_TRADES}. No performance claim is made; any figures shown are diagnostics only."}


def net_gross_block(trades: list[dict], initial_capital: Decimal, n_configs: int = 1) -> dict:
    """Full gross/net report block with statistics. ``trades`` need gross_pnl, net_pnl, charges, turnover, r_multiple, entry_ts, exit_ts."""
    n = len(trades)
    status = sample_status(n)
    nets = [t["net_pnl"] for t in trades]
    grosses = [t["gross_pnl"] for t in trades]

    def pf(xs: list) -> float | None:
        gp = sum((x for x in xs if x > 0), Decimal(0))
        gl = -sum((x for x in xs if x <= 0), Decimal(0))
        return float(gp / gl) if gl > 0 else None

    eq, peak, mdd = initial_capital, initial_capital, Decimal(0)
    for x in [t["net_pnl"] for t in sorted(trades, key=lambda t: t["exit_ts"])]:
        eq += x
        peak = max(peak, eq)
        mdd = max(mdd, peak - eq)
    holds = [(t["exit_ts"] - t["entry_ts"]).total_seconds() / 60 for t in trades if t.get("exit_ts")]
    total_charges = sum((t["charges"] for t in trades), Decimal(0))
    block = {
        "trades": n, "wins_net": sum(1 for x in nets if x > 0), "win_rate_net_pct": (round(sum(1 for x in nets if x > 0) / n * 100, 2) if n else None),
        "gross_pnl": str(sum(grosses, Decimal(0))), "total_charges": str(total_charges), "net_pnl": str(sum(nets, Decimal(0))),
        "gross_profit_factor": pf(grosses), "net_profit_factor": pf(nets),
        "gross_expectancy_per_trade": str((sum(grosses, Decimal(0)) / n).quantize(Decimal("0.01"))) if n else None,
        "net_expectancy_per_trade": str((sum(nets, Decimal(0)) / n).quantize(Decimal("0.01"))) if n else None,
        "total_turnover": str(sum((t.get("turnover", Decimal(0)) for t in trades), Decimal(0))),
        "total_round_trip_cost": str(total_charges), "max_drawdown_net": str(mdd), "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else None,
        "charge_components": {k: str(sum((t.get(k, Decimal(0)) for t in trades), Decimal(0))) for k in ("brokerage", "exchange_charge", "ipft", "sebi", "stt", "stamp_duty", "gst", "other_charges")},
        "net_per_trade_stats": mean_stats(nets), "gross_per_trade_stats": mean_stats(grosses), "net_r_stats": mean_stats([t["r_multiple"] for t in trades]),
        "configurations_evaluated": n_configs, "sample": status, "headline": "net",
    }
    if not status["sufficient"]:
        diag = {k: block[k] for k in list(block) if k not in ("sample", "headline", "configurations_evaluated", "trades")}
        return {"trades": n, "sample": status, "configurations_evaluated": n_configs, "headline": "net", "performance": "suppressed (insufficient_sample)", "diagnostics_only": diag}
    return block
