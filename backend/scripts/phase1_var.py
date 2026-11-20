"""Phase 1 VAR diagnostics on STORED data (read-only). Writes diagnostics/phase1_var.json (replaced each run).

Run from backend/:  python scripts/phase1_var.py
Every run uses variant A (the only variant stored candles can support), the default config, and the single authoritative charge service."""
from __future__ import annotations

import json
import sys
import time as clock
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.var.backtest import instrument_data_from_underlying, run_backtest  # noqa: E402
from app.services.var.config import VarConfig  # noqa: E402
from app.services.var.funnel import build_funnel  # noqa: E402
from app.services.var.loader import load_underlying  # noqa: E402
from app.services.var.universe import rank_stock_universe  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase1_var.json"
CAP = Decimal("2000000")
log = lambda *a: print(*a, flush=True)  # noqa: E731


def compact(r, n_configs: int, keep_trades: bool = False) -> dict:
    d = {"window": r.window, "summary": r.summary, "by_setup": r.by_setup, "by_instrument": r.by_instrument, "by_direction": r.by_direction,
         "funnel": build_funnel(r.rejections, r.signals, r.trades, r.stage_counts), "assumptions": r.assumptions, "skipped_sessions": len(r.skipped_sessions),
         "configurations_evaluated_in_run": n_configs}
    d["funnel"].pop("rows")
    if keep_trades:
        d["trades"] = [{k: v for k, v in t.items() if k not in ("fills", "charge_orders", "derivation")} for t in r.trades]
    return d


def condition_pass_rates(rejections: list[dict], signals: list[dict], setup: str) -> dict:
    """Per-condition outcome among candidates of one setup (only candidates that reached their trigger bar)."""
    c: dict[str, Counter] = {}
    n = 0
    for r in rejections:
        if r.get("stage") == "strategy" and r.get("setup") == setup:
            n += 1
            for name, state in r.get("conditions", []):
                c.setdefault(name, Counter())[state] += 1
    return {"candidates_rejected_by_strategy": n, "conditions": {k: dict(v) for k, v in c.items()}}


