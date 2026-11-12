"""Overfitting controls: in/out-of-sample, walk-forward with train-only optimisation, and parameter sensitivity.

Parameter choices are made on the TRAIN window only and then scored on data the choice never saw. A sensitivity grid is always run on the
in-sample window and labelled as such, so it is never presented as independent validation.
"""
from __future__ import annotations

import itertools
from typing import Any, Callable

import pandas as pd

from app.services.intraday_backtest import BacktestConfig, InstrumentRun
from app.services.intraday_strategy import StrategyDefinition, apply_overrides, tunable_parameters

MAX_GRID = 25


def slice_runs(runs: list[InstrumentRun], start: pd.Timestamp, end: pd.Timestamp) -> list[InstrumentRun]:
    out = []
    for r in runs:
        f = r.frame
        sub = f.loc[(f["time"] >= start) & (f["time"] < end)].reset_index(drop=True)
        out.append(InstrumentRun(r.instrument_id, r.symbol, r.profile, sub, r.result, r.quality))
    return out


def _windows(runs: list[InstrumentRun]) -> tuple[pd.Timestamp, pd.Timestamp]:
    lo = min(r.frame["time"].iloc[0] for r in runs if len(r.frame))
    hi = max(r.frame["time"].iloc[-1] for r in runs if len(r.frame)) + pd.Timedelta(minutes=1)
    return lo, hi


def _split_points(lo: pd.Timestamp, hi: pd.Timestamp, ratio: float) -> pd.Timestamp:
    cut = lo + (hi - lo) * ratio
    return cut.floor("D")


def _headline(res: dict[str, Any]) -> dict[str, Any]:
    m = res["metrics"]
    keys = ("total_trades", "win_rate", "net_profit", "return_pct", "profit_factor_net", "max_drawdown_pct", "expectancy_per_trade")
    return {k: m.get(k) for k in keys}


def rerun(defn: StrategyDefinition, runs: list[InstrumentRun], cfg: BacktestConfig, backtest: Callable) -> dict[str, Any]:
    """``backtest(defn, runs, cfg)`` evaluates signals itself on each run's own (windowed) frame, so no state leaks between windows."""
    return backtest(defn, [r for r in runs if len(r.frame)], cfg)


def in_out_of_sample(defn, runs, cfg, backtest, ratio: float = 0.7) -> dict[str, Any]:
    lo, hi = _windows(runs)
    cut = _split_points(lo, hi, ratio)
    ins = rerun(defn, slice_runs(runs, lo, cut), cfg, backtest)
    oos = rerun(defn, slice_runs(runs, cut, hi), cfg, backtest)
    return {"mode": "in_out_of_sample", "split_at": cut.isoformat(), "in_sample": _headline(ins), "out_of_sample": _headline(oos),
            "note": "The strategy parameters were fixed before this split; both windows use the same untouched definition. "
                    "Out-of-sample is only independent if you did not tune the strategy while looking at it."}


def _grid(defn: StrategyDefinition, keys: list[str] | None, steps: int) -> tuple[list[str], list[dict[str, float]]]:
    params = {p["key"]: p for p in tunable_parameters(defn) if p["kind"] in ("int", "float")}
    chosen = [k for k in (keys or list(params)[:3]) if k in params][:3]
    axes = []
    for k in chosen:
        p = params[k]
        v = p["value"]
        pts = {v}
        for j in range(1, steps + 1):
            for sign in (-1, 1):
                nv = v * (1 + sign * 0.25 * j)
                nv = int(round(nv)) if p["kind"] == "int" else round(nv, 4)
                if (p["min"] is None or nv >= p["min"]) and (p["max"] is None or nv <= p["max"]) and nv > 0:
                    pts.add(nv)
        axes.append(sorted(pts))
    combos = [dict(zip(chosen, c)) for c in itertools.product(*axes)] if chosen else []
    return chosen, combos[:MAX_GRID]


def sensitivity(defn, runs, cfg, backtest, keys: list[str] | None = None, steps: int = 1) -> dict[str, Any]:
    lo, hi = _windows(runs)
    cut = _split_points(lo, hi, 0.7)
    train = slice_runs(runs, lo, cut)
    chosen, combos = _grid(defn, keys, steps)
    rows = []
    for combo in combos:
        res = rerun(apply_overrides(defn, combo), train, cfg, backtest)
        rows.append({"params": combo, **_headline(res)})
    return {"mode": "sensitivity", "window": "in-sample only", "in_sample_end": cut.isoformat(), "parameters": chosen, "grid": rows,
            "note": "Computed on the in-sample window. A result that collapses when a parameter moves 25% is likely overfit."}


def walk_forward(defn, runs, cfg, backtest, train_days: int = 20, test_days: int = 5, keys: list[str] | None = None, metric: str = "net_profit") -> dict[str, Any]:
    lo, hi = _windows(runs)
    chosen, combos = _grid(defn, keys, 1)
    folds, oos_trades, oos_net = [], 0, 0.0
    t0 = lo.floor("D")
    while t0 + pd.Timedelta(days=train_days + test_days) <= hi + pd.Timedelta(days=1):
        t1, t2 = t0 + pd.Timedelta(days=train_days), t0 + pd.Timedelta(days=train_days + test_days)
        train, test = slice_runs(runs, t0, t1), slice_runs(runs, t1, t2)
        if not any(len(r.frame) for r in train) or not any(len(r.frame) for r in test):
            t0 += pd.Timedelta(days=test_days)
            continue
        best, best_score, best_head = {}, None, None
        for combo in combos or [{}]:
            d = apply_overrides(defn, combo) if combo else defn
            res = rerun(d, train, cfg, backtest)
            score = float(res["metrics"].get(metric) or 0)
            if best_score is None or score > best_score:
                best, best_score, best_head = combo, score, _headline(res)
        d = apply_overrides(defn, best) if best else defn
        test_res = rerun(d, test, cfg, backtest)
        head = _headline(test_res)
        oos_trades += int(head["total_trades"] or 0)
        oos_net += float(head["net_profit"] or 0)
        folds.append({"train": [t0.isoformat(), t1.isoformat()], "test": [t1.isoformat(), t2.isoformat()], "chosen_params": best,
                      "train_result": best_head, "test_result": head})
        t0 += pd.Timedelta(days=test_days)
    return {"mode": "walk_forward", "train_days": train_days, "test_days": test_days, "optimised_on": f"{metric} (train windows only)",
            "parameters": chosen, "folds": folds, "oos_total_trades": oos_trades, "oos_net_profit": round(oos_net, 2),
            "note": "Each test window uses parameters chosen only from the preceding train window."}
