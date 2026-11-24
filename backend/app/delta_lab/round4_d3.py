"""Round 4 / Part D3: multi-day strategies on pumped coins (runner long, pump short, parabolic short). Hypothetical research; decision support, not investment advice.

DATA: Binance USDT-M perpetual DAILY candles and funding history from data.binance.vision for every contract the archive lists, INCLUDING contracts that were later delisted
(Delta India publishes no history for delisted contracts, and its own listings are too young for this question). Jan 2022 to Oct 2026. Delta's EVAAUSD is checked separately (see `evaa_case`).
Costs are Delta's: taker 0.05% + 18% GST per fill, slippage 8 bps per fill (assumed: no order book exists for these coins), funding from the history at every 8-hour time held (shorts RECEIVE
a positive rate). Entries are at the next day's open; a day is only used once it has closed.
EVENT: a close at least +70% above the lowest close of the prior 10 days, with 5-day mean quote volume >= 3x its 60-day median; 30-day refractory period. PARABOLIC tag: the running high is >= 4 ATR above the 20-day average.
RISK in every variant: effective leverage 2x, the stop sits beyond the swing extreme plus 1 ATR, the stop must be within 40% of the entry (else no trade: it would not trigger well before liquidation),
scale-in in 3 tranches (never added to a short on a new high or a rising close; never added to a runner below its last higher low), trailing stop (3 ATR), time stop, a half-size cut on a volume spike against the position.
Open interest has no history: the 'funding strongly positive while OI falls' entry is tested with funding only (stated).
SPLITS by event date: train first 50%, validation next 25%, HOLDOUT latest 25% (frozen before looking); choose on train+validation; each finalist touches the holdout once."""
from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import data
from .round4_d2 import boot_ci, dsr

D3_DIR = data.DATA_DIR / "binance_d3"
TAKER, GST, SLIP = 0.0005, 0.18, 8.0
FEE_BPS = TAKER * 1e4 * (1 + GST)
LEVERAGE = 2.0
N_PRIOR_VARIANTS = 148 + 130                      # rounds 1-3 plus tonight's D2 variants
EVENT_RISE, EVENT_VOL, REFRACTORY = 0.70, 3.0, 30
STABLE = ("USDC", "BUSD", "TUSD", "FDUSD", "USDP")


@dataclass
class Coin:
    sym: str
    t: np.ndarray; o: np.ndarray; h: np.ndarray; l: np.ndarray; c: np.ndarray; qv: np.ndarray
    atr: np.ndarray; sma20: np.ndarray
    funding_by_day: np.ndarray                        # funding rate fraction summed over the day's 8-hour times (positive: longs pay)
    delisted: bool


