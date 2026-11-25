"""Parts 1.5 (hour/weekday effects), 1.6 (pairs) and 1.7 (exit variants on the FROZEN R3 entry) on Delta data. Hypothetical; decision support, not investment advice.
1.6 method (statarb-crypto style, rebuilt, its README was not fetched): among the 20 most liquid coins with full train coverage, an Engle-Granger test on log prices in TRAIN (residual ADF t below
-3.37, one lag) and a stability test (both halves of train below -2.9, hedge ratios within 50%) select pairs. Trading: 90-day rolling hedge ratio, z of the spread over 20/30/60 days, enter |z|>2,
exit at z=0, stop at |z|>3.5, time stop 7 days, both legs paying Delta costs and funding. 1.7: the R3 entry exactly as frozen (hash eaea623c741f: 1h bars, 24-bar range in the lowest 10% of its last
1440 bars, stop-entry both sides valid 60 minutes, stop at the other side of the range, 3R target, 48h time stop) on Delta 1-minute data for BTC and ETH; only the exits change. The earlier list of
exit variants was not available to me, so these are my own: break-even, ATR trails, time stops, reversal exits, a partial."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import lit_core as K
from . import lit_data as LD
from .lit_xs import XP

DF = pd.DataFrame
FEE = 0.0005 * 1.18 * 1e4
MAKER = 0.0002 * 1.18 * 1e4
SLIP = 2.0


def adf_t(e: np.ndarray) -> float:
    de, lag = np.diff(e), e[:-1]
    y, X = de[1:], np.column_stack([lag[1:], de[:-1], np.ones(len(de) - 1)])
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    r = y - X @ b
    s2 = r @ r / (len(y) - 3)
    cov = s2 * np.linalg.inv(X.T @ X)
    return float(b[0] / np.sqrt(cov[0, 0]))


def eg(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    X = np.column_stack([b, np.ones(len(b))])
    beta = np.linalg.lstsq(X, a, rcond=None)[0]
    return adf_t(a - X @ beta), float(beta[0])


def run_pairs(store: dict, rows: list, pair_rows: list) -> None:
    x = XP()
    a, b, c, d = x.cuts
    train = slice(a, b)
    valid = np.isfinite(x.C[train]).all(0) & (x.turn30[train].mean(0) > 0)
    liq = np.where(valid)[0]
    liq = liq[np.argsort(-np.nanmean(x.D["TO"][train][:, liq], 0))][:20]
    lc = np.log(x.C)
    mid = a + (b - a) // 2
    chosen = []
    ntest = 0
    for i in range(len(liq)):
        for j in range(i + 1, len(liq)):
            ntest += 1
            ia, ib = liq[i], liq[j]
            t_all, beta = eg(lc[train, ia], lc[train, ib])
            if t_all > -3.37:
                continue
            t1, b1 = eg(lc[a:mid, ia], lc[a:mid, ib])
            t2, b2 = eg(lc[mid:b, ia], lc[mid:b, ib])
            if t1 < -2.9 and t2 < -2.9 and abs(b1 - b2) <= 0.5 * abs(beta):
                chosen.append((ia, ib, t_all))
    pair_rows.append({"tested": ntest, "selected": [(str(x.names[i]), str(x.names[j]), round(t, 2)) for i, j, t in chosen], "liquid_pool": [str(x.names[i]) for i in liq]})
    named = []
    for u, v in (('BTCUSD', 'ETHUSD'), ('SOLUSD', 'ETHUSD')):
        ia, ib = list(x.names).index(u), list(x.names).index(v)
        t_all, _ = eg(lc[train, ia], lc[train, ib])
        named.append((ia, ib, t_all))
    pair_rows[0]['named_train_adf'] = [(str(x.names[i]), str(x.names[j]), round(t, 2)) for i, j, t in named]
    for setname, pairs_ in (('train-selected', chosen), ('owner-named BTC/ETH and SOL/ETH', named)):
        for lb in (20, 30, 60):
            Wtot = np.zeros_like(x.C)
            eps = []
            e2 = []
            for ia, ib, _ in pairs_:
                sa, sb = lc[:, ia], lc[:, ib]
                beta = np.full(len(sa), np.nan)
                for t in range(90, len(sa)):
                    w = slice(t - 89, t + 1)
                    if np.isfinite(sa[w]).all() and np.isfinite(sb[w]).all():
                        X = np.column_stack([sb[w], np.ones(90)])
                        beta[t] = np.linalg.lstsq(X, sa[w], rcond=None)[0][0]
                s = sa - beta * sb
                z = (s - DF(s).rolling(lb).mean()[0].to_numpy()) / DF(s).rolling(lb).std()[0].to_numpy()
                pos, held, start = 0, 0, 0
                Wp = np.zeros_like(x.C)
                for t in range(len(z)):
                    if pos != 0:
                        held += 1
                        if (pos > 0 and z[t] >= 0) or (pos < 0 and z[t] <= 0) or abs(z[t]) > 3.5 or held >= 7:
                            eps.append((ia, start, pos, t))
                            pos = 0
                    if pos == 0 and np.isfinite(z[t]) and abs(z[t]) > 2 and t >= a:
                        pos, held, start = (-1 if z[t] > 0 else 1), 0, t
                    if pos != 0:
                        g = 1 + abs(beta[t])
                        Wp[t, ia], Wp[t, ib] = pos / g, -pos * beta[t] / g
                    else:
                        Wp[t, ia] = Wp[t, ib] = 0
                Wtot += Wp / max(len(pairs_), 1)
                rp = K.run(Wp, x.C, x.F, 1)["net"]                    # this pair alone, gross notional 1: its trades' net, both legs, costs and funding included
                for ia_, st, pos_, en in [e for e in eps if e[0] == ia]:
                    e2.append((ia_, st, pos_, rp[st + 1: en + 2].sum() * 1e4, 0.0, 0.0, en - st))
                eps = [e for e in eps if e[0] != ia]
            res = K.run(Wtot, x.C, x.F, 1)
            res["ep"] = DF(e2, columns=["coin", "t", "side", "bps", "gross_bps", "fund_bps", "bars"])
            rows.append({"family": "pairs", "name": f"pairs {setname} ({len(pairs_)} pairs) | z lookback {lb}d | entry 2, exit 0, stop 3.5, 7d", "params": dict(lookback=lb, pairs=len(pairs_)),
                         "eval": K.evaluate(res, x.cuts, 1), "turn_per_day": round(float(res["turn"].mean()), 3), "key": len(rows)})
            store[len(rows) - 1] = (res, x)




def run_tod(out: dict) -> None:
    for sym in ("BTCUSD", "ETHUSD", "SOLUSD"):
        d = pd.read_pickle(LD.ROOT / "delta_1m" / f"{sym}_1m.pkl").drop_duplicates("time").sort_values("time")
        d = d[(d.time >= pd.Timestamp("2024-04-10", tz="UTC").timestamp()) & (d.time < LD.T1)]
        h = d.groupby(d.time // 3600 * 3600).agg(o=("open", "first"), c=("close", "last"))
        r = (h.c / h.o - 1) * 1e4
        secs = r.index.to_numpy()
        r.index = pd.to_datetime(r.index, unit="s", utc=True)
        a, b, c, e = K.split_idx(secs, "2024-04-10", None)
        per = {"train": r.iloc[a:b], "validation": r.iloc[b:c], "holdout": r.iloc[c:e]}
        rec = {}
        for key, f in (("hour", lambda s: s.index.hour), ("weekday", lambda s: s.index.dayofweek)):
            tab = {}
            for pn, s in per.items():
                g = s.groupby(f(s))
                tab[pn] = {int(k): (round(float(v.mean()), 2), round(float(v.mean() / v.std() * np.sqrt(len(v))), 1)) for k, v in g}
            rec[key] = tab
        out[sym] = rec
