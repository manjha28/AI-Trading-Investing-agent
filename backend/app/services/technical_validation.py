"""Out-of-sample validation of the technical engine on stored real daily candles (hypothetical results; never real P&L).

Answers three questions with data instead of opinion:
  1. ENTRY ZONES: do 'enter now' signals and limit orders placed in the planned zone produce favourable outcomes (R multiples after costs)?
  2. SCORE CALIBRATION: does a higher technical score really precede better forward returns (bucketed, out-of-sample, with a rank correlation)?
  3. WAIT: does 'wait for entry' avoid poor entries without missing too many good ones (versus entering immediately)?

Method: at every Nth completed daily bar the engine sees ONLY candles up to that bar (weekly/monthly candles are built from them and exclude the
forming period), so there is no look-ahead. Forward outcomes are measured on later candles. The first 60% of dates are in-sample (reported, never
used to tune anything: no parameter was fitted here) and the last 40% are out-of-sample; conclusions use out-of-sample numbers. Costs: flat round-trip
percentage. Survivorship: only stocks that still trade are stored, so results are optimistic by that bias.

Run from backend/:  python -m app.services.technical_validation --horizon swing --stocks 60 --step 7
"""
from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

import numpy as np
import pandas as pd

from app.services import technical_engine as te

FWD_BARS = {"swing": 10, "positional": 30, "long_term": 90}       # forward window for return / calibration measures
LIMIT_WINDOW = {"swing": 10, "positional": 30, "long_term": 60}   # how long a resting limit order in the entry zone stays valid
TARGET_INDEX = {"swing": 0, "positional": 1, "long_term": 1}
WARMUP = {"swing": 260, "positional": 700, "long_term": 1100}
SCORE_BINS = [(0, 45), (45, 65), (65, 80), (80, 101)]


def _simulate(df: pd.DataFrame, i0: int, entry: float, stop: float, target: float, max_hold: int, cost_pct: float) -> dict[str, Any]:
    """Enter at ``entry`` on bar i0 (already filled), stop checked before target inside a bar, gap-downs fill at the open; exit at the window end at the close."""
    risk = entry - stop
    n = len(df)
    last = min(i0 + max_hold, n - 1)
    exit_price, kind, held = float(df["close"].iloc[last]), "time", last - i0
    mfe = mae = 0.0
    for j in range(i0, last + 1):
        bar = df.iloc[j]
        mfe, mae = max(mfe, float(bar["high"]) - entry), max(mae, entry - float(bar["low"]))
        if j == i0 and bar["open"] > entry:         # filled mid-bar: only the low/high after the fill matter; keep it simple and conservative
            pass
        if bar["low"] <= stop:
            exit_price, kind, held = min(float(bar["open"]) if j > i0 else stop, stop), "stop", j - i0
            break
        if bar["high"] >= target:
            exit_price, kind, held = target, "target", j - i0
            break
    r = (exit_price - entry) / risk - (cost_pct / 100 * entry) / risk
    return {"r": r, "exit": kind, "bars": held, "mfe_r": mfe / risk, "mae_r": mae / risk}


