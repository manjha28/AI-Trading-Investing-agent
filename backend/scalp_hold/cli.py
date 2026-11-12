"""scalp_hold command line:  python -m scalp_hold <fetch|causality|phase0|phase1|phase2|phase3> ...

Only ``fetch`` touches the network. Surrogate (bootstrap) output is always tagged is_surrogate=True.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from scalp_hold import evaluate as ev
from scalp_hold.causality import assert_causal
from scalp_hold.data import DataError, default_path, exclude_bad_sessions, fetch_delta, gap_report, in_session_mask, load_candles, resample, validate, bar_minutes
from scalp_hold.features import FeatureCfg, feature_functions, fit_seasonal
from scalp_hold.venues import SLIP_BPS, VENUES, crypto_delta, warn_unverified

DEFAULT_DATA = "diagnostics/scalp_hold/data"
DEFAULT_OUT = "diagnostics/scalp_hold/out"
RES_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def slip_for(venue_key: str, symbol: str) -> float:
    if venue_key == "equity":
        return SLIP_BPS["EQUITY"]
    return SLIP_BPS.get(symbol.replace("USDT", "USD"), SLIP_BPS.get(symbol, 0.0))


def load_instrument(args, symbol: str):
    venue = VENUES[args.venue]
    path = Path(args.file) if getattr(args, "file", None) else default_path(args.data_dir, args.venue, symbol, args.res)
    if not path.exists():
        raise SystemExit(f"no data file at {path}. Run `python -m scalp_hold fetch` (crypto) or pass --file with candles (ts_utc,open,high,low,close,volume).")
    df = load_candles(path)
    if bar_minutes(df.index) < RES_MIN[args.res]:
        df = resample(df, RES_MIN[args.res])
    if venue.is_india:
        df = df[in_session_mask(df.index, venue)]
    validate(df, venue)
    df, report = exclude_bad_sessions(df, venue, args.max_missing_pct)
    bad = int(report["excluded"].sum())
    print(f"[{symbol}] {len(df):,} bars, {df.index[0]} -> {df.index[-1]}; sessions excluded for >1% missing: {bad}")
    return venue, df


def cmd_fetch(args) -> None:
    if args.venue != "crypto":
        raise SystemExit("fetch is implemented for the crypto venue (Delta). For NSE data pass --file to the other commands.")
    out = Path(args.data_dir)
    out.mkdir(parents=True, exist_ok=True)
    for symbol in args.symbols:
        df = fetch_delta(symbol, args.res, args.days)
        validate(df, VENUES["crypto"])
        path = default_path(out, args.venue, symbol, args.res)
        df.to_csv(path, index_label="ts_utc")
        print(f"[{symbol}] saved {len(df):,} bars -> {path}")


def cmd_causality(args) -> None:
    venue, df = load_instrument(args, args.symbol)
    seasonal = fit_seasonal(df.iloc[: len(df) // 2], venue)
    failed = 0
    for name, fn in feature_functions(venue, FeatureCfg(), seasonal).items():
        try:
            n = assert_causal(df, fn, n_samples=args.samples if name != "volume_profile" else min(args.samples, 40), seed=0)
            print(f"  PASS  {name:<20} ({n} indices recomputed on prefixes)")
        except AssertionError as error:
            failed += 1
            print(f"  FAIL  {name:<20} {error}")
    if failed:
        raise SystemExit(f"{failed} feature(s) are NOT causal: do not use any downstream result.")
    print("All features are causal.")


def _prepared(args, symbol):
    venue, df = load_instrument(args, symbol)
    warn_unverified(scalper_offer_enabled=False, tds_pct=Decimal("0"), slip_bps={symbol: slip_for(args.venue, symbol)})
    return ev.prepare_instrument(df, venue, symbol, slip_for(args.venue, symbol))


def _grid(args) -> ev.Grid:
    return ev.QUICK_GRID if getattr(args, "quick", False) else ev.Grid()


def cmd_phase0(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = []
    for symbol in args.symbols:
        t = ev.phase0(_prepared(args, symbol), _grid(args))
        tables.append(t)
        print(f"\n== Phase 0: base rate, {symbol} ==")
        ev.print_phase0(t)
    pd.concat(tables).to_csv(out / "phase0.csv", index=False)


def cmd_phase1(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tables = []
    for symbol in args.symbols:
        t = ev.phase1(_prepared(args, symbol), _grid(args))
        tables.append(t)
        print(f"\n== Phase 1: signal edge over baseline, {symbol} (non-overlapping samples) ==")
        print(t.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    pd.concat(tables).to_csv(out / "phase1.csv", index=False)


def cmd_phase2(args) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    for symbol in args.symbols:
        p = _prepared(args, symbol)
        waivers = (False, True) if args.venue == "crypto" else (False,)
        if True in waivers:
            warn_unverified(scalper_offer_enabled=True, tds_pct=Decimal("0"), slip_bps={})
        res = ev.phase2(p, _grid(args), n_folds=args.folds, slip_mults=tuple(args.slip_mult), waiver_variants=waivers)
        results[symbol] = (p, res)
        print(f"\n== Phase 2: walk-forward, {symbol} ==   total_combos_evaluated={res.total_combos_evaluated}")
        print(res.folds.to_string(index=False, float_format=lambda x: f"{x:.4f}") if len(res.folds) else "no folds produced trades")
        for w in res.warnings:
            print("WARNING:", w, file=sys.stderr)
        if len(res.trades):
            res.trades.to_csv(out / f"trades_{symbol}.csv", index=False)
            print(json.dumps(ev.run_report(res.trades[(res.trades.slip_mult == 1) & (res.trades.partial_pct == 0)]), indent=2, default=str))
    return results


def cmd_phase3(args) -> None:
    args.slip_mult = [1]
    results = cmd_phase2(args)
    for symbol, (p, res) in results.items():
        if res.trades.empty:
            print(f"[{symbol}] no out-of-sample trades; nulls skipped")
            continue
        out = ev.phase3(p, res, _grid(args), seeds=args.control_seeds, bootstrap=args.bootstrap)
        mr = out["matched_random"]
        print(f"\n== Phase 3: nulls, {symbol} ==  (total_combos_evaluated={res.total_combos_evaluated + out['bootstrap']['total_combos_evaluated']})")
        print(f"  matched random entry: strategy mean R {mr['strategy_mean_R']:.4f} sits at percentile {mr['percentile']:.1f} of {len(mr['control_means'])} controls")
        print(f"  sign-flip:            mean R {out['sign_flip']['mean_R']:.4f} over {out['sign_flip']['n']} trades")
        b = out["bootstrap"]
        print(f"  block bootstrap [is_surrogate=True]: {b['n']} surrogates; strategy percentile {b['strategy_percentile']:.1f}; best-combo test mean R "
              f"min/median/max = {np.nanmin(b['best_combo_test_meanR']):.4f}/{np.nanmedian(b['best_combo_test_meanR']):.4f}/{np.nanmax(b['best_combo_test_meanR']):.4f}")
        Path(args.out).mkdir(parents=True, exist_ok=True)
        (Path(args.out) / f"phase3_{symbol}.json").write_text(json.dumps(out, default=str, indent=2))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="scalp_hold", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp, many=True):
        sp.add_argument("--venue", choices=sorted(VENUES), default="crypto")
        if many:
            sp.add_argument("--symbols", nargs="+", required=True)
        sp.add_argument("--res", default="5m", choices=sorted(RES_MIN))
        sp.add_argument("--data-dir", default=DEFAULT_DATA)
        sp.add_argument("--file", help="candle file (csv/parquet) instead of the default location; use with a single symbol")
        sp.add_argument("--out", default=DEFAULT_OUT)
        sp.add_argument("--quick", action="store_true", help="tiny grid for a smoke run (NOT for results)")
        sp.add_argument("--max-missing-pct", type=float, default=1.0, help="exclude sessions with more missing bars than this (spec default 1.0; raise it only knowingly, e.g. for provider-limited NSE tails)")

    f = sub.add_parser("fetch", help="download Delta candles (the only network command)")
    common(f)
    f.add_argument("--days", type=int, default=730)
    c = sub.add_parser("causality", help="prove every feature is causal (run this FIRST)")
    common(c, many=False)
    c.add_argument("--symbol", required=True)
    c.add_argument("--samples", type=int, default=200)
    for name in ("phase0", "phase1"):
        common(sub.add_parser(name))
    for name in ("phase2", "phase3"):
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--folds", type=int, default=5)
        sp.add_argument("--slip-mult", type=float, nargs="+", default=[1, 2, 3])
    sp3 = sub.choices["phase3"]
    sp3.add_argument("--bootstrap", type=int, default=50)
    sp3.add_argument("--control-seeds", type=int, default=50)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        {"fetch": cmd_fetch, "causality": cmd_causality, "phase0": cmd_phase0, "phase1": cmd_phase1, "phase2": cmd_phase2, "phase3": cmd_phase3}[args.cmd](args)
    except DataError as error:
        raise SystemExit(f"data error: {error}") from None


__all__ = ["main", "crypto_delta", "gap_report"]
