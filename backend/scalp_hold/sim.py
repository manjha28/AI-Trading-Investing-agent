"""Event simulator. Signal at bar i close; earliest fill at bar i+1.

    Market entry : open[i+1] +/- slip, taker
    Limit entry  : low[i+1] < limit - penetration (strict, NOT touch); fills at the limit, maker; TTL 2 bars
    Target exit  : high[k] > target + penetration (strict); fills at the target, maker
    Stop exit    : low[k] <= stop -> min(stop, open[k]) - slip, taker
    Time exit    : close[k] -/+ slip, taker
    Intrabar     : stop before target, always

Floats everywhere except trade-level accounting: ``Decimal`` is created once per fill (``venues.make_fill``) and PnL/cost/carry are summed in Decimal.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from scalp_hold.barriers import BarrierCfg, cap_target_with_level, h_effective, penetration, stop_fraction
from scalp_hold.data import bar_minutes
from scalp_hold.features import Seasonal, sigma_h, tod_bucket
from scalp_hold.signals import SignalCfg, regime_flip, thesis_dead
from scalp_hold.venues import CostCtx, VenueProfile, index_end_date, make_fill, round_trip_pct

D = Decimal
SIGNAL_ORDER = ("T", "R", "B")


@dataclass
class SimCfg:
    equity: float = 10_000.0
    risk_pct: float = 0.005
    max_lev: float = 10.0
    leverage: float = 10.0              # crypto liquidation check
    maintenance_margin: float = 0.005
    r: float = 2.0
    H: float = 30.0
    partial_pct: float = 0.0            # grid [0, 30, 60]
    entry_mode: str = "market"          # market | limit
    limit_ttl: int = 2
    must_work_enabled: bool = False        # OFF by default: it pre-empted the barrier geometry on 50-100% of exits; now a swept parameter
    must_work_frac: float = 0.3
    must_work_at_frac: float = 1.0 / 3.0   # check the must-work rule after this fraction of H (spec: H/3)
    no_entry_hours_ist: frozenset[int] = frozenset()   # IST clock hours in which no NEW entry is taken (e.g. {11, 12, 13}); exits unaffected
    daily_loss_R: float = -3.0
    max_trades_day: int = 8
    lockout_bars: int = 12
    lockout_atr: float = 0.5
    forward_window_bars: int = 12       # MFE/MAE window for filtered-out signals
    slip_mult: float = 1.0
    tds_pct: Decimal = D("0")
    cost_ref_notional: float | None = None
    early_exits: bool = True
    entry_cutoff_minutes: int | None = None   # local-clock minutes after midnight; None = the venue's no_new_entry_after (NSE 15:00)


@dataclass
class SimResult:
    trades: pd.DataFrame
    filtered: pd.DataFrame
    skips: Counter
    n_signals: int
    cost_report: dict[str, float]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def below_min_lot_rate(self) -> float:
        return self.skips.get("below_min_lot", 0) / self.n_signals if self.n_signals else float("nan")


def _excursions(side: int, entry: float, hi: np.ndarray, lo: np.ndarray) -> tuple[float, int, float, int]:
    fav = (hi - entry) / entry if side == 1 else (entry - lo) / entry
    adv = (entry - lo) / entry if side == 1 else (hi - entry) / entry
    return float(max(fav.max(), 0.0)), int(fav.argmax()), float(max(adv.max(), 0.0)), int(adv.argmax())


def prepare(df: pd.DataFrame, f: pd.DataFrame) -> dict[str, np.ndarray]:
    keys = ("vwap", "sigma", "atr", "regime", "rvol", "vah", "val", "poc", "bsa", "sigma_bar", "slope_n", "z")
    arrays = {k: (f[k].to_numpy(dtype=float) if k in f else np.full(len(df), np.nan)) for k in keys}
    for k in ("open", "high", "low", "close", "volume"):
        arrays[k] = df[k].to_numpy(dtype=float)
    if "funding_rate" in df:
        arrays["funding_rate"] = df["funding_rate"].to_numpy(dtype=float)
    return arrays


def simulate(df: pd.DataFrame, f: pd.DataFrame, sig: pd.DataFrame, venue: VenueProfile, symbol: str, cfg: SimCfg, *, slip_bps: float, seasonal: Seasonal | None = None,
             barrier_cfg: BarrierCfg = BarrierCfg(), signal_cfg: SignalCfg = SignalCfg(), types: tuple[str, ...] = SIGNAL_ORDER, flip: bool = False,
             forced: list[dict] | None = None, is_surrogate: bool = False) -> SimResult:
    seasonal = seasonal or Seasonal()
    a = prepare(df, f)
    n, bm = len(df), bar_minutes(df.index)
    idx = df.index
    tick = float(venue.tick_size)
    lot = float(venue.lot_size)
    slip = slip_bps * cfg.slip_mult * 1e-4
    sig_h = sigma_h(f["sigma_bar"], idx, venue, cfg.H, seasonal).to_numpy()
    ref_price = float(np.nanmedian(a["close"]))
    cost_rt = round_trip_pct(venue, symbol, ref_price, cfg.cost_ref_notional or cfg.equity * min(cfg.max_lev, 2.0),
                              on=index_end_date(idx) if venue.is_india else None)
    local = idx.tz_convert(venue.tz)
    ist_hour = idx.tz_convert("Asia/Kolkata").hour.to_numpy()
    ist_min = idx.tz_convert("Asia/Kolkata").minute.to_numpy()          # the no-entry filter is specified in IST whatever the venue
    local_min = (local.hour * 60 + local.minute).to_numpy()
    day_key = local.strftime("%Y-%m-%d").to_numpy()
    waiver_window = venue.waiver.window_for(symbol) if venue.waiver and venue.waiver.enabled else None

    skips: Counter = Counter()
    trades: list[dict] = []
    filtered: list[dict] = []
    equity = cfg.equity
    next_free = 0
    day_R: dict[str, float] = {}
    day_trades: Counter = Counter()
    lock_type: dict[str, tuple[int, float]] = {}
    lock_any: tuple[int, float] | None = None

    if forced is None:
        cols = {(t, d): sig[f"{t}_{'long' if d == 1 else 'short'}"].to_numpy() for t in types for d in (1, -1)}
        cand_idx = np.flatnonzero(np.logical_or.reduce(list(cols.values())))
        candidates = [(int(i), t, d) for i in cand_idx for t in SIGNAL_ORDER if t in types for d in (1, -1) if cols[(t, d)][i]]
    else:
        candidates = [(int(g["i"]), g.get("stype", "X"), int(g["d"])) for g in forced]
    n_signals = len(candidates)
    forced_by_key = {(int(g["i"]), int(g["d"])): g for g in (forced or [])}

    def record_filtered(i: int, stype: str, d: int, reason: str) -> None:
        skips[reason] += 1
        if i + 1 >= n:
            return
        end = min(n, i + 1 + cfg.forward_window_bars)
        e = a["open"][i + 1]
        mfe, mfe_b, mae, mae_b = _excursions(d, e, a["high"][i + 1:end], a["low"][i + 1:end])
        filtered.append({"ts_signal": idx[i], "signal_type": stype, "side": "long" if d == 1 else "short", "reason": reason, "ist_hour": int(ist_hour[i]), "ist_minute": int(ist_min[i]),
                         "price": float(a["close"][i]), "session": str(day_key[i]), "MFE_pct": mfe * 100, "MFE_bar": mfe_b,
                         "MAE_pct": mae * 100, "MAE_bar": mae_b, "window_bars": end - i - 1})

    for i, stype, d0 in candidates:
        d = -d0 if flip else d0
        if i + 1 >= n:
            skips["no_next_bar"] += 1                                 # counted so the funnel reconciles: candidates = skips + trades
            continue
        if i < next_free:
            skips["position_open"] += 1                               # one position per instrument (previously uncounted)
            continue
        if venue.is_india:
            t_next = local_min[i + 1]
            cutoff = cfg.entry_cutoff_minutes if cfg.entry_cutoff_minutes is not None else venue.no_new_entry_after.hour * 60 + venue.no_new_entry_after.minute  # type: ignore[union-attr]
            if t_next >= cutoff or local.dayofweek[i + 1] >= 5:
                record_filtered(i, stype, d, "after_entry_cutoff")
                continue
        dk = day_key[i]
        if cfg.no_entry_hours_ist and int(ist_hour[i + 1]) in cfg.no_entry_hours_ist:   # applies to controls too, so they share the strategy's session constraints
            record_filtered(i, stype, d, "entry_hour_excluded")
            continue
        if forced is None:
            if day_R.get(dk, 0.0) <= cfg.daily_loss_R:
                record_filtered(i, stype, d, "daily_loss_limit")
                continue
            if day_trades[dk] >= cfg.max_trades_day:
                record_filtered(i, stype, d, "max_trades_day")
                continue
            e_px = a["open"][i + 1]
            locked = (stype in lock_type and lock_type[stype][0] > i) or (lock_any is not None and lock_any[0] > i and abs(e_px - lock_any[1]) <= cfg.lockout_atr * a["atr"][i])
            if locked:
                record_filtered(i, stype, d, "lockout")
                continue
        # ── geometry ──
        if forced is not None:
            g = forced_by_key[(i, d0)]
            s_i, tgt_pct, H_bars = float(g["s"]), float(g["target_pct"]), int(g["H_bars"])
            H_eff = H_bars * bm
        else:
            price_i = a["close"][i]
            s_arr, skip = stop_fraction(np.array([sig_h[i]]), cfg.r, cost_rt, tick, np.array([price_i]), barrier_cfg)
            if skip[0]:
                record_filtered(i, stype, d, "s_cap" if np.isfinite(sig_h[i]) else "no_volatility_estimate")
                continue
            s_i = float(s_arr[0])
            to_flat = None
            if venue.is_india and venue.flatten_time is not None:
                to_flat = venue.flatten_time.hour * 60 + venue.flatten_time.minute - local_min[i + 1]
                if to_flat <= 0:
                    record_filtered(i, stype, d, "no_time_before_flatten")
                    continue
            H_eff = h_effective(cfg.H, to_flat, waiver_window, barrier_cfg)
            H_bars = int(H_eff // bm)
            if H_bars < 1:
                record_filtered(i, stype, d, "horizon_below_one_bar")
                continue
            levels = [a["poc"][i], a["vah"][i], a["val"][i], a["vwap"][i]]
            tgt_pct = cap_target_with_level(a["close"][i], d, s_i, cfg.r, levels, barrier_cfg)
            if venue.has_funding and s_i >= 0.5 * (1.0 / cfg.leverage - cfg.maintenance_margin):
                record_filtered(i, stype, d, "liquidation_risk")
                continue
        # ── sizing ──
        px_est = a["open"][i + 1]
        target_notional = min(equity * cfg.risk_pct / s_i, equity * cfg.max_lev)
        qty = np.floor(target_notional / px_est / lot + 1e-9) * lot
        if qty <= 0:
            record_filtered(i, stype, d, "below_min_lot")
            continue
        # ── entry ──
        if cfg.entry_mode == "limit":
            limit_px = a["close"][i]
            e = None
            for b in range(i + 1, min(n, i + 1 + cfg.limit_ttl)):
                pen_b = float(penetration(limit_px, tick, slip_bps * cfg.slip_mult))
                if (a["low"][b] < limit_px - pen_b) if d == 1 else (a["high"][b] > limit_px + pen_b):
                    e = b
                    break
            if e is None:
                record_filtered(i, stype, d, "limit_not_filled")
                continue
            entry_px, entry_maker, first_k = limit_px, True, e + 1
        else:
            e = i + 1
            entry_px, entry_maker, first_k = a["open"][e] * (1 + d * slip), False, e
        stop_px = entry_px * (1 - d * s_i)
        tgt_px = entry_px * (1 + d * tgt_pct)
        one_r = entry_px * (1 + d * s_i)
        pen = float(penetration(entry_px, tick, slip_bps * cfg.slip_mult))
        k_end = min(e + H_bars - 1, n - 1)
        entry_ctx = {"extreme": (np.nanmin(a["low"][max(0, i - 2):i + 1]) if d == 1 else np.nanmax(a["high"][max(0, i - 2):i + 1])),
                     "level": a["vah"][i] if d == 1 else a["val"][i]}
        exit_k, exit_px, exit_maker, reason = k_end, a["close"][k_end] * (1 - d * slip), False, "time"
        partial: tuple[float, float] | None = None
        mfe_so_far = 0.0
        found = False
        for k in range(first_k, k_end + 1):
            hi, lo, op = a["high"][k], a["low"][k], a["open"][k]
            fav = ((hi - entry_px) / entry_px) if d == 1 else ((entry_px - lo) / entry_px)
            if (lo <= stop_px) if d == 1 else (hi >= stop_px):                       # stop before target, always
                exit_k, exit_px, exit_maker, reason, found = k, (min(stop_px, op) - slip * op) if d == 1 else (max(stop_px, op) + slip * op), False, "stop", True
                break
            mfe_so_far = max(mfe_so_far, fav)
            if cfg.partial_pct > 0 and partial is None and ((hi > one_r + pen) if d == 1 else (lo < one_r - pen)):
                partial = (one_r, cfg.partial_pct / 100.0)
            if (hi > tgt_px + pen) if d == 1 else (lo < tgt_px - pen):
                exit_k, exit_px, exit_maker, reason, found = k, tgt_px, True, "target", True
                break
            if k == k_end:
                break
            if cfg.early_exits and k + 1 <= n - 1:
                elapsed = (k - e + 1) * bm
                dead = None
                if cfg.must_work_enabled and elapsed >= cfg.H * cfg.must_work_at_frac and mfe_so_far < cfg.must_work_frac * tgt_pct:
                    dead = "must_work"
                elif stype in SIGNAL_ORDER and thesis_dead(stype, d, k, a, entry_ctx, signal_cfg):
                    dead = "thesis_dead"
                elif stype == "T" and regime_flip(d, k, a):
                    dead = "regime_flip"
                if dead:
                    exit_k, exit_px, exit_maker, reason, found = k + 1, a["open"][k + 1] * (1 - d * slip), False, dead, True
                    break
        last_bar = min(exit_k, n - 1)
        # ── trade accounting (Decimal from here) ──
        entry_ts, exit_ts = idx[e].to_pydatetime(), idx[last_bar].to_pydatetime()
        side_open, side_close = ("buy", "sell") if d == 1 else ("sell", "buy")
        fills = [make_fill(side_open, entry_px, qty, entry_maker, entry_ts, 0)]
        qty_left = qty
        seq = 1
        if partial is not None:
            pq = np.floor(qty * partial[1] / lot + 1e-9) * lot
            if 0 < pq < qty:
                fills.append(make_fill(side_close, partial[0], pq, True, exit_ts, seq))
                qty_left, seq = qty - pq, seq + 1
        fills.append(make_fill(side_close, exit_px, qty_left, exit_maker, exit_ts, seq))
        held = ((exit_k - e) + (0 if reason in ("must_work", "thesis_dead", "regime_flip") else 1)) * bm
        opened = sum((f_.notional for f_ in fills if f_.side == side_open), D("0"))
        closed = sum((f_.notional for f_ in fills if f_.side == side_close), D("0"))
        gross = closed - opened if d == 1 else opened - closed
        cost = venue.costs(fills, CostCtx(symbol, held, cfg.tds_pct))
        carry = D("0")
        if venue.has_funding and "funding_rate" in a:
            for kk in range(e, last_bar + 1):
                if idx[kk].hour in venue.funding_hours_utc and idx[kk].minute == 0 and np.isfinite(a["funding_rate"][kk]):
                    carry += D(repr(float(a["funding_rate"][kk]))) * opened * D(d)
        net = gross - cost - carry
        risk_amount = D(repr(float(qty * entry_px * s_i)))
        R = float(net / risk_amount) if risk_amount else float("nan")
        mfe, mfe_b, mae, mae_b = _excursions(d, entry_px, a["high"][e:last_bar + 1], a["low"][e:last_bar + 1])
        equity += float(net)
        day_R[dk] = day_R.get(dk, 0.0) + R
        day_trades[dk] += 1
        if net < 0:
            lock_type[stype] = (last_bar + cfg.lockout_bars, entry_px)
            lock_any = (last_bar + cfg.lockout_bars, entry_px)
        next_free = last_bar + 1
        trades.append({
            "instrument": symbol, "venue": venue.id, "ts_signal": idx[i], "ts_entry": idx[e], "ts_exit": idx[last_bar], "side": "long" if d == 1 else "short",
            "signal_type": stype, "entry_px": entry_px, "exit_px": exit_px, "s": s_i, "target": tgt_pct, "H_eff": H_eff, "qty": qty, "notional": float(qty * entry_px),
            "gross_pnl": float(gross), "cost_breakdown_json": _breakdown_json(venue, fills, CostCtx(symbol, held, cfg.tds_pct)), "carry": float(carry), "net_pnl": float(net),
            "R": R, "MFE_pct": mfe * 100, "MFE_bar": mfe_b, "MAE_pct": mae * 100, "MAE_bar": mae_b, "exit_reason": reason, "hold_minutes": held,
            "tod_bucket": int(tod_bucket(idx[i:i + 1], venue)[0]), "fee_R": float(cost / risk_amount) if risk_amount else float("nan"), "is_surrogate": is_surrogate,
            "H_bars": H_bars, "partial_pct": cfg.partial_pct,
            "cost_total": float(cost), "cost_pct_notional": float(cost / D(repr(float(qty * entry_px)))) * 100 if qty * entry_px else float("nan"),
            "ist_hour": int(ist_hour[i]), "ist_minute": int(ist_min[i]), "session": str(day_key[i]),
        })
    t = pd.DataFrame(trades)
    flt = pd.DataFrame(filtered)
    f_median = cost_rt / t["s"].median() if len(t) else float("nan")
    report = {"cost_rt_pct": cost_rt * 100, "f_median": f_median, "p_star": (1 + f_median) / (cfg.r + 1), "cost_schedule_date": str(index_end_date(idx)) if venue.is_india else None}
    return SimResult(t, flt, skips, n_signals, report, {"final_equity": equity, "is_surrogate": is_surrogate, "slip_bps": slip_bps * cfg.slip_mult})


def _breakdown_json(venue: VenueProfile, fills, ctx: CostCtx) -> str:
    import json
    return json.dumps({k: str(v) for k, v in venue.breakdown_fn(fills, ctx).items()})