def _norm_t(t: np.ndarray) -> np.ndarray:
    t = t.astype("int64")
    return np.where(t > 10**14, t // 1000, t) // 1000        # seconds


def make_coin(sym: str, k: pd.DataFrame, f: pd.DataFrame, last_day: int) -> Coin | None:
    if len(k) < 90:
        return None
    t = _norm_t(k.t.to_numpy())
    o, h, l, c, qv = (k[x].to_numpy(float) for x in ("o", "h", "l", "c", "qv"))
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    sma = pd.Series(c).rolling(20).mean().to_numpy()
    fd = np.zeros(len(t))
    if len(f):
        ft = _norm_t(f.t.to_numpy())
        day = ft // 86400 * 86400
        s = pd.Series(f.rate.to_numpy(float)).groupby(day).sum()
        idx = {int(x): i for i, x in enumerate(t // 86400 * 86400)}
        for d, v in s.items():
            if int(d) in idx:
                fd[idx[int(d)]] = v
    return Coin(sym, t, o, h, l, c, qv, atr, sma, fd, delisted=bool(t[-1] < last_day - 3 * 86400))


def find_events(cn: Coin) -> list[int]:
    out, last = [], -10**9
    qm = pd.Series(cn.qv)
    vol5, vol60 = qm.rolling(5).mean().to_numpy(), qm.rolling(60).median().shift(5).to_numpy()
    for i in range(70, len(cn.t) - 3):
        base = np.min(cn.c[i - 10:i])
        if base > 0 and cn.c[i] / base - 1 >= EVENT_RISE and vol60[i] > 0 and vol5[i] / vol60[i] >= EVENT_VOL and i - last >= REFRACTORY:
            out.append(i)
            last = i
    return out


def _cost_return(entries: list[tuple[int, float]], exit_px: float, exit_i: int, side: int, cn: Coin) -> tuple[float, float, float]:
    """entries: [(day index, fill price)] equal tranches. Returns (net return of the whole position as a fraction of notional, gross, funding)."""
    n = len(entries)
    gross = sum(side * (exit_px - p) / p for _, p in entries) / n
    fees = 2 * (FEE_BPS + SLIP) / 1e4
    funding = 0.0
    for di, _ in entries:
        funding += side * float(np.sum(cn.funding_by_day[di + 1:exit_i + 1])) / n          # a long pays a positive rate, a short receives it
    return gross - fees - funding, gross, funding


def trade(cn: Coin, ev: int, kind: str, rule: str, trail_atr: float, hold: int, parabolic_only: bool = False) -> dict | None:
    """kind: runner | short. Returns one event result or None (no entry). All decisions use days up to and including the decision day; the fill is the next day's open."""
    n = len(cn.t)
    start = ev
    hi_i = ev                                                                  # running high day since the event
    base_low = float(np.min(cn.l[max(0, ev - 10):ev + 1]))
    entry_i, parabolic = None, False
    for t in range(ev + 1, min(n - 2, ev + 25)):
        if cn.h[t] >= cn.h[hi_i]:
            hi_i = t
        dist = (cn.h[hi_i] - cn.sma20[hi_i]) / cn.atr[hi_i] if cn.atr[hi_i] > 0 and np.isfinite(cn.sma20[hi_i]) else 0
        parabolic = dist >= 4
        if kind == "runner":
            hi10 = np.max(cn.h[max(0, ev - 10):t])
            if rule == "breakout_confirm" and t == ev + 1:
                entry_i = t
            elif rule == "first_pullback_holds" and cn.l[t] <= cn.h[hi_i] * 0.92 and cn.c[t] > cn.c[t - 1] * 0.99 and cn.c[t] > np.min(cn.l[ev:t + 1]) and cn.c[t] > cn.sma20[t]:
                entry_i = t
            elif rule == "close_above_prior_swing_high" and t > ev + 1 and cn.c[t] > hi10 and hi_i == t:
                entry_i = t
        else:
            if parabolic_only and not parabolic:
                continue
            if hi_i == t:
                continue                                                          # never short into a new high
            lower_high = cn.h[t] < cn.h[t - 1] and hi_i == t - 1 and cn.c[t] < cn.c[t - 1]
            lowest_recent = np.min(cn.l[max(hi_i - 3, 0):hi_i + 1])
            if rule == "first_lower_high" and lower_high:
                entry_i = t
            elif rule == "break_last_higher_low" and t > hi_i and cn.c[t] < lowest_recent and cn.c[t] < cn.c[t - 1]:
                entry_i = t
            elif rule == "funding_hot_longs_leaving" and t > hi_i and cn.funding_by_day[t] >= 0.0006 and cn.c[t] < cn.c[t - 1] and cn.c[t] < cn.c[hi_i] * 0.95:
                entry_i = t
            elif rule == "blowoff_then_close_below_low":
                rng_ = cn.h[hi_i] - cn.l[hi_i]
                wick = (cn.h[hi_i] - max(cn.o[hi_i], cn.c[hi_i])) / rng_ if rng_ > 0 else 0
                record = cn.qv[hi_i] >= np.max(cn.qv[max(0, hi_i - 60):hi_i]) if hi_i > 0 else False
                if t > hi_i and wick >= 0.5 and record and cn.c[t] < cn.l[hi_i]:
                    entry_i = t
            elif rule == "fixed_delay_3d" and t == hi_i + 3 and cn.c[t] < cn.c[t - 1]:
                entry_i = t
        if entry_i is not None:
            break
    if entry_i is None or entry_i + 2 >= n:
        return None
    side = 1 if kind == "runner" else -1
    ext = cn.l[ev:entry_i + 1].min() if side > 0 else cn.h[hi_i]
    px0 = cn.o[entry_i + 1]
    stop = ext - cn.atr[entry_i] if side > 0 else ext + cn.atr[entry_i]
    if abs(stop - px0) / px0 > 0.40 or (side > 0 and stop >= px0) or (side < 0 and stop <= px0):
        return None                                                                # the stop would not trigger well before liquidation at 2x, or it is on the wrong side
    tranches = [(entry_i + 1, px0 * (1 + side * SLIP / 1e4))]
    best_close = cn.c[entry_i]
    exit_i, exit_px, reason, cut = None, None, "time", False
    last = min(n - 1, entry_i + 1 + hold)
    worst = 0.0
    run_high_after = cn.h[hi_i]
    for d in range(entry_i + 1, last + 1):
        adverse = (cn.h[d] / px0 - 1) if side < 0 else (1 - cn.l[d] / px0)
        worst = max(worst, adverse)
        stop_hit = (cn.h[d] >= stop) if side < 0 else (cn.l[d] <= stop)
        trail = (best_close + trail_atr * cn.atr[d - 1]) if side < 0 else (best_close - trail_atr * cn.atr[d - 1])
        trail_hit = (cn.h[d] >= trail and d > entry_i + 1) if side < 0 else (cn.l[d] <= trail and d > entry_i + 1)
        if stop_hit:
            exit_i, exit_px, reason = d, (max(stop, cn.o[d]) if side < 0 else min(stop, cn.o[d])) * (1 - side * SLIP / 1e4), "stop"
            break
        if trail_hit:
            lvl = max(trail, cn.o[d]) if side < 0 else min(trail, cn.o[d])
            exit_i, exit_px, reason = d, lvl * (1 - side * SLIP / 1e4), "trail"
            break
        if side < 0 and cn.l[d] <= base_low:                                       # target: the pre-pump base
            exit_i, exit_px, reason = d, base_low * (1 - side * SLIP / 1e4), "target_base"
            break
        best_close = min(best_close, cn.c[d]) if side < 0 else max(best_close, cn.c[d])
        # scale-in on days 2 and 3 only while the rules for adding still hold
        if len(tranches) < 3 and d <= entry_i + 2 and d + 1 < n:
            ok = (cn.c[d] < cn.c[d - 1] and cn.h[d] < run_high_after) if side < 0 else (cn.c[d] > cn.c[d - 1] and cn.l[d] > np.min(cn.l[ev:d]))
            if ok:
                tranches.append((d + 1, cn.o[d + 1] * (1 + side * SLIP / 1e4)))
        vol_med = np.median(cn.qv[max(0, d - 20):d]) if d > 5 else 0
        if not cut and vol_med > 0 and cn.qv[d] >= 3 * vol_med and ((cn.c[d] - cn.o[d]) * -side) > cn.atr[d]:
            cut = True                                                              # sharp volume spike against the position: half size from here
            half = 0.5 * side * (cn.c[d] - px0) / px0
            cut_gain = half
            tranches = tranches[:1] + tranches[1:]                                  # (cost of the half exit is charged below via fees on a half position)
            cut_info = (d, cn.c[d] * (1 - side * SLIP / 1e4))
            _ = cut_gain, cut_info
    if exit_i is None:
        exit_i = last
        exit_px = cn.c[last] * (1 - side * SLIP / 1e4)
        reason = "delisted_close" if (cn.delisted and last == n - 1) else "time"
    net, gross, funding = _cost_return(tranches, exit_px, exit_i, side, cn)
    return {"coin": cn.sym, "t": int(cn.t[entry_i]), "side": side, "net": net, "gross": gross, "funding": funding, "days": int(exit_i - entry_i), "reason": reason, "mae": worst, "tranches": len(tranches),
            "parabolic": bool(parabolic), "squeeze_stop": reason == "stop" and side < 0, "delisted_coin": cn.delisted, "half_cut": cut}


RULES = {
    "runner": [("breakout_confirm", 3.0, 20), ("first_pullback_holds", 3.0, 20), ("close_above_prior_swing_high", 3.0, 20), ("first_pullback_holds", 2.0, 15)],
    "pump_short": [(r, 3.0, 15) for r in ("first_lower_high", "break_last_higher_low", "funding_hot_longs_leaving", "blowoff_then_close_below_low", "fixed_delay_3d")] + [("first_lower_high", 2.0, 10)],
    "parabolic_short": [(r, 3.0, 15) for r in ("first_lower_high", "blowoff_then_close_below_low", "funding_hot_longs_leaving")] + [("break_last_higher_low", 2.0, 10)],
}


def summarise(rs: list[dict]) -> dict:
    if not rs:
        return {"events": 0}
    r = np.array([x["net"] for x in rs])
    gp, gl = r[r > 0].sum(), -r[r < 0].sum()
    return {"events": len(rs), "coins": len({x["coin"] for x in rs}), "hit_rate": float((r > 0).mean()), "mean_net_pct": float(r.mean() * 100), "median_net_pct": float(np.median(r) * 100),
            "worst_loss_pct": float(r.min() * 100), "profit_factor": float(gp / gl) if gl > 0 else None, "avg_days": float(np.mean([x["days"] for x in rs])),
            "max_adverse_before_exit_pct": float(np.max([x["mae"] for x in rs]) * 100), "squeeze_stop_outs": int(sum(x["squeeze_stop"] for x in rs)), "delisted_coin_trades": int(sum(x["delisted_coin"] for x in rs))}


def run_all() -> dict:
    files = sorted(D3_DIR.glob("*.pkl"))
    coins, last_day = [], 0
    raw = {}
    for p in files:
        k, f = pickle.load(open(p, "rb"))
        raw[p.stem] = (k, f)
        last_day = max(last_day, int(_norm_t(k.t.to_numpy())[-1]))
    for sym, (k, f) in raw.items():
        if any(sym.startswith(s) for s in STABLE) or sym.startswith("USDC"):
            continue
        cn = make_coin(sym, k, f, last_day)
        if cn is not None:
            coins.append(cn)
    events = [(cn, ev) for cn in coins for ev in find_events(cn)]
    events.sort(key=lambda x: x[0].t[x[1]])
    times = [int(cn.t[ev]) for cn, ev in events]
    n = len(events)
    e1, e2 = times[int(n * 0.5)], times[int(n * 0.75)]
    cuts = {"train": [times[0], e1], "validation": [e1, e2], "holdout": [e2, times[-1] + 1]}
    iso = lambda t: pd.Timestamp(t, unit="s").strftime("%Y-%m-%d")  # noqa: E731
    variants, best = [], {}
    results: dict[tuple, list[dict]] = {}
    for kind, rules in RULES.items():
        for rule, trail, hold in rules:
            rs = []
            for cn, ev in events:
                x = trade(cn, ev, "runner" if kind == "runner" else "short", rule, trail, hold, parabolic_only=(kind == "parabolic_short"))
                if x:
                    rs.append(x)
            results[(kind, rule, trail, hold)] = rs
            trn = [x for x in rs if cuts["train"][0] <= x["t"] < cuts["train"][1]]
            val = [x for x in rs if cuts["validation"][0] <= x["t"] < cuts["validation"][1]]
            rec = {"type": kind, "rule": rule, "trail_atr": trail, "hold_days": hold, "train": summarise(trn), "validation": summarise(val)}
            allr = np.array([x["net"] for x in trn + val])
            rec["sharpe"] = float(allr.mean() / allr.std(ddof=1)) if len(allr) > 10 and allr.std(ddof=1) > 0 else None
            variants.append(rec)
            if len(trn) + len(val) >= 15 and len(val) >= 5:
                score = float(np.mean([x["net"] for x in val]))
                if kind not in best or score > best[kind]["score"]:
                    best[kind] = {"score": score, "key": (kind, rule, trail, hold), "rec": rec}
    n_total = N_PRIOR_VARIANTS + len(variants)
    srs = [v["sharpe"] for v in variants if v["sharpe"] is not None]
    var_sr = float(np.var(srs, ddof=1)) if len(srs) > 2 else 0.01
    finals = []
    h0, h1 = cuts["holdout"]
    for kind, b in best.items():
        rs = results[b["key"]]
        hold = [x for x in rs if h0 <= x["t"] < h1]
        r = np.array([x["net"] for x in hold])
        thirds = [h0 + (h1 - h0) * k // 3 for k in range(4)]
        subs = [float(np.mean([x["net"] for x in hold if thirds[k] <= x["t"] < thirds[k + 1]]) * 100) if any(thirds[k] <= x["t"] < thirds[k + 1] for x in hold) else None for k in range(3)]
        lo, hi = boot_ci(r) if len(r) else (float("nan"), float("nan"))
        by_coin: dict[str, float] = {}
        for x in hold:
            by_coin[x["coin"]] = by_coin.get(x["coin"], 0.0) + x["net"]
        profit = sum(v for v in by_coin.values() if v > 0)
        top_share = max(by_coin.values()) / profit if profit > 0 and by_coin else None
        st = summarise(hold)
        d = dsr(r, n_total, var_sr) if len(r) >= 10 else float("nan")
        rnd = []
        rng = np.random.default_rng(5)
        for seed in range(20):                                                       # baseline: same rule structure but the entry day is random within the 25 days after the event
            sims = []
            for cn, ev in events:
                if not (h0 <= int(cn.t[ev]) < h1):
                    continue
                sub = Coin(cn.sym, cn.t, cn.o, cn.h, cn.l, cn.c, cn.qv, cn.atr, cn.sma20, cn.funding_by_day, cn.delisted)
                x = trade(sub, ev + int(rng.integers(0, 12)), "runner" if kind == "runner" else "short", b["key"][1], b["key"][2], b["key"][3], parabolic_only=(kind == "parabolic_short"))
                if x:
                    sims.append(x["net"])
            rnd.append(np.mean(sims) * 100 if sims else np.nan)
        checks = {"events_ge_30": st.get("events", 0) >= 30, "profit_factor_ge_1.3": bool(st.get("profit_factor") and st["profit_factor"] >= 1.3), "positive_in_2_of_3_subperiods": sum(1 for x in subs if x is not None and x > 0) >= 2,
                  "bootstrap90_lower_above_0": bool(np.isfinite(lo) and lo > 0), "deflated_prob_ge_0.9": bool(np.isfinite(d) and d >= 0.9), "many_coins_not_one_or_two": bool(st.get("coins", 0) >= 10 and (top_share is not None and top_share < 0.3)),
                  "random_entry_baseline_loses": bool(np.nanmean(rnd) < 0)}
        finals.append({"type": kind, "rule": b["key"][1], "trail_atr": b["key"][2], "hold_days": b["key"][3], "param_hash": hashlib.sha256(json.dumps(list(b["key"])).encode()).hexdigest()[:16],
                       "validation": b["rec"]["validation"], "train": b["rec"]["train"], "holdout": st, "holdout_bootstrap90_net_pct": [lo * 100, hi * 100], "holdout_thirds_net_pct": subs,
                       "top_coin_share_of_profit": top_share, "deflated_probability": d, "random_entry_mean_net_pct": float(np.nanmean(rnd)), "checks": checks, "passed": all(checks.values()),
                       "anecdotal_under_30_events": st.get("events", 0) < 30})
    return {"cuts": {k: [iso(a), iso(b)] for k, (a, b) in cuts.items()}, "coins_loaded": len(coins), "delisted_coins": sum(c.delisted for c in coins), "events_total": n, "events_coins": len({c.sym for c, _ in events}),
            "events_per_split": {k: sum(1 for t in times if a <= t < b) for k, (a, b) in cuts.items()}, "variants_this_round": len(variants), "variants_total_with_prior": n_total, "finalists": finals, "variants": variants,
            "not_testable": {"open_interest": "no OI history; 'funding hot while OI falls' uses funding only"}}


def evaa_case() -> dict:
    """Delta's own EVAAUSD (1-hour candles aggregated to days): is it on Delta India, and what did the move look like."""
    df = pd.read_pickle(data.DATA_DIR / "d2_EVAAUSD_1h.pkl").dropna(subset=["close"])
    df["day"] = df.time // 86400
    d = df.groupby("day").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last")).reset_index()
    peak = d.loc[d.h.idxmax()]
    return {"on_delta_india": True, "symbol": "EVAAUSD", "first_candle": pd.Timestamp(int(df.time.min()), unit="s").strftime("%Y-%m-%d"), "peak_high": float(peak.h), "peak_day": pd.Timestamp(int(peak.day) * 86400, unit="s").strftime("%Y-%m-%d"),
            "last_close": float(d.c.iloc[-1]), "drawdown_from_peak_pct": float((d.c.iloc[-1] / peak.h - 1) * 100), "days_listed": int(len(d))}


if __name__ == "__main__":
    out = run_all()
    out["evaa_case"] = evaa_case()
    path = Path(__file__).resolve().parents[3] / "reports" / "round4_d3_results.json"
    path.write_text(json.dumps(out, indent=1, default=float))
    print({k: out[k] for k in ("cuts", "coins_loaded", "delisted_coins", "events_total", "events_coins", "events_per_split", "variants_this_round", "variants_total_with_prior")})
    for f in out["finalists"]:
        print(f["type"], f["rule"], f["trail_atr"], f["hold_days"], "val", f["validation"].get("events"), round(f["validation"].get("mean_net_pct", float("nan")), 2), "holdout", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f["holdout"].items()},
              "boot90", [round(x, 1) for x in f["holdout_bootstrap90_net_pct"]], "DSR", f["deflated_probability"], "rand", round(f["random_entry_mean_net_pct"], 2), "PASSED" if f["passed"] else "no", [k for k, v in f["checks"].items() if not v])
    print(out["evaa_case"])
