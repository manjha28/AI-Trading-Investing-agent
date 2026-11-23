"""Signal and entry-level research on stored real daily candles (hypothetical, never real P&L).

Two questions, answered with out-of-sample data before anything is implemented:
  A. WHICH SIGNALS predict forward returns (and can be turned into a confidence score that is actually calibrated)?
     Candidate features (momentum, 52-week-high proximity, short-term reversal, trend, relative strength, volatility, volume, breakout) are scored by the
     cross-sectional rank correlation (information coefficient, IC) between the feature at date t and the forward return after t, and by top-vs-bottom
     quintile spreads. Composites are built from the in-sample period only and judged out of sample.
  B. WHICH ENTRY STYLE (market, limit pullback, EMA limit, breakout stop) with which stop earns the best R, measured against a control that enters the
     same stocks on random dates with the same exits, so market drift is not mistaken for skill.

No look-ahead: every feature at date t uses candles up to and including t; trades start the next bar. First 60% of dates in-sample, last 40% out-of-sample.
Costs: flat round-trip percentage. Survivorship: only stocks that still trade are stored.

Run from backend/:  python -m app.services.signal_research --out ../diagnostics/signal_research
"""
from __future__ import annotations

import argparse
import json
import math
import os
from typing import Any

import numpy as np
import pandas as pd

HORIZONS = {"swing": 10, "positional": 30, "long_term": 90}
IS_FRACTION = 0.6


# ───────────────────────────── data ─────────────────────────────
def load_panel(min_bars: int = 900) -> dict[str, pd.DataFrame]:
    from sqlalchemy import text

    from app.db import engine
    q = text("""select i.symbol, b.timestamp::date as d, b.open, b.high, b.low, b.close, b.volume from market_bars b join instruments i on i.id=b.instrument_id
                where b.interval='1d' and b.source='dhan' and i.is_synthetic=false and i.asset_class='equity' and i.country='IN'
                  and i.symbol in (select i2.symbol from instruments i2 join market_bars b2 on b2.instrument_id=i2.id where b2.interval='1d' and b2.source='dhan'
                                   and i2.asset_class='equity' group by i2.symbol having count(*) >= :n)""")
    with engine.connect() as c:
        df = pd.read_sql(q, c, params={"n": min_bars})
    df["d"] = pd.to_datetime(df["d"])
    df = df.drop_duplicates(["symbol", "d"])
    out = {}
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = df.pivot(index="d", columns="symbol", values=col).astype(float).sort_index()
    return out


