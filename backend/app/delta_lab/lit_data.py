"""Delta India panel for the 2026-11-26 literature round: 4h and daily bars, turnover (USD) and exact funding per bar, for every Delta crypto perp with history.
Sources: data/delta_1m (1-minute, 10 coins, from Dec 2023) and data/delta_15m (183 coins, from Nov 2024), both Delta India candles; funding from Delta's FUNDING series
(cached; fetched from the public API when missing). Turnover = volume (contracts) x contract value x close. Funding is paid at 00/08/16 UTC at the stored rate (percent per 8h).
Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .data import BASE, fetch_candles
from .round5_r3_breadth import NOT_CRYPTO

ROOT = Path(__file__).resolve().parents[3] / "data"
OUT = ROOT / "delta_lit"
T0 = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp())
T1 = int(pd.Timestamp("2026-11-25", tz="UTC").timestamp())      # exclusive: last complete UTC day is 24 Nov
H4 = 14400
NB = (T1 - T0) // H4
MAJORS = ["BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"]


@lru_cache(maxsize=1)
def contract_values() -> dict[str, float]:
    r = httpx.get(f"{BASE}/v2/products", params={"contract_types": "perpetual_futures"}, timeout=30).json()["result"]
    return {p["symbol"]: float(p["contract_value"]) for p in r}


def _funding(sym: str) -> pd.DataFrame | None:
    p = OUT / f"{sym}_funding.pkl"
    if p.exists():
        return pd.read_pickle(p)
    for cand in (ROOT / "delta_15m" / f"{sym}_funding_1h.pkl",):
        if cand.exists():
            d = pd.read_pickle(cand)
            d.to_pickle(p)
            return d
    try:
        d = fetch_candles(f"FUNDING:{sym}", "1h", T0 - 86400, T1 + 86400)
    except Exception:
        return None
    d.to_pickle(p)
    return d


def _bars4(d: pd.DataFrame, per: int) -> dict[str, np.ndarray]:
    g = (d.time.to_numpy() // H4) * H4
    a = d.assign(g=g).groupby("g").agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"), v=("volume", "sum"), n=("time", "size"))
    a = a[a.n >= per * 0.5]
    return {k: a[k].to_numpy(dtype=float) for k in "ohlcv"} | {"t": a.index.to_numpy()}


def build() -> dict:
    cv = contract_values()
    src: dict[str, tuple[Path, int]] = {}
    for p in sorted((ROOT / "delta_15m").glob("*_15m.pkl")):
        src[p.name[:-8]] = (p, 16)
    for p in sorted((ROOT / "delta_1m").glob("*_1m.pkl")):
        src[p.name[:-7]] = (p, 240)                                 # the 1-minute file wins where both exist
    syms = [s for s in src if s.endswith("USD") and s[:-3] not in NOT_CRYPTO and s in cv]
    syms.sort(key=lambda s: (s not in MAJORS, s))
    n = len(syms)
    O, Hh, L, C, TO, F = (np.full((NB, n), np.nan) for _ in range(6))
    keep = []
    for j, s in enumerate(syms):
        d = pd.read_pickle(src[s][0]).drop_duplicates("time").sort_values("time")
        d = d[(d.time >= T0 - H4) & (d.time < T1)]
        if len(d) < 100:
            continue
        b = _bars4(d, src[s][1])
        ix = (b["t"] - T0) // H4
        m = (ix >= 0) & (ix < NB)
        ix = ix[m]
        for arr, k in ((O, "o"), (Hh, "h"), (L, "l"), (C, "c")):
            arr[ix, j] = b[k][m]
        TO[ix, j] = b["v"][m] * cv[s] * b["c"][m]
        f = _funding(s)
        if f is None:
            continue
        hh = (f.time.to_numpy() // 3600) % 24
        f = f[np.isin(hh, (0, 8, 16)) & (f.time >= T0) & (f.time < T1)]
        fx = (f.time.to_numpy() - T0) // H4
        F[fx, j] = f.close.to_numpy(dtype=float) / 100.0
        keep.append(j)
        time.sleep(0.0)
    keep = np.array(keep)
    names = [syms[j] for j in keep]
    out = dict(O=O[:, keep], H=Hh[:, keep], L=L[:, keep], C=C[:, keep], TO=TO[:, keep], F=F[:, keep], names=np.array(names), t=T0 + H4 * np.arange(NB))
    np.savez_compressed(OUT / "panel4h.npz", **out)
    return out


@lru_cache(maxsize=1)
def panel() -> dict:
    p = OUT / "panel4h.npz"
    if not p.exists():
        return build()
    z = np.load(p, allow_pickle=True)
    return {k: z[k] for k in z.files}


def daily(P: dict) -> dict:
    """Daily bars (UTC) from the 4h panel; a day needs all six 4h bars of that coin to count."""
    nd = NB // 6
    def r(a):
        return a[: nd * 6].reshape(nd, 6, -1)
    O, H, L, C, TO, F = (r(P[k]) for k in ("O", "H", "L", "C", "TO", "F"))
    ok = np.isfinite(C).all(axis=1)
    with np.errstate(all="ignore"):
        D = {"O": O[:, 0], "H": np.nanmax(H, axis=1), "L": np.nanmin(L, axis=1), "C": C[:, -1], "TO": np.nansum(TO, axis=1), "F": np.nansum(F, axis=1)}
    for k in ("O", "H", "L", "C", "TO"):
        D[k] = np.where(ok, D[k], np.nan)
    D["Fok"] = np.isfinite(F).any(axis=1)
    D["names"], D["t"] = P["names"], P["t"][: nd * 6 : 6]
    return D


if __name__ == "__main__":
    P = build()
    print(P["C"].shape, len(P["names"]))
