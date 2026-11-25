"""A battery of published / well-known systematic strategies, tested on Delta India's own data (owner, 25 Nov 2026: "dig the internet ... test here").
Hypothetical research; decision support, not investment advice. No order path.

SOURCES the families come from:
  - Time-series momentum in crypto (Liu & Tsyvinski, "Risks and Returns of Cryptocurrency", RFS 2021): past 1-8 week returns predict future returns.
  - Cross-sectional momentum (Liu, Tsyvinski & Wu, "Common Risk Factors in Cryptocurrency"): long past winners / short losers, 1-4 week lookback, weekly.
  - Calendar effects (Baur et al. 2019; NYDIG 2021 "Temporalities of Bitcoin returns": Friday and start-of-week strength; hour-of-day effects unstable).
  - Funding carry (arXiv 2212.06888 "Fundamentals of Perpetual Futures"; arXiv 2510.14435): short high-funding perps, long low-funding ones.
  - Classic technical systems: moving-average crossovers, Donchian/Turtle breakouts, Connors RSI(2), Larry Williams volatility breakout,
    Supertrend, Bollinger-squeeze breakout, short-term cross-sectional reversal.
DATA: Delta's 15-minute candles and hourly funding (data/delta_15m, the 183 crypto perpetuals listed today, up to 2 years; survivorship bias).
  Single-asset systems run on BTC, ETH, SOL, XRP (equal weight across the four). Cross-sectional systems use, each day, the 20 most traded coins
  by median daily turnover over the prior 30 days (no look-ahead).
PORTFOLIO MATH: daily, close-to-close (UTC); weights set at a day's close earn the NEXT day's return; 1x gross exposure.
COSTS: every unit of turnover pays Delta taker 0.05% + 18% GST (5.9 bps) + slippage (2 bps on BTC/ETH/SOL/XRP, 10 bps on the cross-sectional
  universe); funding from Delta's series (a long pays a positive rate, a short receives it).
SPLITS by date: train 50%, validation 25%, holdout 25%. Per family the variant with the best validation Sharpe is chosen; holdout once."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .delta_pump_intraday import OUT as D15, funding_series
from .round4_d2 import boot_ci
from .run import REPORTS

MAJORS = ("BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD")
FEE = 5.9


def load_daily() -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, pd.DataFrame]]:
    uni = json.loads((D15 / "universe.json").read_text())
    daily, m15 = {}, {}
    fund = {}
    from .delta_all import DELTA_1M
    import httpx
    with httpx.Client(timeout=30) as cl:
        prods = {p["symbol"]: float(p["contract_value"]) for p in cl.get("https://api.india.delta.exchange/v2/products", params={"contract_types": "perpetual_futures"}).json()["result"]}
    srcs = {s: (D15 / f"{s}_15m.pkl", cv) for s, cv in uni.items()}
    for s in MAJORS:                                     # the majors come from the 1-minute set (the 15-minute download skipped them)
        srcs[s] = (DELTA_1M / f"{s}_1m.pkl", prods[s])
    for s, (path, cv) in srcs.items():
        d = pd.read_pickle(path)
        if len(d) < 96 * 60 and s not in MAJORS:
            continue
        d = d.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
        d["day"] = pd.to_datetime(d.time // 86400 * 86400, unit="s")
        g = d.groupby("day")
        x = pd.DataFrame({"o": g.open.first(), "h": g.high.max(), "l": g.low.min(), "c": g.close.last(), "turn": (d.volume * cv * d.close).groupby(d.day).sum(), "n": g.size()})
        daily[s] = x[x.n >= (1200 if s in MAJORS else 90)]
        if s in MAJORS:
            m15[s] = d
        f = funding_series(s)
        if len(f):
            fd = pd.Series(f.to_numpy() / 8, index=pd.to_datetime(f.index // 86400 * 86400, unit="s")).groupby(level=0).sum()
            fund[s] = fd
    close = pd.DataFrame({s: x.c for s, x in daily.items()}).sort_index()
    turn = pd.DataFrame({s: x.turn for s, x in daily.items()}).reindex(close.index)
    fnd = pd.DataFrame(fund).reindex(close.index).reindex(columns=close.columns).fillna(0.0)
    return daily, close, turn, fnd, m15


def run_weights(w: pd.DataFrame, close: pd.DataFrame, fnd: pd.DataFrame, slip: float) -> pd.Series:
    """Daily net return of a weight matrix (rows = day t decisions, applied to t+1)."""
    ret = close.pct_change().shift(-1)                      # the return of the NEXT day
    w = w.reindex(close.index).fillna(0.0)
    gross = (w * ret).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    funding = (w * fnd.shift(-1).reindex(w.index).fillna(0.0)).sum(axis=1)      # long pays positive funding
    net = gross - turnover * (FEE + slip) / 1e4 - funding
    return net.iloc[:-1]


def ew(sig: pd.DataFrame) -> pd.DataFrame:
    """Equal weight across the columns that have a signal (gross 1)."""
    n = sig.abs().sum(axis=1).replace(0, np.nan)
    return sig.div(n, axis=0).fillna(0.0)


def rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def strategies(daily, close, turn, fnd, m15) -> dict[str, tuple[pd.Series, str]]:
    out: dict[str, tuple[pd.Series, str]] = {}
    maj = close[list(MAJORS)].dropna(how="all")
    hi = pd.DataFrame({s: daily[s].h for s in MAJORS}).reindex(maj.index)
    lo = pd.DataFrame({s: daily[s].l for s in MAJORS}).reindex(maj.index)
    op = pd.DataFrame({s: daily[s].o for s in MAJORS}).reindex(maj.index)
    # 1 time-series momentum
    for n in (7, 14, 28, 56):
        r = maj / maj.shift(n) - 1
        out[f"TSMOM|{n}d|long-short"] = (run_weights(ew(np.sign(r).fillna(0)), close, fnd, 2), "TSMOM")
        out[f"TSMOM|{n}d|long-only"] = (run_weights(ew((r > 0).astype(float)), close, fnd, 2), "TSMOM")
    # 2 moving-average crossover
    for f_, s_ in ((10, 30), (20, 100), (50, 200)):
        sig = np.sign(maj.rolling(f_).mean() - maj.rolling(s_).mean()).fillna(0)
        out[f"MACROSS|{f_}/{s_}|long-short"] = (run_weights(ew(sig), close, fnd, 2), "MACROSS")
        out[f"MACROSS|{f_}/{s_}|long-only"] = (run_weights(ew(sig.clip(lower=0)), close, fnd, 2), "MACROSS")
    # 3 Donchian / Turtle
    for n_in, n_out in ((20, 10), (55, 20)):
        up_, dn_ = hi.rolling(n_in).max().shift(1), lo.rolling(n_in).min().shift(1)
        xu, xd = lo.rolling(n_out).min().shift(1), hi.rolling(n_out).max().shift(1)
        pos = pd.DataFrame(0.0, index=maj.index, columns=maj.columns)
        for s in maj.columns:
            p, arr = 0.0, []
            for t in maj.index:
                c = maj.at[t, s]
                if np.isnan(c):
                    arr.append(0.0); continue
                if p == 0 and c > up_.at[t, s]:
                    p = 1.0
                elif p == 0 and c < dn_.at[t, s]:
                    p = -1.0
                elif p > 0 and c < xu.at[t, s]:
                    p = 0.0
                elif p < 0 and c > xd.at[t, s]:
                    p = 0.0
                arr.append(p)
            pos[s] = arr
        out[f"DONCHIAN|{n_in}/{n_out}|long-short"] = (run_weights(pos / len(MAJORS), close, fnd, 2), "DONCHIAN")
        out[f"DONCHIAN|{n_in}/{n_out}|long-only"] = (run_weights(pos.clip(lower=0) / len(MAJORS), close, fnd, 2), "DONCHIAN")
    # 4 Connors RSI(2)
    for lo_t, hi_t in ((10, 90), (5, 95)):
        pos = pd.DataFrame(0.0, index=maj.index, columns=maj.columns)
        for s in maj.columns:
            c = maj[s]
            r2, ma200, ma5 = rsi(c, 2), c.rolling(200).mean(), c.rolling(5).mean()
            p, arr = 0.0, []
            for t in c.index:
                if p == 0 and r2[t] < lo_t and c[t] > ma200[t]:
                    p = 1.0
                elif p == 0 and r2[t] > hi_t and c[t] < ma200[t]:
                    p = -1.0
                elif (p > 0 and c[t] > ma5[t]) or (p < 0 and c[t] < ma5[t]):
                    p = 0.0
                arr.append(p)
            pos[s] = arr
        out[f"RSI2|{lo_t}/{hi_t}"] = (run_weights(pos / len(MAJORS), close, fnd, 2), "RSI2")
    # 5 cross-sectional momentum and reversal (top-20 liquid)
    liq = turn.rolling(30, min_periods=20).median().shift(1)
    top = liq.rank(axis=1, ascending=False) <= 20
    for lb in (7, 14, 21, 28):
        r = (close / close.shift(lb) - 1).where(top)
        rk = r.rank(axis=1, pct=True)
        sig = ((rk >= 0.8).astype(float) - (rk <= 0.2).astype(float)).where(top, 0.0)
        weekly = sig.copy()
        weekly.loc[sig.index.dayofweek != 0] = np.nan                 # rebalance on Mondays only
        weekly = weekly.ffill().fillna(0)
        out[f"XSMOM|{lb}d|weekly"] = (run_weights(ew(weekly), close, fnd, 10), "XSMOM")
    r1 = (close / close.shift(1) - 1).where(top)
    rk = r1.rank(axis=1, pct=True)
    out["XSREV|1d|daily"] = (run_weights(ew(((rk <= 0.2).astype(float) - (rk >= 0.8).astype(float)).where(top, 0.0)), close, fnd, 10), "XSREV")
    # 6 funding carry (top-20 liquid): short highest trailing funding, long lowest
    for lb in (3, 7):
        fr = fnd.rolling(lb).mean().where(top)
        rk = fr.rank(axis=1, pct=True)
        sig = ((rk <= 0.2).astype(float) - (rk >= 0.8).astype(float)).where(top, 0.0)
        weekly = sig.copy()
        weekly.loc[sig.index.dayofweek != 0] = np.nan
        weekly = weekly.ffill().fillna(0)
        out[f"CARRY|{lb}d|weekly"] = (run_weights(ew(weekly), close, fnd, 10), "CARRY")
    # 7 calendar: long the majors only on given weekdays (NYDIG: Friday and Monday strength)
    for days, name in (((4,), "Fri"), ((0,), "Mon"), ((0, 4), "Mon+Fri"), ((5, 6), "weekend")):
        hold = pd.Series(np.isin((maj.index + pd.Timedelta(days=1)).dayofweek, days), index=maj.index)   # weights at t earn day t+1
        out[f"CALENDAR|long {name}"] = (run_weights(ew(pd.DataFrame({s: hold.astype(float) for s in MAJORS})), close, fnd, 2), "CALENDAR")
    # 8 Larry Williams volatility breakout (intraday, majors): long above open + k x yesterday's range, short below open - k x range, out at the close
    for k in (0.5, 0.7):
        rets = {}
        for s in MAJORS:
            d = m15[s]
            x = daily[s]
            rng = (x.h - x.l).shift(1)
            pnl = {}
            for day, g in d.groupby("day"):
                if day not in rng.index or not np.isfinite(rng[day]):
                    continue
                up_l, dn_l = g.open.iloc[0] + k * rng[day], g.open.iloc[0] - k * rng[day]
                hit_u = np.flatnonzero(g.high.to_numpy() >= up_l)
                hit_d = np.flatnonzero(g.low.to_numpy() <= dn_l)
                first = min(hit_u[0] if len(hit_u) else 10**9, hit_d[0] if len(hit_d) else 10**9)
                if first == 10**9:
                    pnl[day] = 0.0
                    continue
                side = 1 if (len(hit_u) and hit_u[0] == first) else -1
                entry = up_l if side > 0 else dn_l
                pnl[day] = side * (g.close.iloc[-1] / entry - 1) - 2 * (FEE + 2) / 1e4
            rets[s] = pd.Series(pnl)
        out[f"WILLIAMS|k={k}"] = (pd.DataFrame(rets).mean(axis=1).sort_index(), "WILLIAMS")
    # 9 Supertrend (daily, ATR 10, x3) and Bollinger squeeze breakout (daily)
    for mult in (2.0, 3.0):
        pos = pd.DataFrame(0.0, index=maj.index, columns=maj.columns)
        for s in MAJORS:
            c, h, l_ = maj[s], hi[s], lo[s]
            atr = pd.concat([h - l_, (h - c.shift()).abs(), (l_ - c.shift()).abs()], axis=1).max(axis=1).ewm(alpha=1 / 10, adjust=False).mean()
            mid = (h + l_) / 2
            ub, lb_ = mid + mult * atr, mid - mult * atr
            fu, fl, trend, arr = np.nan, np.nan, 1, []
            for i, t in enumerate(c.index):
                if i == 0 or np.isnan(atr[t]):
                    arr.append(0.0); continue
                fu = ub[t] if (np.isnan(fu) or ub[t] < fu or c.iloc[i - 1] > fu) else fu
                fl = lb_[t] if (np.isnan(fl) or lb_[t] > fl or c.iloc[i - 1] < fl) else fl
                trend = 1 if c[t] > fu else -1 if c[t] < fl else trend
                arr.append(float(trend))
            pos[s] = arr
        out[f"SUPERTREND|10,{mult:g}|long-short"] = (run_weights(pos / len(MAJORS), close, fnd, 2), "SUPERTREND")
        out[f"SUPERTREND|10,{mult:g}|long-only"] = (run_weights(pos.clip(lower=0) / len(MAJORS), close, fnd, 2), "SUPERTREND")
    pos = pd.DataFrame(0.0, index=maj.index, columns=maj.columns)
    for s in MAJORS:
        c = maj[s]
        mid, sd = c.rolling(20).mean(), c.rolling(20).std()
        width = (4 * sd / mid)
        squeeze = (width <= width.rolling(180, min_periods=60).quantile(0.2)).shift(1).fillna(False)
        p, arr = 0.0, []
        for t in c.index:
            if p == 0 and squeeze[t] and c[t] > mid[t] + 2 * sd[t]:
                p = 1.0
            elif p == 0 and squeeze[t] and c[t] < mid[t] - 2 * sd[t]:
                p = -1.0
            elif (p > 0 and c[t] < mid[t]) or (p < 0 and c[t] > mid[t]):
                p = 0.0
            arr.append(p)
        pos[s] = arr
    out["BBSQUEEZE|20,2|exit mid"] = (run_weights(pos / len(MAJORS), close, fnd, 2), "BBSQUEEZE")
    return out


def perf(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 20:
        return {"days": int(len(r))}
    eq = (1 + r).cumprod()
    return {"days": int(len(r)), "ann_return_pct": float((eq.iloc[-1] ** (365 / len(r)) - 1) * 100), "sharpe": float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else None,
            "max_dd_pct": float((1 - eq / eq.cummax()).max() * 100), "days_active_pct": float((r != 0).mean() * 100)}


def main() -> dict:
    daily, close, turn, fnd, m15 = load_daily()
    strat = strategies(daily, close, turn, fnd, m15)
    idx = close[list(MAJORS)].dropna(how="all").index
    c1, c2 = idx[int(len(idx) * 0.5)], idx[int(len(idx) * 0.75)]
    res = {"variants_added": len(strat), "cuts": [str(c1.date()), str(c2.date())], "variants": {}, "chosen": {}}
    for name, (r, fam) in strat.items():
        r = r.sort_index()
        res["variants"][name] = {"train": perf(r[r.index < c1]), "validation": perf(r[(r.index >= c1) & (r.index < c2)])}
    for fam in sorted({f for _r, f in strat.values()}):
        names = [n for n, (_r, f) in strat.items() if f == fam]
        best = max(names, key=lambda n: res["variants"][n]["validation"].get("sharpe") or -1e9)
        r = strat[best][0].sort_index()
        h = r[r.index >= c2]
        lo, hi = boot_ci(h.dropna().to_numpy() * 1e4) if len(h) > 20 else (None, None)
        res["chosen"][fam] = {"variant": best, "holdout": perf(h), "holdout_daily_mean_bps_boot90": [lo, hi]}
    (REPORTS / "delta_runs").mkdir(parents=True, exist_ok=True)
    (REPORTS / "delta_runs" / "battery.json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    o = main()
    print("cuts", o["cuts"])
    for n, v in o["variants"].items():
        print(f"{n:34s} train SR {v['train'].get('sharpe') or 0:5.2f} ann {v['train'].get('ann_return_pct', 0):6.1f}% | val SR {v['validation'].get('sharpe') or 0:5.2f} ann {v['validation'].get('ann_return_pct', 0):6.1f}%")
    for f, c in o["chosen"].items():
        h = c["holdout"]
        print(f"HOLDOUT {f:11s} {c['variant']:32s} SR {h.get('sharpe') or 0:5.2f} ann {h.get('ann_return_pct', 0):6.1f}% maxDD {h.get('max_dd_pct', 0):5.1f}%  boot90 daily bps {c['holdout_daily_mean_bps_boot90']}")
