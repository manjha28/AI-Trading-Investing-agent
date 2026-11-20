"""Bridge between the app (stored 1-minute bars, owner-scoped jobs) and the scalp_hold engine. Pure Python: no database access, no network."""
from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from scalp_hold import evaluate as ev
from scalp_hold.barriers import BarrierCfg
from scalp_hold.data import DataError, exclude_bad_sessions, in_session_mask, resample, validate
from scalp_hold.features import fit_seasonal
from scalp_hold.sim import SimCfg, simulate
from scalp_hold.venues import VENUES, VenueProfile, warn_unverified

DEFAULT_PARAMS: dict[str, Any] = {
    "bar_minutes": 5, "signal_types": "T,R,B",
    # barrier geometry (spec section 5)
    "r": 2.0, "H_minutes": 30, "k_vol": 1.0, "theta_feeR": 0.25, "s_cap_mult": 2.0, "min_tick_mult": 5, "r_min": 1.5, "waiver_buffer": 2,
    # sizing and risk (spec section 7)
    "risk_pct": 0.005, "max_lev": 10.0, "leverage": 10.0, "maintenance_margin": 0.005, "daily_loss_R": -3.0, "max_trades_day": 8, "lockout_bars": 12,
    "lockout_atr": 0.5, "partial_pct": 0, "entry_mode": "market", "limit_ttl": 2, "must_work_enabled": False, "must_work_frac": 0.3, "must_work_at_frac": 0.3333,
    # session filter: IST clock hours with no new entries, comma-separated (e.g. "11,12,13"); empty = none
    "no_entry_hours_ist": "",
    # NSE entry cutoff as HH:MM local clock (default 15:00; try 15:10 with the existing 15:20 flatten). "" = venue default
    "entry_cutoff": "",
    # unverified config (spec hard rule 5): off / zero unless you change it
    "scalper_offer_enabled": False, "tds_pct": 0.0,
}