def main() -> None:
    t0 = clock.time()
    out: dict = {"generated_for": "Phase 1 checkpoint", "variant": "A (volume profile + VWAP; order flow unavailable in stored data)", "initial_capital": str(CAP)}
    eng = create_engine(settings.database_url)
    with Session(eng) as db:
        idx = [load_underlying(db, u) for u in ("NIFTY", "BANKNIFTY")]
        short = [r[0] for r in db.execute(text("""
            with d as (select d.underlying_symbol u, (f.ts at time zone 'Asia/Kolkata')::date as dy, sum(f.close * f.volume) as t
                       from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id where d.instrument_type = 'FUTSTK' group by 1, 2)
            select u from d group by u order by percentile_cont(0.5) within group (order by t) desc limit 30"""))]
        stocks = [load_underlying(db, u) for u in short]
    log("loaded", len(idx), "index +", len(stocks), "stock underlyings in", round(clock.time() - t0), "s")
    out["stock_shortlist_by_whole_window_median_turnover"] = short
    out["shortlist_caveat"] = "shortlist uses the whole-window median turnover (mild look-ahead); the traded universe below is ranked point-in-time per session from prior sessions only"

    base = [instrument_data_from_underlying(u) for u in idx]
    runs: dict = {}

    def go(name: str, data, cfg: VarConfig, keep: bool = False) -> None:
        t = clock.time()
        r = run_backtest(data, cfg, CAP)
        runs[name] = compact(r, 1, keep)
        runs[name]["_raw"] = r
        log(name, "trades", len(r.trades), "signals", len(r.signals), "rejections", len(r.rejections), round(clock.time() - t), "s")

    A = dict(variant="A")
    go("index_both_engines_k1", base, VarConfig(**A), keep=True)
    go("index_breakout_only_k1", base, VarConfig(**A, engines=("breakout",)), keep=True)
    go("index_reversal_only_k1", base, VarConfig(**A, engines=("reversal",)), keep=True)
    for k in (2, 3, 4):
        go(f"index_both_engines_k{k}", base, VarConfig(**A, risk={"min_net_reward_to_cost": k}))
    for m in (10, 5):
        go(f"index_both_engines_k1_rvol_min{m}", base, VarConfig(**A, breakout={"rvol_min_sessions": m}))

    # engine interference: solo trades missing from the combined run
    def keyset(name: str) -> dict:
        return {(t["instrument"], t["entry_ts"].isoformat(), t["setup"]): t for t in runs[name]["_raw"].trades}

    comb, bo, rv = keyset("index_both_engines_k1"), keyset("index_breakout_only_k1"), keyset("index_reversal_only_k1")
    solo = {**{k: ("breakout", v) for k, v in bo.items()}, **{k: ("reversal", v) for k, v in rv.items()}}
    interference = {"combined_trades": len(comb), "breakout_solo_trades": len(bo), "reversal_solo_trades": len(rv),
                    "solo_sum": len(bo) + len(rv), "solo_net_sum": str(sum((v["net_pnl"] for v in list(bo.values()) + list(rv.values())), Decimal(0))),
                    "combined_net": str(sum((v["net_pnl"] for v in comb.values()), Decimal(0))), "in_solo_not_combined": [], "in_combined_not_solo": []}
    for k, (eng_name, t) in solo.items():
        if k not in comb:
            interference["in_solo_not_combined"].append({"engine": eng_name, "instrument": k[0], "entry": k[1], "net_pnl": str(t["net_pnl"]), "exit_reason": t["exit_reason"]})
    for k, t in comb.items():
        if k not in solo:
            interference["in_combined_not_solo"].append({"instrument": k[0], "entry": k[1], "setup": k[2], "net_pnl": str(t["net_pnl"])})
    # why: risk-gate / duplicate rejections in the combined run for the missing signals
    rr = runs["index_both_engines_k1"]["_raw"].rejections
    interference["combined_run_risk_and_duplicate_rejections"] = Counter(r["reasons"][0].split(":")[0] for r in rr if r.get("stage") in ("risk", "execution")).most_common()
    out["engine_interference"] = interference

    # reversal diagnosis (per-condition outcomes among candidates + pattern stages)
    rraw = runs["index_reversal_only_k1"]["_raw"]
    out["reversal_diagnosis"] = {"pattern_stages_unique": rraw.stage_counts, "conditions": condition_pass_rates(rraw.rejections, rraw.signals, "FAILED_AUCTION_REVERSAL"),
                                 "signals": len(rraw.signals), "trades": len(rraw.trades), "regime_note": "regime fails dominate when the day is not BALANCED; see conditions"}
    cb = runs["index_both_engines_k1"]["_raw"]
    out["breakout_conditions"] = condition_pass_rates(cb.rejections, cb.signals, "ACCEPTANCE_BREAKOUT")

    # warm-up: sessions whose relative-volume baseline was unavailable
    wu = {}
    for nm in ("index_both_engines_k1", "index_both_engines_k1_rvol_min10", "index_both_engines_k1_rvol_min5"):
        raw = runs[nm]["_raw"]
        sess_with_rv_invalid = {r["bar_end"][:10] for r in raw.rejections if any("relative_volume" in x for x in r["reasons"]) and any(n == "relative_volume" and s == "invalid_data_quality" for n, s in r.get("conditions", []))}
        wu[nm] = {"trades": len(raw.trades), "candidates_with_rvol_unavailable": sum(1 for r in raw.rejections if any(n == "relative_volume" and s == "invalid_data_quality" for n, s in r.get("conditions", []))),
                  "sessions_affected": len(sess_with_rv_invalid)}
    out["rvol_warmup"] = {"note": "futures history starts 2026-08-26; with a 20-session median the first 20 sessions have no baseline and no older stored futures candles exist to seed it. min_sessions is a diagnostic sweep, not a new default.", "runs": wu}

    # universe expansion: stock futures, point-in-time top-20 among the shortlist
    sdata = [instrument_data_from_underlying(u) for u in stocks]
    cfg = VarConfig(**A)
    sdays = sorted({d for x in sdata for d in x.sessions})
    prior = None
    uni_log = []
    for day in sdays:
        picked, rows = rank_stock_universe(stocks, day, prior, cfg)
        prior = day
        uni_log.append({"day": day.isoformat(), "selected": picked, "n_eligible": sum(1 for r in rows if r.get("eligible"))})
        for u, x in zip(stocks, sdata):
            if u.underlying not in picked:
                x.excluded_days[day] = "not in the point-in-time top-N liquid universe"
    out["stock_universe_pit"] = {"first_days_without_universe": [u["day"] for u in uni_log if not u["selected"]][:30], "sample_last_day": uni_log[-1] if uni_log else None,
                                 "days_with_universe": sum(1 for u in uni_log if u["selected"]), "criteria": "median traded value >= Rs 100 crore over the prior 20 sessions; spread/ban-list/results filters unavailable (not stored)"}
    go("index_plus_stock_futures_k1", base + sdata, cfg, keep=True)
    for nm in list(runs):
        raw = runs[nm].pop("_raw")
        if nm.startswith("index_plus_stock"):
            by_seg = {}
            for t in raw.trades:
                by_seg.setdefault("index" if t["instrument"].startswith(("NIFTY", "BANKNIFTY")) else "stock_futures", []).append(t)
            runs[nm]["trades_by_segment"] = {k: len(v) for k, v in by_seg.items()}
    out["runs"] = runs
    out["seconds"] = round(clock.time() - t0)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=str))
    log("done", OUT)


if __name__ == "__main__":
    main()