def evaluate_symbol(args: tuple[str, str, int, int, float]) -> list[dict[str, Any]]:
    """Replay one stock. Runs in a worker process (own DB session)."""
    symbol, horizon, step, warmup, cost_pct = args
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.db import engine
    from app.models import Instrument
    from app.services import research_pipeline as rp

    with Session(engine) as db:
        inst = db.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.is_synthetic.is_(False)).limit(1))
        df = rp._daily_frame(db, inst.id, "dhan") if inst else None
    if df is None or len(df) < warmup + FWD_BARS[horizon] + 30:
        return []
    cfg = te.EngineConfig()
    out: list[dict[str, Any]] = []
    fwd, win = FWD_BARS[horizon], LIMIT_WINDOW[horizon]
    for t in range(warmup, len(df) - fwd - 2, step):
        hist = df.iloc[: t + 1]
        asof = hist.index[-1]
        res = te.analyze_horizon(horizon, hist, None, cfg, asof)
        if res.get("status") != "available":
            continue
        close = float(df["close"].iloc[t])
        nxt_open = float(df["open"].iloc[t + 1])
        atr = res["atr"]["atr14_daily"] or res["atr"]["atr14"]
        row: dict[str, Any] = {"symbol": symbol, "date": asof.date().isoformat(), "score": res["technical_score"], "direction": res["direction"], "regime": res["market_regime"],
                               "setup": res["selected_setup"], "status": res["entry_status"], "watch_only": bool(res.get("watch_only")),
                               "fwd_ret_pct": (float(df["close"].iloc[t + fwd]) / nxt_open - 1) * 100, "fwd_atr": (float(df["close"].iloc[t + fwd]) - nxt_open) / atr}
        if res.get("entry") and res.get("stop_loss") and res.get("targets"):
            stop = float(res["stop_loss"]["price"])
            target = float(res["targets"][min(TARGET_INDEX[horizon], len(res["targets"]) - 1)]["price"])
            planned, zone_high = float(res["entry"]["planned_price"]), float(res["entry"]["high"])
            row["rr_t1"] = res["targets"][0]["risk_reward"]
            max_hold = {"swing": 20, "positional": 60, "long_term": 250}[horizon]
            # (a) enter at the next open (what 'enter now' means; what 'wait' would have done if you ignored it)
            if nxt_open > stop and nxt_open - stop > 0:
                row["immediate"] = _simulate(df, t + 1, nxt_open, stop, target, max_hold, cost_pct)
            # (b) a resting limit order at the planned price for the window (what 'wait for entry' asks you to do)
            if planned > stop:
                for j in range(t + 1, min(t + 1 + win, len(df) - 2)):
                    bar = df.iloc[j]
                    if bar["low"] <= stop:
                        row["limit"] = {"filled": False, "stopped_before_fill": True}
                        break
                    if bar["low"] <= planned:
                        fill = min(planned, float(bar["open"]))
                        row["limit"] = {"filled": True, **_simulate(df, j, fill, stop, target, max_hold, cost_pct)}
                        break
                else:
                    ran_to_target = float(df["high"].iloc[t + 1: t + 1 + win].max()) >= target
                    row["limit"] = {"filled": False, "ran_to_t1_unfilled": ran_to_target}
        out.append(row)
    return out


def _stats(rs: list[float]) -> dict[str, Any]:
    if not rs:
        return {"n": 0}
    a = np.array(rs)
    wins, losses = a[a > 0], a[a <= 0]
    return {"n": len(a), "mean_r": round(float(a.mean()), 3), "win_rate": round(float((a > 0).mean()), 3),
            "profit_factor": None if losses.sum() == 0 else round(float(wins.sum() / -losses.sum()), 2), "median_r": round(float(np.median(a)), 3)}