def _num(x: Any) -> Any:
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    if isinstance(x, dict):
        return {str(k): _num(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_num(v) for v in x]
    return x


def _records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    out = df.head(limit) if limit else df
    return [{str(k): _num(v) for k, v in row.items()} for row in out.to_dict("records")]


def venue_for(market: str, asset_class: str, params: dict[str, Any]) -> VenueProfile:
    if market == "crypto":
        from scalp_hold.venues import crypto_delta
        return crypto_delta(waiver_enabled=bool(params["scalper_offer_enabled"]))
    return VENUES["index"] if asset_class in ("future", "index") else VENUES["equity"]


def prepare_frame(frame_1m: pd.DataFrame, venue: VenueProfile, bar_minutes: int, max_missing_pct: float) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """1-minute -> bar_minutes bars, validated and gap-reported. Returns (frame or None, info). Never fills a gap."""
    info: dict[str, Any] = {"bars_1m": int(len(frame_1m))}
    try:
        df = resample(frame_1m, bar_minutes) if bar_minutes > 1 else frame_1m
        if venue.is_india:
            df = df[in_session_mask(df.index, venue)]
        validate(df, venue)
        df, report = exclude_bad_sessions(df, venue, max_missing_pct)
    except DataError as error:
        return None, {**info, "refused": f"data validation failed: {error}"}
    info.update(bars_used=int(len(df)), sessions_excluded=int(report["excluded"].sum()) if len(report) else 0, sessions_total=int(len(report)))
    if len(df) < 400:
        return None, {**info, "refused": f"only {len(df)} usable {bar_minutes}-minute bars after excluding sessions with more than {max_missing_pct}% missing candles"}
    return df, info


def _hhmm(v: Any) -> int | None:
    v = str(v).strip()
    if not v:
        return None
    h, m = v.split(":")
    if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError(f"entry_cutoff {v!r} is not HH:MM")
    return int(h) * 60 + int(m)


def coerce_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate per-run parameter overrides: known keys only, numbers parsed from strings, bad values reported together."""
    out: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in raw.items():
        if key not in DEFAULT_PARAMS:
            errors.append(f"unknown parameter {key!r}")
            continue
        default = DEFAULT_PARAMS[key]
        try:
            if isinstance(default, bool):
                out[key] = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                out[key] = int(float(value))
            elif isinstance(default, float):
                out[key] = float(value)
            else:
                out[key] = str(value)
        except (TypeError, ValueError):
            errors.append(f"{key}: {value!r} is not a valid value")
    if errors:
        raise ValueError("; ".join(errors))
    return out


def run_scalp_hold(frames: dict[str, tuple[pd.DataFrame, str]], market: str, kind: str, params_in: dict[str, Any], *, equity: float, max_lev: float | None, slip_bps: float,
                   max_missing_pct: float, quick: bool = False, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """``frames``: symbol -> (1-minute candle frame indexed by bar open UTC, asset_class). ``kind``: 'single' or 'walk_forward'.
    ``overrides``: per-run parameter changes (validated by ``coerce_params``), recorded in the result so every run states what it used."""
    if kind not in ("single", "walk_forward"):
        raise ValueError("scalp_hold supports the 'single' run and the 'walk_forward' evaluation (phases 0-2 plus the matched-random and sign-flip controls)")
    applied = coerce_params(overrides or {})
    p = {**DEFAULT_PARAMS, **{k: v for k, v in params_in.items() if k in DEFAULT_PARAMS}, **applied}
    if int(p["bar_minutes"]) < 1 or p["r"] <= 0 or p["H_minutes"] < p["bar_minutes"]:
        raise ValueError("bar_minutes >= 1, r > 0 and H_minutes >= bar_minutes are required")
    bm = int(p["bar_minutes"])
    types = tuple(t for t in str(p["signal_types"]).replace(" ", "").split(",") if t in ("T", "R", "B")) or ("T", "R", "B")
    bcfg = BarrierCfg(k_vol=float(p["k_vol"]), theta_feeR=float(p["theta_feeR"]), s_cap_mult=float(p["s_cap_mult"]), min_tick_mult=float(p["min_tick_mult"]),
                      r_min=float(p["r_min"]), waiver_buffer=float(p["waiver_buffer"]))
    base = SimCfg(equity=equity, risk_pct=float(p["risk_pct"]), max_lev=float(max_lev or p["max_lev"]), leverage=float(p["leverage"]), maintenance_margin=float(p["maintenance_margin"]),
                  r=float(p["r"]), H=float(p["H_minutes"]), partial_pct=float(p["partial_pct"]), entry_mode=str(p["entry_mode"]), limit_ttl=int(p["limit_ttl"]),
                  must_work_enabled=bool(p["must_work_enabled"]), must_work_frac=float(p["must_work_frac"]), entry_cutoff_minutes=_hhmm(p["entry_cutoff"]), daily_loss_R=float(p["daily_loss_R"]), max_trades_day=int(p["max_trades_day"]), lockout_bars=int(p["lockout_bars"]),
                  lockout_atr=float(p["lockout_atr"]), tds_pct=Decimal(repr(float(p["tds_pct"]))), must_work_at_frac=float(p["must_work_at_frac"]),
                  no_entry_hours_ist=frozenset(int(h) for h in str(p["no_entry_hours_ist"]).replace(" ", "").split(",") if h.strip().isdigit()))
    warnings = warn_unverified(scalper_offer_enabled=bool(p["scalper_offer_enabled"]), tds_pct=Decimal(repr(float(p["tds_pct"]))), slip_bps={s: slip_bps for s in frames})
    grid = ev.QUICK_GRID if quick else ev.Grid()
    out: dict[str, Any] = {"mode": "scalp_hold", "kind": kind, "engine": "scalp_hold", "params": _num(p), "param_overrides": _num(applied), "slip_bps": slip_bps, "unverified_assumptions": warnings,
                           "refused_instruments": [], "instruments": [], "is_surrogate": False,
                           "note": "scalp_hold results are hypothetical research output on stored bars, never real P&L, and not investment advice. Bootstrap surrogates (phase 3) run from the command line only."}
    all_trades: list[pd.DataFrame] = []
    total_combos = 0
    for symbol, (frame, asset_class) in frames.items():
        venue = venue_for(market, asset_class, p)
        df, info = prepare_frame(frame, venue, bm, max_missing_pct)
        if df is None:
            out["refused_instruments"].append({"symbol": symbol, "reason": info["refused"], "info": _num(info)})
            continue
        prep = ev.prepare_instrument(df, venue, symbol, slip_bps)
        entry: dict[str, Any] = {"symbol": symbol, "venue": venue.id, **_num(info), "start": df.index[0].isoformat(), "end": df.index[-1].isoformat()}
        if kind == "single":
            cut = int(len(df) * 0.4)
            seasonal = fit_seasonal(df.iloc[:cut], venue)                      # fitted on the first 40% only, frozen for the 60% that is traded
            test = replace(prep, df=prep.df.iloc[cut:], f=prep.f.iloc[cut:], sig=prep.sig.iloc[cut:])
            res = simulate(test.df, test.f, test.sig, venue, symbol, base, slip_bps=slip_bps, seasonal=seasonal, barrier_cfg=bcfg, types=types)
            entry.update(traded_from=test.df.index[0].isoformat(), seasonal_fitted_through=seasonal.fitted_through, n_signals=res.n_signals, skips=dict(res.skips),
                         below_min_lot_rate=_num(res.below_min_lot_rate), cost_report=_num(res.cost_report), filtered_signals=_records(res.filtered, 200),
                         report=_num(ev.run_report(res.trades)))
            if len(res.trades):
                all_trades.append(res.trades)
        else:
            waivers = (False, True) if venue.has_funding else (False,)
            p2 = ev.phase2(prep, grid, slip_mults=(1, 2, 3), base=base, bcfg=bcfg, waiver_variants=waivers)
            total_combos += p2.total_combos_evaluated
            entry.update(folds=_records(p2.folds), chosen=_num(p2.chosen), warnings=p2.warnings, total_combos_evaluated=p2.total_combos_evaluated)
            base_trades = p2.trades[(p2.trades["slip_mult"] == 1) & (p2.trades["partial_pct"] == 0) & (~p2.trades["waiver"])] if len(p2.trades) else p2.trades
            entry["report"] = _num(ev.run_report(base_trades))
            if len(base_trades):
                all_trades.append(base_trades)
                seasonal_by_fold = {j: fit_seasonal(prep.df.iloc[tr], venue) for j, (tr, _) in enumerate(ev.anchored_folds(len(prep.df), 5), 1)}
                mr = ev.matched_random_control(prep, base_trades, seasonal_by_fold[max(seasonal_by_fold)], base, seeds=50)
                entry["matched_random"] = _num({k: (v if k != "control_means" else [round(x, 4) for x in v]) for k, v in mr.items()})
                entry["sign_flip"] = _num(ev.sign_flip_control(prep, p2.chosen, seasonal_by_fold, base, bcfg))
        entry["phase0"] = _records(ev.phase0(prep, grid, equity=equity, bcfg=bcfg), 60)
        entry["phase1"] = _records(ev.phase1(prep, grid, equity=equity, bcfg=bcfg), 120)
        out["instruments"].append(entry)
    out["total_combos_evaluated"] = total_combos
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    out["trades_total"] = int(len(trades))
    out["trades"] = _records(trades.drop(columns=[c for c in ("fold",) if c in trades], errors="ignore"), 500)
    out["summary"] = _num(ev.run_report(trades)) if len(trades) else {"n": 0}
    return out