def ema(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(p: dict[str, pd.DataFrame], n: int = 14) -> pd.DataFrame:
    pc = p["close"].shift(1)
    tr = np.maximum(p["high"] - p["low"], np.maximum((p["high"] - pc).abs(), (p["low"] - pc).abs()))
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(x: pd.DataFrame, n: int) -> pd.DataFrame:
    d = x.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(p: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    c, h, l, v = p["close"], p["high"], p["low"], p["volume"]
    a14 = atr(p)
    mkt = c.pct_change().mean(axis=1)                         # equal-weight market proxy of the stored universe
    mkt_r63 = (1 + mkt).rolling(63).apply(np.prod, raw=True) - 1
    f: dict[str, pd.DataFrame] = {}
    for n in (5, 10, 21, 63, 126):
        f[f"ret_{n}"] = c / c.shift(n) - 1
    f["mom_12_1"] = c.shift(21) / c.shift(252) - 1
    f["mom_6_1"] = c.shift(21) / c.shift(126) - 1
    f["near_52w_high"] = c / c.rolling(252, min_periods=200).max()
    f["dist_20d_high_atr"] = (h.rolling(20).max() - c) / a14
    f["above_ema50"] = c / ema(c, 50) - 1
    f["above_ema200"] = c / ema(c, 200) - 1
    f["ema50_slope"] = ema(c, 50) / ema(c, 50).shift(10) - 1
    f["rsi2"] = rsi(c, 2)
    f["rsi14"] = rsi(c, 14)
    f["vol_atr_pct"] = a14 / c
    f["rv_60"] = c.pct_change().rolling(60).std()
    f["rs_63"] = f["ret_63"].sub(mkt_r63, axis=0)
    f["rvol_20"] = v / v.rolling(20).mean()
    f["breakout_20"] = (c >= h.rolling(20).max().shift(1)).astype(float)
    # the engine's own trend read, reproduced as a vote (price vs EMA20/50/200 and EMA50 slope) for a like-for-like baseline
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    f["engine_trend_vote"] = ((c > e20).astype(float) + (c > e50) + (e20 > e50) + (f["ema50_slope"] > 0) + (c > e200) + (e50 > e200)) / 6
    # simple, classic composites
    f["pullback_in_uptrend"] = -f["rsi2"].where(c > e200)                      # low RSI(2) while above the 200-EMA = buy-the-dip
    f["lowvol"] = -f["rv_60"]
    return f


def forward_returns(p: dict[str, pd.DataFrame], h: int, cost_pct: float = 0.2) -> pd.DataFrame:
    """Buy at the NEXT open, sell at the close h bars later, net of a flat round-trip cost."""
    return p["close"].shift(-(h + 1)) / p["open"].shift(-1) - 1 - cost_pct / 100


# ───────────────────────────── part A: signals ─────────────────────────────
def ic_series(feat: pd.DataFrame, fwd: pd.DataFrame, min_names: int = 30) -> pd.Series:
    out = {}
    for d in feat.index:
        x, y = feat.loc[d], fwd.loc[d]
        m = x.notna() & y.notna()
        if m.sum() >= min_names:
            out[d] = x[m].rank().corr(y[m].rank())
    return pd.Series(out)


def summarise_ic(ic: pd.Series, h: int, split: pd.Timestamp) -> dict[str, Any]:
    res = {}
    for name, part in (("in_sample", ic[ic.index < split]), ("out_of_sample", ic[ic.index >= split])):
        part = part.dropna().iloc[::h]                   # non-overlapping forward windows: an honest t-statistic
        n = len(part)
        res[name] = {"mean_ic": None if n == 0 else round(float(part.mean()), 4), "t_stat": None if n < 5 else round(float(part.mean() / (part.std() / math.sqrt(n))), 2), "n": n,
                     "hit_rate": None if n == 0 else round(float((part > 0).mean()), 3)}
    return res


def quintile_spread(feat: pd.DataFrame, fwd: pd.DataFrame, split: pd.Timestamp, h: int) -> dict[str, Any]:
    rows = []
    for d in feat.index[::h]:
        x, y = feat.loc[d], fwd.loc[d]
        m = x.notna() & y.notna()
        if m.sum() < 50:
            continue
        q = pd.qcut(x[m].rank(method="first"), 5, labels=False)
        means = y[m].groupby(q).mean()
        rows.append((d, *[means.get(i, np.nan) for i in range(5)]))
    t = pd.DataFrame(rows, columns=["d", "q1", "q2", "q3", "q4", "q5"]).set_index("d")
    out = {}
    for name, part in (("in_sample", t[t.index < split]), ("out_of_sample", t[t.index >= split])):
        out[name] = {"quintile_mean_fwd_pct": [None if part.empty else round(float(part[c].mean() * 100), 3) for c in ("q1", "q2", "q3", "q4", "q5")],
                     "top_minus_bottom_pct": None if part.empty else round(float((part["q5"] - part["q1"]).mean() * 100), 3)}
    return out


def composite(feats: dict[str, pd.DataFrame], signs: dict[str, int]) -> pd.DataFrame:
    ranks = [feats[k].rank(axis=1, pct=True) * (1 if s > 0 else -1) + (0 if s > 0 else 1) for k, s in signs.items()]
    return sum(ranks) / len(ranks)


def calibration(score: pd.DataFrame, fwd: pd.DataFrame, split: pd.Timestamp, h: int) -> dict[str, Any]:
    """Convert the composite into a 0-100 'confidence' (its cross-sectional percentile) and see whether higher bins really earn more, out of sample."""
    pct = score.rank(axis=1, pct=True) * 100
    res = {}
    for name, sel in (("in_sample", pct.index < split), ("out_of_sample", pct.index >= split)):
        x, y = pct[sel].iloc[::h].stack(), fwd[sel].iloc[::h].stack()
        df = pd.concat([x.rename("conf"), y.rename("ret")], axis=1).dropna()
        bins = pd.cut(df["conf"], [0, 20, 40, 60, 80, 100.0001], right=False, labels=["0-20", "20-40", "40-60", "60-80", "80-100"])
        g = df.groupby(bins, observed=True)["ret"]
        res[name] = {"bins": {str(k): {"n": int(v.size), "mean_fwd_pct": round(float(v.mean() * 100), 3), "hit_rate": round(float((v > 0).mean()), 3)} for k, v in g},
                     "monotonic_rank_corr": round(float(pd.Series([g.mean().get(b, np.nan) for b in ["0-20", "20-40", "40-60", "60-80", "80-100"]]).rank().corr(pd.Series(range(5)))), 3)}
    return res


# ───────────────────────────── part B: entries ─────────────────────────────
def simulate_entry(o, hi, lo, cl, a, t, style, k, stop_mult, rr, window, hold, cost):
    """One signal at bar t (close). Returns (filled, r_multiple, pct) or None if the order never fills. Stop is checked before target inside a bar."""
    n = len(cl)
    if t + 2 >= n or not np.isfinite(a[t]) or a[t] <= 0:
        return None
    atr_t, close_t = a[t], cl[t]
    if style == "market":
        fill_bar, fill = t + 1, o[t + 1]
    else:
        if style == "limit_atr":
            level = close_t - k * atr_t
        elif style == "limit_ema20":
            level = k                                    # ema20 passed in via k
        elif style == "breakout":
            level = np.nanmax(hi[max(0, t - 19):t + 1]) + 0.2 * atr_t
        else:
            return None
        fill_bar, fill = None, None
        for j in range(t + 1, min(t + 1 + window, n - 1)):
            if style == "breakout":
                if hi[j] >= level:
                    fill_bar, fill = j, max(level, o[j])
                    break
            elif lo[j] <= level:
                fill_bar, fill = j, min(level, o[j])
                break
        if fill_bar is None:
            return ("unfilled", 0.0, 0.0)
    stop = fill - stop_mult * atr_t
    risk = fill - stop
    if risk <= 0:
        return None
    target = fill + rr * risk if rr else None
    last = min(fill_bar + hold, n - 1)
    exit_px, kind = cl[last], "time"
    for j in range(fill_bar, last + 1):
        if lo[j] <= stop:
            exit_px, kind = (min(o[j], stop) if j > fill_bar else stop), "stop"
            break
        if target is not None and hi[j] >= target:
            exit_px, kind = target, "target"
            break
    r = (exit_px - fill) / risk - (cost / 100 * fill) / risk
    return ("filled", r, (exit_px / fill - 1) * 100 - cost)


def entry_study(p, feats, score, split, cost=0.2) -> dict[str, Any]:
    """Selected = top-quintile composite AND above the 200-EMA; Control = every stock-date (same exits). Lift over control is the evidence of skill."""
    o, hi, lo, cl = p["open"].values, p["high"].values, p["low"].values, p["close"].values
    a = atr(p).values
    e20 = ema(p["close"], 20).values
    pct = score.rank(axis=1, pct=True).values
    trend = (p["close"] > ema(p["close"], 200)).values
    dates = p["close"].index
    specs = [("market", 0, 2.0, 2.0), ("market", 0, 2.0, None), ("limit_atr", 0.5, 2.0, 2.0), ("limit_atr", 1.0, 2.0, 2.0), ("limit_atr", 1.5, 2.0, 2.0), ("limit_ema20", 0, 2.0, 2.0),
             ("breakout", 0, 2.0, 2.0), ("market", 0, 1.5, 2.0), ("market", 0, 3.0, 2.0), ("limit_atr", 1.0, 3.0, 2.0), ("limit_atr", 1.0, 1.5, 2.0), ("limit_atr", 1.0, 2.0, 3.0)]
    res: dict[str, Any] = {}
    step, window, hold = 5, 5, 20
    for style, k, stop_mult, rr in specs:
        key = f"{style}{'' if not k else '_' + str(k)}|stop {stop_mult} ATR|{'target ' + str(rr) + 'R' if rr else 'time exit'}"
        acc = {"selected": {"in_sample": [], "out_of_sample": []}, "control": {"in_sample": [], "out_of_sample": []}}
        fills = {"selected": [0, 0], "control": [0, 0]}
        for s in range(cl.shape[1]):
            col_o, col_h, col_l, col_c, col_a, col_e = o[:, s], hi[:, s], lo[:, s], cl[:, s], a[:, s], e20[:, s]
            for t in range(260, cl.shape[0] - hold - window - 3, step):
                sel = bool(np.isfinite(pct[t, s]) and pct[t, s] >= 0.8 and trend[t, s])
                sample = "in_sample" if dates[t] < split else "out_of_sample"
                for grp in (("selected",) if sel else ()) + ("control",):
                    kk = col_e[t] if style == "limit_ema20" else k
                    if style == "limit_ema20" and not (kk < col_c[t]):
                        continue
                    out = simulate_entry(col_o, col_h, col_l, col_c, col_a, t, style, kk, stop_mult, rr, window, hold, cost)
                    if out is None or not (np.isfinite(out[1]) and np.isfinite(out[2])):
                        continue
                    fills[grp][1] += 1
                    if out[0] == "filled":
                        fills[grp][0] += 1
                        acc[grp][sample].append((out[1], out[2]))
        entry = {}
        for grp in ("selected", "control"):
            entry[grp] = {"fill_rate": None if not fills[grp][1] else round(fills[grp][0] / fills[grp][1], 3)}
            for sample in ("in_sample", "out_of_sample"):
                v = np.array(acc[grp][sample]) if acc[grp][sample] else np.empty((0, 2))
                entry[grp][sample] = {"n": int(len(v)), "mean_r": None if not len(v) else round(float(v[:, 0].mean()), 3), "win_rate": None if not len(v) else round(float((v[:, 0] > 0).mean()), 3),
                                      "mean_pct": None if not len(v) else round(float(v[:, 1].mean()), 3),
                                      "profit_factor": None if not len(v) or (v[v[:, 0] <= 0, 0].sum() == 0) else round(float(v[v[:, 0] > 0, 0].sum() / -v[v[:, 0] <= 0, 0].sum()), 2)}
        s, c = entry["selected"]["out_of_sample"], entry["control"]["out_of_sample"]
        entry["oos_lift_mean_r"] = None if s["mean_r"] is None or c["mean_r"] is None else round(s["mean_r"] - c["mean_r"], 3)
        res[key] = entry
    return res


# ───────────────────────────── main ─────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="../diagnostics/signal_research")
    parser.add_argument("--min-bars", type=int, default=900)
    parser.add_argument("--skip-entries", action="store_true")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)
    p = load_panel(args.min_bars)
    dates = p["close"].index
    split = dates[int(len(dates) * IS_FRACTION)]
    print(f"{p['close'].shape[1]} stocks, {len(dates)} dates, {dates[0].date()}..{dates[-1].date()}, split {split.date()}", flush=True)
    feats = features(p)
    report: dict[str, Any] = {"stocks": int(p["close"].shape[1]), "dates": [str(dates[0].date()), str(dates[-1].date())], "split": str(split.date()), "cost_round_trip_pct": 0.2, "horizons": {}}
    best_signs: dict[str, dict[str, int]] = {}
    for hz, h in HORIZONS.items():
        fwd = forward_returns(p, h)
        table = {}
        for name, f in feats.items():
            table[name] = {"ic": summarise_ic(ic_series(f, fwd), h, split), "spread": quintile_spread(f, fwd, split, h)}
        # in-sample-only selection of composite members: |IS t| >= 1.0, sign from IS IC
        signs = {k: (1 if v["ic"]["in_sample"]["mean_ic"] > 0 else -1) for k, v in table.items() if v["ic"]["in_sample"]["t_stat"] is not None and abs(v["ic"]["in_sample"]["t_stat"]) >= 1.0
                 and k not in ("engine_trend_vote",)}
        best_signs[hz] = signs
        comp = composite(feats, signs) if signs else None
        baseline = calibration(feats["engine_trend_vote"], fwd, split, h)
        report["horizons"][hz] = {"forward_bars": h, "features": table, "composite_members": signs,
                                  "composite": None if comp is None else {"ic": summarise_ic(ic_series(comp, fwd), h, split), "spread": quintile_spread(comp, fwd, split, h), "calibration": calibration(comp, fwd, split, h)},
                                  "engine_trend_vote_baseline": {"ic": summarise_ic(ic_series(feats["engine_trend_vote"], fwd), h, split), "calibration": baseline}}
        print(f"  {hz}: composite {signs}", flush=True)
    if not args.skip_entries:
        signs = best_signs["swing"]
        score = composite(feats, signs) if signs else feats["engine_trend_vote"]
        print("entry study (swing composite)...", flush=True)
        report["entry_study_swing"] = entry_study(p, feats, score, split)
    with open(f"{args.out}/signal_research.json", "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    print("written", f"{args.out}/signal_research.json")


if __name__ == "__main__":
    main()