def summarise(rows: list[dict[str, Any]], horizon: str) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    if df.empty:
        return {"error": "no decisions"}
    cut = sorted(df["date"])[int(len(df) * 0.6)]
    df["sample"] = np.where(df["date"] < cut, "in_sample", "out_of_sample")
    res: dict[str, Any] = {"horizon": horizon, "decisions": len(df), "stocks": int(df["symbol"].nunique()), "split_date": cut, "label": "Hypothetical. Never real P&L, an order or a position."}
    base = df["fwd_ret_pct"].mean()
    for sample in ("in_sample", "out_of_sample"):
        part = df[df["sample"] == sample]
        # 2. score calibration
        bins = []
        for lo, hi in SCORE_BINS:
            b = part[(part["score"] >= lo) & (part["score"] < hi)]
            bins.append({"score_range": f"{lo}-{min(hi, 100)}", "n": len(b), "mean_fwd_ret_pct": None if b.empty else round(float(b["fwd_ret_pct"].mean()), 3),
                         "hit_rate": None if b.empty else round(float((b["fwd_ret_pct"] > 0).mean()), 3), "mean_fwd_atr": None if b.empty else round(float(b["fwd_atr"].mean()), 3)})
        rho = None
        if len(part) > 30:
            rho = round(float(part["score"].rank().corr(part["fwd_ret_pct"].rank())), 4)
        # 1. entry zones
        now_rows = part[(part["status"] == "ENTER_NOW")]
        r_now = [x["r"] for x in now_rows["immediate"].dropna()] if "immediate" in now_rows else []
        wait_rows = part[(part["status"] == "WAIT_FOR_ENTRY") & (~part["watch_only"])]
        r_wait_imm = [x["r"] for x in wait_rows["immediate"].dropna()] if "immediate" in wait_rows else []
        limits = [x for x in wait_rows["limit"].dropna()] if "limit" in wait_rows else []
        filled = [x for x in limits if x.get("filled")]
        unfilled = [x for x in limits if not x.get("filled")]
        res[sample] = {
            "unconditional_mean_fwd_ret_pct": round(float(part["fwd_ret_pct"].mean()), 3) if len(part) else None,
            "score_calibration": {"bins": bins, "spearman_rank_corr_score_vs_fwd_return": rho},
            "enter_now_entries": _stats(r_now),
            "wait_for_entry": {
                "decisions": len(wait_rows), "if_entered_immediately": _stats(r_wait_imm), "limit_order_filled": _stats([x["r"] for x in filled]),
                "fill_rate": None if not limits else round(len(filled) / len(limits), 3),
                "unfilled_then_ran_to_first_target_pct": None if not unfilled else round(sum(1 for x in unfilled if x.get("ran_to_t1_unfilled")) / len(unfilled), 3),
                "stopped_out_before_fill_pct": None if not limits else round(sum(1 for x in limits if x.get("stopped_before_fill")) / len(limits), 3),
            },
            "by_direction_mean_fwd_ret_pct": {d: round(float(g["fwd_ret_pct"].mean()), 3) for d, g in part.groupby("direction")} if len(part) else {},
        }
    # where does the edge (if any) live? limit-order R by setup, regime and direction, out-of-sample only
    oos = df[df["sample"] == "out_of_sample"]
    def _lim(g: pd.DataFrame) -> dict[str, Any]:
        rs = [x["r"] for x in g["limit"].dropna() if isinstance(x, dict) and x.get("filled")] if "limit" in g else []
        return _stats(rs)
    res["out_of_sample_limit_by"] = {k: {str(n): _lim(g) for n, g in oos.groupby(k)} for k in ("setup", "regime", "direction")}
    res["market_drift_note"] = f"Average forward {FWD_BARS[horizon]}-bar return across ALL decisions was {base:.2f}% (the bar to beat)."
    return res


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", choices=("swing", "positional", "long_term"), default="swing")
    parser.add_argument("--stocks", type=int, default=60)
    parser.add_argument("--step", type=int, default=7)
    parser.add_argument("--cost-pct", type=float, default=0.2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--out", default="../diagnostics/technical_validation")
    args = parser.parse_args()
    from sqlalchemy import text

    from app.db import engine
    with engine.connect() as c:
        symbols = [r[0] for r in c.execute(text("""select i.symbol from instruments i join market_bars b on b.instrument_id=i.id
            where b.interval='1d' and b.source='dhan' and i.is_synthetic=false and i.asset_class='equity' and i.country='IN'
            group by i.symbol having count(*) >= :need order by avg(b.close*b.volume) desc limit :k"""), {"need": WARMUP[args.horizon] + 400, "k": args.stocks})]
    print(f"validating {len(symbols)} stocks, horizon={args.horizon}, step={args.step}", flush=True)
    jobs = [(s, args.horizon, args.step, WARMUP[args.horizon], args.cost_pct) for s in symbols]
    rows: list[dict[str, Any]] = []
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for k, part in enumerate(pool.map(evaluate_symbol, jobs), 1):
            rows.extend(part)
            if k % 5 == 0:
                print(f"  {k}/{len(jobs)} stocks, {len(rows)} decisions, {time.time() - started:.0f}s", flush=True)
    summary = summarise(rows, args.horizon)
    import os
    os.makedirs(args.out, exist_ok=True)
    flat = pd.DataFrame([{**{k: v for k, v in r.items() if k not in ("immediate", "limit")}, "imm_r": (r.get("immediate") or {}).get("r"), "imm_exit": (r.get("immediate") or {}).get("exit"),
                          "lim_filled": (r.get("limit") or {}).get("filled"), "lim_r": (r.get("limit") or {}).get("r"), "lim_exit": (r.get("limit") or {}).get("exit")} for r in rows])
    flat.to_csv(f"{args.out}/{args.horizon}_decisions.csv", index=False)
    with open(f"{args.out}/{args.horizon}.json", "w") as fh:
        json.dump(summary, fh, indent=2, default=lambda o: None if (isinstance(o, float) and math.isnan(o)) else str(o))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
