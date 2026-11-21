"""Walk-forward replay of the technical engine's ENTER_NOW signals on stored daily candles (hypothetical results; never mixed with real P&L).

At each decision bar t the engine sees ONLY candles up to t (weekly/monthly candles are built from those and exclude the forming period), so there is no
look-ahead. A signal is entered at the NEXT bar's open (skipped as "missed" if it gaps beyond the entry zone by more than half an ATR). The stop is
checked before the target inside a bar (conservative), gap-downs fill at the open, and the exit at the end of the holding window is the close. Costs are a flat
round-trip percentage. NO parameters are fitted here: the engine runs with its stated defaults, and results are reported separately for the first
(in-sample) and later (out-of-sample) part of the history. Survivorship: only stocks that still trade are in the store; the report says so.

Run from backend/:  python -m app.services.technical_backtest --symbols RELIANCE,HDFCBANK --horizon swing   (prints engine vs previous-rule, out-of-sample)
"""
from __future__ import annotations

import argparse
from typing import Any

import pandas as pd

from app.services import technical_engine as te

MAX_HOLD_BARS = {"swing": 20, "positional": 60, "long_term": 250}
TARGET_INDEX = {"swing": 0, "positional": 1, "long_term": 1}     # T1 / T2 / T2
WARMUP_BARS = {"swing": 260, "positional": 700, "long_term": 1100}


# The previous (baseline) level rule, kept ONLY so the engine can be compared against it on identical data and timing:
# daily bars for every horizon, stop = lowest low of N bars - buffer x ATR, entry near the last close, targets at fixed R multiples.
BASELINE = {"swing": (10, 0.25, 3.0, (1.5, 2.5)), "positional": (20, 0.5, 4.0, (2.0, 3.5)), "long_term": (60, 0.5, 6.0, (3.0, 5.0))}


def _baseline_plan(hist: pd.DataFrame, horizon: str) -> dict[str, Any] | None:
    lookback, buffer, max_risk, mult = BASELINE[horizon]
    a = te.atr(hist, 14).iloc[-1]
    if pd.isna(a) or a <= 0 or len(hist) < 60:
        return None
    close = float(hist["close"].iloc[-1])
    stop = float(hist["low"].iloc[-lookback:].min()) - buffer * a
    risk = close - stop
    if risk <= 0 or risk > max_risk * a:
        return None
    return {"stop": stop, "targets": [close + m * risk for m in mult], "zone_high": close + 0.25 * a, "atr": float(a)}


def _metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    r = [t["r"] for t in trades]
    wins, losses = [x for x in r if x > 0], [x for x in r if x <= 0]
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in r:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    by_regime: dict[str, dict[str, Any]] = {}
    for t in trades:
        g = by_regime.setdefault(t["regime"], {"trades": 0, "sum_r": 0.0})
        g["trades"] += 1
        g["sum_r"] = round(g["sum_r"] + t["r"], 3)
    return {"trades": len(r), "win_rate": round(len(wins) / len(r), 3), "profit_factor": None if not losses or sum(losses) == 0 else round(sum(wins) / -sum(losses), 2),
            "expectancy_r": round(sum(r) / len(r), 3), "avg_win_r": round(sum(wins) / len(wins), 2) if wins else None, "avg_loss_r": round(sum(losses) / len(losses), 2) if losses else None,
            "max_drawdown_r": round(dd, 2), "stop_rate": round(sum(t["exit"] == "stop" for t in trades) / len(r), 3), "target_rate": round(sum(t["exit"] == "target" for t in trades) / len(r), 3),
            "false_breakout_rate": None if not any(t["setup"] == "BREAKOUT_ENTRY" for t in trades) else round(
                sum(1 for t in trades if t["setup"] == "BREAKOUT_ENTRY" and t["exit"] == "stop" and t["bars"] <= 5) / sum(t["setup"] == "BREAKOUT_ENTRY" for t in trades), 3),
            "by_regime": by_regime}


