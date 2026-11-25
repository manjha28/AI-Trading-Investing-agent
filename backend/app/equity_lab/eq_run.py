"""Part 2 of the 2026-11-26 literature round: NSE swing and positional strategies on DAILY Dhan bars. Hypothetical; decision support, not investment advice.
Long-only cash-segment strategies (no shorting of delivery equity). COSTS per side, as a fraction of traded value: delivery STT 0.1% (buy and sell), exchange transaction 0.00297%, SEBI 0.0001%,
stamp duty 0.015% on the buy, GST 18% on exchange+SEBI charges, brokerage assumed Rs 0 for delivery (please verify your Dhan plan), plus 10 bps slippage per side: about 21 bps per side on average,
42 bps per round trip. Seasonality (Nifty, futures-style costs) uses 5 bps per round trip.
Signals use completed daily bars; a signal on day t is entered at that day's close and held over t+1. Positions are equal weight with 20 slots (a slot is 5% of capital; more than 20 open scale down).
Universe: 264 stocks in the app database, ranked by turnover (see eq_data). Split 50/25/25 of 1 Mar 2022 to 23 Nov 2026 (train to 12 Jul 2024, validation to 16 Sep 2025, holdout after).
Definitions are the textbook forms (Jegadeesh-Titman 12-1 momentum, George-Hwang 52-week-high, relative strength vs Nifty, 200-day trend, Turtle 55/20 Donchian, Connors RSI(2)/IBS/down-days,
Crabel NR7/inside bar, Bollinger squeeze); I did not read primary sources for these."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from app.delta_lab import lit_core as K
from app.delta_lab.round4_d2 import boot_ci

from . import eq_data as ED

DF = pd.DataFrame
COST_EQ = 0.0021
ANN = 248
SLOTS = 20
REPORTS = ED.Path(__file__).resolve().parents[3] / "reports"


class Ctx:
    def __init__(self):
        P = ED.panel()
        self.P = P
        self.O, self.H, self.L, self.C, self.V = P["open"], P["high"], P["low"], P["close"], P["volume"]
        self.T, self.N = self.C.shape
        self.t = P["t"]
        self.rank = P["rank"]
        self.uni = np.isfinite(self.rank) & (self.rank <= 200)
        self.large = np.isfinite(self.rank) & (self.rank <= 50)
        self.hist = DF(np.isfinite(self.C).astype(float)).rolling(260, min_periods=1).sum().to_numpy()
        self.elig = self.uni & np.isfinite(self.C) & (self.hist >= 252)
        self.cuts = K.split_idx(self.t, "2022-03-01", None)
        self.F = np.zeros_like(self.C)
        nf = P["nifty"]
        self.nifty_down = (pd.Series(nf) / pd.Series(nf).shift(30) - 1 < -0.05).fillna(False).to_numpy()
        self.sma50 = self.sma(50)
        self.sma200 = self.sma(200)
        self.avgv = DF(self.V).rolling(20, min_periods=15).mean().shift(1).to_numpy()

    def sma(self, n):
        return DF(self.C).rolling(n, min_periods=n).mean().to_numpy()

    def sh(self, a, k):
        out = np.full_like(a, np.nan)
        out[k:] = a[:-k]
        return out


def time_events(x: Ctx, trig: np.ndarray, hold: int, stop: np.ndarray | None = None) -> np.ndarray:
    cur, held = np.zeros(x.N), np.zeros(x.N)
    sl = np.full(x.N, np.nan)
    pos = np.zeros((x.T, x.N))
    for t in range(x.T):
        held = np.where(cur > 0, held + 1, 0.0)
        ex = held >= hold
        if stop is not None:
            with np.errstate(invalid="ignore"):
                ex |= (cur > 0) & (x.C[t] < sl)
        cur, held = np.where(ex, 0.0, cur), np.where(ex, 0.0, held)
        ent = (cur == 0) & trig[t] & x.elig[t]
        cur = np.where(ent, 1.0, cur)
        if stop is not None:
            sl = np.where(ent, stop[t], sl)
        held = np.where(ent, 0.0, held)
        pos[t] = cur
    return pos / np.maximum(pos.sum(1, keepdims=True), SLOTS)


def state_pos(x: Ctx, cond: np.ndarray, volok: np.ndarray | None) -> np.ndarray:
    cur = np.zeros(x.N)
    pos = np.zeros((x.T, x.N))
    for t in range(x.T):
        enter = x.elig[t] if volok is None else (x.elig[t] & volok[t])
        cur = np.where(cond[t] & ((cur > 0) | enter), 1.0, 0.0)
        pos[t] = cur
    return pos / np.maximum(pos.sum(1, keepdims=True), SLOTS)


def donchian(x: Ctx, volok):
    hi = DF(x.H).rolling(55, min_periods=55).max().shift(1).to_numpy()
    lo = DF(x.L).rolling(20, min_periods=20).min().shift(1).to_numpy()
    cur = np.zeros(x.N)
    pos = np.zeros((x.T, x.N))
    for t in range(x.T):
        with np.errstate(invalid="ignore"):
            en = (x.C[t] > hi[t]) & x.elig[t] & (True if volok is None else volok[t])
            ex = x.C[t] < lo[t]
        cur = np.where(en, 1.0, np.where(ex, 0.0, cur))
        pos[t] = cur
    return pos / np.maximum(pos.sum(1, keepdims=True), SLOTS)


def monthly(x: Ctx, score: np.ndarray, n: int) -> np.ndarray:
    d = pd.to_datetime(x.t, unit="s")
    month_end = np.r_[d[1:].month != d[:-1].month, False]
    W = np.zeros((x.T, x.N))
    cur = np.zeros(x.N)
    for t in range(x.T):
        if month_end[t] and t >= 260:
            s = np.where(x.elig[t], score[t], np.nan)
            ok = np.flatnonzero(np.isfinite(s))
            cur = np.zeros(x.N)
            if len(ok) >= n:
                cur[ok[np.argsort(-s[ok])[:n]]] = 1.0 / n
        W[t] = cur
    return W


def rsi2(x: Ctx):
    ch = DF(x.C).diff()
    up, dn = ch.clip(lower=0), (-ch).clip(lower=0)
    au, ad = up.ewm(alpha=0.5, adjust=False, min_periods=2).mean(), dn.ewm(alpha=0.5, adjust=False, min_periods=2).mean()
    return (100 - 100 / (1 + au / ad.replace(0, np.nan))).to_numpy()


def add(x: Ctx, store, rows, fam, name, params, W):
    res = K.run(W, x.C, x.F, 1, cost=COST_EQ)
    rows.append({"family": fam, "name": name, "params": params, "eval": K.evaluate(res, x.cuts, 1, ANN), "key": len(rows)})
    store[len(rows) - 1] = res


def run_all():
    x = Ctx()
    rows, store = [], {}
    nf = x.P["nifty"][:, None]
    # 2.1 momentum
    scores = {"12-1 month momentum": x.sh(x.C, 21) / x.sh(x.C, 252) - 1, "52-week-high proximity": x.C / DF(x.H).rolling(252, min_periods=200).max().to_numpy(),
              "relative strength 6m vs Nifty": (x.C / x.sh(x.C, 126) - 1) - (nf / x.sh(nf, 126) - 1), "relative strength 12m vs Nifty": (x.C / x.sh(x.C, 252) - 1) - (nf / x.sh(nf, 252) - 1)}
    for nm, sc in scores.items():
        for n in (10, 20, 30):
            add(x, store, rows, "momentum", f"{nm} | top {n} of the turnover-ranked top 200, monthly", dict(score=nm, n=n), monthly(x, sc, n))
    # 2.2 trend filters
    volok = x.V > 1.5 * x.avgv
    for vf in (False, True):
        v = volok if vf else None
        tag = " + volume > 1.5x its 20-day average at entry" if vf else ""
        add(x, store, rows, "trend", f"Close above the 200-day average{tag}", dict(volume_filter=vf), state_pos(x, np.nan_to_num(x.C > x.sma200).astype(bool), v))
        add(x, store, rows, "trend", f"50/200 golden cross{tag}", dict(volume_filter=vf), state_pos(x, np.nan_to_num(x.sma50 > x.sma200).astype(bool), v))
        add(x, store, rows, "trend", f"Donchian 55/20 (Turtle){tag}", dict(volume_filter=vf), donchian(x, v))
    # 2.3 mean reversion
    up = np.nan_to_num(x.C > x.sma200).astype(bool)
    r2 = rsi2(x)
    ibs = (x.C - x.L) / np.where(x.H > x.L, x.H - x.L, np.nan)
    dn3 = (x.C < x.sh(x.C, 1)) & (x.sh(x.C, 1) < x.sh(x.C, 2)) & (x.sh(x.C, 2) < x.sh(x.C, 3))
    for hold in (2, 5):
        add(x, store, rows, "mean_reversion", f"RSI(2) below 10 in an uptrend (above 200-day) | hold {hold}d", dict(hold=hold), time_events(x, np.nan_to_num(r2 < 10).astype(bool) & up, hold))
        for ut in (True, False):
            tag = "in an uptrend" if ut else "no trend filter"
            add(x, store, rows, "mean_reversion", f"IBS below 0.2, {tag} | hold {hold}d", dict(hold=hold, uptrend=ut), time_events(x, np.nan_to_num(ibs < 0.2).astype(bool) & (up if ut else True), hold))
            add(x, store, rows, "mean_reversion", f"Buy after 3 down days, {tag} | hold {hold}d", dict(hold=hold, uptrend=ut), time_events(x, np.nan_to_num(dn3).astype(bool) & (up if ut else True), hold))
    # 2.4 gaps
    gap = x.O / x.sh(x.C, 1) - 1
    for g in (0.02, 0.04):
        for hold in (5, 10):
            trig = np.nan_to_num(gap >= g).astype(bool) & np.nan_to_num(x.V >= 3 * x.avgv).astype(bool)
            add(x, store, rows, "gap", f"Gap up >= {int(g * 100)}% on volume >= 3x average, enter at the close | hold {hold}d", dict(gap=g, hold=hold), time_events(x, trig, hold))
    # 2.5 volatility contraction
    rng = x.H - x.L
    nr7 = np.nan_to_num(rng <= DF(rng).rolling(7, min_periods=7).min().to_numpy()).astype(bool)
    inside = np.nan_to_num((x.H < x.sh(x.H, 1)) & (x.L > x.sh(x.L, 1))).astype(bool)
    ma20, sd20 = x.sma(20), DF(x.C).rolling(20, min_periods=20).std().to_numpy()
    width = 4 * sd20 / ma20
    thr = DF(width).rolling(126, min_periods=90).quantile(0.10).shift(1).to_numpy()
    sq = np.nan_to_num(width <= thr).astype(bool)
    armed = DF(sq.astype(float)).rolling(5, min_periods=1).max().to_numpy() > 0
    hi20 = DF(x.H).rolling(20, min_periods=20).max().shift(1).to_numpy()
    setups = {"NR7 then close above the setup high": (nr7, True), "Inside bar then close above the setup high": (inside, True)}
    for nm, (st, _) in setups.items():
        sH = x.sh(np.where(st, x.H, np.nan), 1)
        sL = x.sh(np.where(st, x.L, np.nan), 1)
        for hold in (5, 10):
            add(x, store, rows, "vol_contraction", f"{nm} (stop: close below the setup low) | hold {hold}d", dict(setup=nm, hold=hold), time_events(x, np.nan_to_num(x.C > sH).astype(bool), hold, stop=sL))
    for hold in (5, 10):
        add(x, store, rows, "vol_contraction", f"Bollinger squeeze (width in bottom 10% of 6 months) then close above the upper band | hold {hold}d", dict(hold=hold), time_events(x, armed & np.nan_to_num(x.C > ma20 + 2 * sd20).astype(bool), hold))
        trig = np.nan_to_num(x.C > hi20).astype(bool) & np.nan_to_num(x.V > 1.5 * x.avgv).astype(bool) & np.nan_to_num(x.V > x.sh(x.V, 1)).astype(bool)
        add(x, store, rows, "vol_contraction", f"20-day high on rising volume (>1.5x average and above yesterday) | hold {hold}d", dict(hold=hold), time_events(x, trig, hold))
    return x, rows, store


def seasonality() -> list[dict]:
    s = ED.panel()["nsei"]
    s = s[s.index >= pd.Timestamp("2015-01-01").date()]
    d = pd.to_datetime(s.index)
    r = (s.close / s.close.shift(1) - 1).to_numpy() * 1e4
    n = len(r)
    month = d.to_period("M")
    pos_in_month = pd.Series(np.arange(n)).groupby(month).cumcount().to_numpy()
    size = pd.Series(np.ones(n)).groupby(month).transform("sum").to_numpy()
    last, first3 = pos_in_month == size - 1, pos_in_month < 3
    dow = d.dayofweek.to_numpy()
    # expiry day: last Thursday of the month until Aug 2025, last Tuesday after
    exp = np.zeros(n, bool)
    for m, g in pd.Series(np.arange(n)).groupby(month):
        idx = g.to_numpy()
        want = 1 if d[idx[0]] >= pd.Timestamp("2025-09-01") else 3
        c = [i for i in idx if dow[i] == want]
        if c:
            exp[c[-1]] = True
    expiry_week = np.zeros(n, bool)
    for i in np.flatnonzero(exp):
        j = i
        while j >= 0 and (i - j) < 4 and (d[j].isocalendar().week == d[i].isocalendar().week):
            expiry_week[j] = True
            j -= 1
    wins = {"Monday": dow == 0, "Tuesday": dow == 1, "Wednesday": dow == 2, "Thursday": dow == 3, "Friday": dow == 4,
            "Turn of month (last trading day + first 3 of the next month)": last | first3, "Expiry week (Monday to expiry day)": expiry_week}
    a, b, c, e = K.split_idx(np.arange(n), "0", None) if False else (0, n // 2, n * 3 // 4, n)
    out = []
    for nm, m in wins.items():
        # a trade = a contiguous run of window days
        edges = np.flatnonzero(np.diff(np.r_[0, m.astype(int), 0]) != 0)
        runs = [(i, j) for i, j in zip(edges[0::2], edges[1::2])]
        for sign, lab in ((1, "long"), (-1, "short")):
            tr = np.array([sign * np.nansum(r[i:j]) - 5.0 for i, j in runs])
            st = np.array([i for i, j in runs])
            rec = {"effect": nm, "direction": lab}
            for pn, lo, hi in (("train", a, b), ("validation", b, c), ("holdout", c, e)):
                x = tr[(st >= lo) & (st < hi)]
                if len(x):
                    w, l_ = x[x > 0], x[x <= 0]
                    rec[pn] = {"trades": int(len(x)), "net_bps": round(float(x.mean()), 1), "win": round(float((x > 0).mean()), 3), "pf": round(float(w.sum() / -l_.sum()), 2) if l_.sum() < 0 else None,
                               "t": round(float(x.mean() / x.std() * np.sqrt(len(x))), 1) if x.std() > 0 else None}
                else:
                    rec[pn] = {"trades": 0}
            out.append(rec)
    out.append({"span": f"{s.index[0]} to {s.index[-1]}", "cuts": [str(d[0].date()), str(d[b].date()), str(d[c].date())]})
    return out


def main():
    x, rows, store = run_all()
    seas = seasonality()
    n_this = len(rows) + len(seas) - 1
    D = json.load(open(REPORTS / "literature_delta.json"))
    n_total = D["n_total"] + n_this
    srs = np.array([r["eval"]["validation"].get("sharpe", 0) / np.sqrt(ANN) for r in rows if r["eval"]["validation"].get("trades", 0) >= 8])
    var_sr = float(srs.var())
    ts = pd.to_datetime(x.t, unit="s")
    year = ts.year.to_numpy()
    out = {"n_variants": n_this, "n_total": n_total, "var_sr_daily": var_sr, "n_jumps": x.P["n_jumps"], "rows": [], "best": {}, "seasonality": seas,
           "bench": {}, "cuts": [str(ts[min(i, len(ts) - 1)].date()) for i in x.cuts]}
    nf = x.P["nifty"]
    for nm, lo, hi in (("train", x.cuts[0], x.cuts[1]), ("validation", x.cuts[1], x.cuts[2]), ("holdout", x.cuts[2], x.cuts[3])):
        rr = nf[lo + 1:hi] / nf[lo:hi - 1] - 1
        eq = np.cumsum(rr)
        out["bench"][nm] = {"ann_ret_pct": round(float(rr.mean() * ANN * 100), 1), "sharpe": round(float(rr.mean() / rr.std() * np.sqrt(ANN)), 2), "max_dd_pct": round(float((np.maximum.accumulate(eq) - eq).max() * 100), 1)}
    for r in rows:
        item = {k: r[k] for k in ("family", "name", "params", "eval", "key")}
        e = r["eval"]
        if e["validation"].get("net_bps", -1) > 0 and e["holdout"].get("net_bps", -1) > 0 and e["holdout"].get("trades", 0) >= 5:
            ep = store[r["key"]]["ep"]
            h = ep[(ep.t >= x.cuts[2]) & (ep.t < x.cuts[3])]
            lo, hi = boot_ci(h.bps.to_numpy())
            item["boot_holdout"] = [round(lo, 1), round(hi, 1)]
        out["rows"].append(item)
    for fam in dict.fromkeys(r["family"] for r in rows):
        rs = [r for r in rows if r["family"] == fam and r["eval"]["validation"].get("trades", 0) >= 8]
        rs.sort(key=lambda r: -r["eval"]["validation"].get("sharpe", -1e9))
        best = []
        for r in rs[:3]:
            res = store[r["key"]]
            ep = res["ep"]
            ex = K.holdout_extras(res, x.cuts, 1, x.P["names"], x.nifty_down)
            ex["dsr"] = K.dsr_prob(res["net"][x.cuts[2]: x.cuts[3]], n_total, var_sr)
            ep = ep.assign(year=year[ep.t.to_numpy()], grp=np.where(x.large[ep.t.to_numpy(), ep.coin.to_numpy()], "large (top 50)", "mid (51-200)"))
            ex["per_year"] = {int(k): {"trades": int(len(g)), "net_bps": round(float(g.bps.mean()), 1)} for k, g in ep.groupby("year")}
            ex["by_size"] = {k: {"trades": int(len(g)), "net_bps": round(float(g.bps.mean()), 1)} for k, g in ep[ep.t >= x.cuts[2]].groupby("grp")}
            ex["by_size_all"] = {k: {"trades": int(len(g)), "net_bps": round(float(g.bps.mean()), 1)} for k, g in ep.groupby("grp")}
            ex["nifty_down5_days"] = ex.pop("btc_down20")
            best.append({"name": r["name"], "eval": r["eval"], "extras": ex})
        out["best"][fam] = best
    (REPORTS / "literature_equity.json").write_text(json.dumps(out, indent=1, default=str))
    print(n_this, n_total, "done")


if __name__ == "__main__":
    main()
