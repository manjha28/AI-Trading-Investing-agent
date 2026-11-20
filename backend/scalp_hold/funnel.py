"""scalp_hold candidate funnel, s_cap feasibility and per-trade cost decomposition (diagnostics; nothing here changes trading logic)."""
from __future__ import annotations

import json
from collections import Counter

import numpy as np
import pandas as pd

from scalp_hold.barriers import BarrierCfg, stop_fraction
from scalp_hold.features import Seasonal, sigma_h
from scalp_hold.sim import SimCfg, SimResult
from scalp_hold.venues import VenueProfile, index_end_date, round_trip_pct

# reasons that occur BEFORE the stop geometry is computed (so they are not s_cap feasibility denominators)
PRE_GEOMETRY = ("position_open", "no_next_bar", "after_entry_cutoff", "entry_hour_excluded", "daily_loss_limit", "max_trades_day", "lockout")


def _bucket(hour: int, minute: int) -> str:
    return f"{hour:02d}:{(minute // 30) * 30:02d}"


def scalp_funnel(res: SimResult, symbol: str) -> dict:
    n = res.n_signals
    skips = Counter(res.skips)
    trades = res.trades
    flt = res.filtered
    n_tr = len(trades)
    rejected = sum(skips.values())
    pct = lambda x: round(x / n * 100, 1) if n else None  # noqa: E731
    reasons = [{"reason": k, "n": v, "pct_of_candidates": pct(v)} for k, v in skips.most_common()]
    cross: dict[str, dict[str, int]] = {}
    if len(flt):
        flt = flt.assign(bucket=[_bucket(h, m) for h, m in zip(flt["ist_hour"], flt["ist_minute"])])
        for (reason, bucket), g in flt.groupby(["reason", "bucket"]):
            cross.setdefault(reason, {})[bucket] = int(len(g))
    tr_bucket: dict[str, int] = {}
    if n_tr:
        for h, m in zip(trades["ist_hour"], trades["ist_minute"]):
            tr_bucket[_bucket(h, m)] = tr_bucket.get(_bucket(h, m), 0) + 1
    rows = []
    if len(flt):
        for r in flt.itertuples():
            rows.append({"engine": "scalp_hold", "stage": "rejected", "reason": r.reason, "instrument": symbol, "session": r.session, "tod_bucket": r.bucket, "setup": r.signal_type, "direction": r.side})
    for t in trades.itertuples() if n_tr else []:
        rows.append({"engine": "scalp_hold", "stage": "trade", "reason": "trade_executed", "instrument": symbol, "session": t.session, "tod_bucket": _bucket(t.ist_hour, t.ist_minute),
                     "setup": t.signal_type, "direction": t.side})
    return {"engine": "scalp_hold", "instrument": symbol, "candidates_generated": n, "rejected": rejected, "trades_executed": n_tr, "reasons": reasons,
            "reconciles": n == rejected + n_tr, "reconciliation": f"candidates {n} = rejected {rejected} + trades {n_tr}",
            "reason_by_time_of_day": cross, "trades_by_time_of_day": tr_bucket, "rows": rows}


def s_cap_feasibility(df: pd.DataFrame, f: pd.DataFrame, venue: VenueProfile, cfg: SimCfg, bcfg: BarrierCfg, seasonal: Seasonal, notionals: list[float],
                      signal_idx: np.ndarray | None = None) -> list[dict]:
    """Share of bars (or signal bars) whose stop would survive the cost/tick floors, by notional x time-of-day. Pure recomputation of the
    same geometry the simulator uses; cost_rt is re-priced per notional because flat brokerage dominates at small notional."""
    idx = df.index
    price = df["close"].to_numpy(float)
    sh = sigma_h(f["sigma_bar"], idx, venue, cfg.H, seasonal).to_numpy()
    loc = idx.tz_convert("Asia/Kolkata")
    buckets = np.array([_bucket(h, m) for h, m in zip(loc.hour, loc.minute)])
    sel = np.zeros(len(df), bool)
    if signal_idx is None:
        sel[:] = True
    else:
        sel[signal_idx] = True
    on = index_end_date(idx) if venue.is_india else None
    out = []
    for nt in notionals:
        cost_rt = round_trip_pct(venue, "x", float(np.nanmedian(price)), nt, on=on)
        _s, skip = stop_fraction(sh, cfg.r, cost_rt, float(venue.tick_size), price, bcfg)
        ok = ~skip & np.isfinite(sh)
        for b in sorted(set(buckets[sel])):
            m = sel & (buckets == b) & np.isfinite(sh)
            out.append({"notional": nt, "cost_rt_pct": round(cost_rt * 100, 4), "tod_bucket": b, "bars": int(m.sum()), "feasible_pct": round(float(ok[m].mean()) * 100, 1) if m.any() else None})
    return out


def cost_decomposition(trades: pd.DataFrame) -> dict:
    if not len(trades):
        return {"trades": 0}
    parts = pd.DataFrame([json.loads(x) for x in trades["cost_breakdown_json"]]).astype(float)
    tot = parts.sum(axis=1)
    pct = trades["cost_pct_notional"].astype(float)
    corr = float(np.corrcoef(trades["notional"].astype(float), pct)[0, 1]) if len(trades) > 2 else None
    q = pct.quantile([0.1, 0.25, 0.5, 0.75, 0.9]).round(4).to_dict()
    return {"trades": int(len(trades)), "component_share_pct": {k: round(float(parts[k].sum() / tot.sum() * 100), 1) for k in parts.columns},
            "cost_pct_of_notional_quantiles": {str(k): v for k, v in q.items()}, "corr_notional_vs_cost_pct": corr,
            "median_notional": float(trades["notional"].median()), "median_cost": float(trades["cost_total"].median())}