def backtest(df: pd.DataFrame, horizon: str, cfg: te.EngineConfig | None = None, bench: pd.Series | None = None, *, step: int = 5, cost_pct: float = 0.2,
             split: float = 0.6, warmup: int | None = None, mode: str = "engine") -> dict[str, Any]:
    cfg = cfg or te.EngineConfig()
    warmup = warmup if warmup is not None else WARMUP_BARS[horizon]
    trades: list[dict[str, Any]] = []
    missed = 0
    t = warmup
    n = len(df)
    while t < n - 2:
        hist = df.iloc[: t + 1]
        asof = hist.index[-1]
        res = te.analyze_horizon(horizon, hist, None if bench is None else bench[bench.index <= asof], cfg, asof)
        bullish = res.get("status") == "available" and res["direction"] in ("BULLISH", "STRONG_BULLISH")   # the same direction gate for both modes
        if mode == "engine":
            ok = bullish and res["entry_status"] == "ENTER_NOW" and bool(res.get("targets"))
            plan = None if not ok else {"stop": float(res["stop_loss"]["price"]), "targets": [float(x["price"]) for x in res["targets"]], "zone_high": float(res["entry"]["high"]), "atr": res["atr"]["atr14"]}
            setup = res.get("selected_setup")
        else:
            plan = _baseline_plan(hist, horizon) if bullish else None
            setup = "BASELINE_LEVELS"
        if plan is None:
            t += step
            continue
        zone_high, stop, atr = plan["zone_high"], plan["stop"], plan["atr"]
        nxt = df.iloc[t + 1]
        entry = float(nxt["open"])
        if entry > zone_high + 0.5 * atr or entry <= stop:
            missed += 1
            t += step
            continue
        target = plan["targets"][min(TARGET_INDEX[horizon], len(plan["targets"]) - 1)]
        risk = entry - stop
        exit_price, exit_kind, held = float(df.iloc[min(t + 1 + MAX_HOLD_BARS[horizon], n - 1)]["close"]), "time", MAX_HOLD_BARS[horizon]
        for j in range(t + 1, min(t + 1 + MAX_HOLD_BARS[horizon], n - 1) + 1):
            bar = df.iloc[j]
            if bar["low"] <= stop:
                exit_price, exit_kind, held = min(float(bar["open"]), stop), "stop", j - t
                break
            if bar["high"] >= target:
                exit_price, exit_kind, held = target, "target", j - t
                break
        r = (exit_price - entry) / risk - (cost_pct / 100 * entry) / risk
        trades.append({"date": asof.date().isoformat(), "setup": setup, "regime": res["market_regime"], "r": round(r, 3), "exit": exit_kind, "bars": held})
        t += max(step, held)
    cut = df.index[int(len(df) * split)] if len(df) else None
    ins = [x for x in trades if cut is not None and pd.Timestamp(x["date"]) < cut]
    oos = [x for x in trades if cut is not None and pd.Timestamp(x["date"]) >= cut]
    return {"label": "Hypothetical backtest on stored candles. Not real P&L, never an order or a position.", "horizon": horizon, "mode": mode, "bars": n, "missed_gap_entries": missed,
            "cost_round_trip_pct": cost_pct, "split_date": None if cut is None else cut.date().isoformat(), "all": _metrics(trades), "in_sample": _metrics(ins), "out_of_sample": _metrics(oos),
            "notes": ["No parameters were tuned on this data; the engine ran with its defaults.", "Only stocks that still trade are stored, so results carry survivorship bias.",
                      "Signals are entered at the next bar's open; stops are checked before targets within a bar."], "trades": trades[-200:]}


def compare(df: pd.DataFrame, horizon: str, cfg: te.EngineConfig | None = None, bench: pd.Series | None = None, **kw: Any) -> dict[str, Any]:
    """Engine versus the previous level rule on the same candles, direction gate, costs and exit rules. Out-of-sample numbers are the ones to read."""
    eng, base = backtest(df, horizon, cfg, bench, mode="engine", **kw), backtest(df, horizon, cfg, bench, mode="baseline", **kw)
    pick = lambda r, k: r["out_of_sample"].get(k)  # noqa: E731
    return {"horizon": horizon, "engine": eng, "baseline": base,
            "out_of_sample_summary": {k: {"engine": pick(eng, k), "baseline": pick(base, k)} for k in ("trades", "win_rate", "expectancy_r", "profit_factor", "max_drawdown_r", "stop_rate")},
            "note": "Both modes use the same engine direction gate, next-open entries, stop-before-target rule and costs; only entry timing, stop and targets differ."}


def main() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models import Instrument
    from app.services import research_pipeline as rp

    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--horizon", choices=te.HORIZONS, default="swing")
    parser.add_argument("--step", type=int, default=5)
    args = parser.parse_args()
    with Session(engine) as db:
        bench = rp._benchmark_series(db)
        for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
            inst = db.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.is_synthetic.is_(False)).limit(1))
            frame = rp._daily_frame(db, inst.id, None) if inst else None
            if frame is None:
                print(symbol, "no stored daily candles")
                continue
            res = compare(frame, args.horizon, bench=bench, step=args.step)
            print(symbol, args.horizon, res["out_of_sample_summary"])


if __name__ == "__main__":
    main()
