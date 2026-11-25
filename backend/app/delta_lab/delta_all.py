"""Re-run the strategy families that were first tested on Binance data on DELTA's own 1-minute data instead (owner, 25 Nov 2026: Delta coins only).
Hypothetical research; decision support, not investment advice. No order path.

DATA: Delta India 1-minute candles in data/delta_1m (BTC, ETH, SOL, LISTA, XRP, EVAA, AIOT, BLESS, ARIA, AKE; each from its Delta listing, BTC/ETH from
Dec 2023 / Feb 2024). Families and their own variation grids, rules unchanged from their original modules:
  - trend_rejection (owner's trend + rejection rule, 6 variants)
  - lower_high_volume (owner's lower-high shorts with volume, 16 variants)
  - R3 1h quiet-range breakout: the frozen rule plus 17 variations (range 12/24/48 h x quiet threshold 5/10/20% x target 2R/3R), Delta contract specs,
    funding not counted (no Delta funding history in the 1-minute set; stated)
Each family keeps its own time split (train 50% / validation 25% / holdout 25% of its own trades). Results go to reports/delta_runs/."""
from __future__ import annotations

import json
from decimal import Decimal as D
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import families, round2 as r2
from .engine import CostModel, Market, Signal, Leg, simulate
from .risk import ContractSpec, RiskConfig
from .round4_d2 import boot_ci
from .run import REPORTS as _REPORTS

DELTA_1M = Path(__file__).resolve().parents[3] / "data" / "delta_1m"
OUT = _REPORTS / "delta_runs"
COINS = tuple(sorted(p.stem.replace("_1m", "") for p in DELTA_1M.glob("*_1m.pkl")))


def delta_load(sym: str) -> pd.DataFrame:
    d = pd.read_pickle(DELTA_1M / f"{sym}_1m.pkl")
    return d.astype({"time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float}).drop_duplicates("time").sort_values("time").reset_index(drop=True)


def run_module(mod) -> dict:
    from functools import lru_cache
    mod.load = lru_cache(maxsize=4)(delta_load)
    mod.SYMBOLS = COINS
    mod.REPORTS = OUT
    return mod.main()


def r3_variant(m: Market, range_bars: int, rank_max: float, target_r: float) -> list[Signal]:
    bars = families.resample(m, "1h")
    hi, lo = bars["h"].rolling(range_bars).max(), bars["l"].rolling(range_bars).min()
    rank = (hi - lo).rolling(1440).rank(pct=True)
    starts = families._epoch_s(bars.index)
    out = []
    for i in np.flatnonzero((rank <= rank_max).fillna(False).to_numpy()):
        idx = r2._idx_at(m, int(starts[i]) + 3600)
        if idx < 0:
            continue
        a, b = float(hi.iloc[i]), float(lo.iloc[i])
        out.append(Signal(idx, "R3", f"R3|{range_bars}h|q{rank_max}|{target_r}R", [Leg("long", a, b, a + target_r * (a - b)), Leg("short", b, a, b - target_r * (a - b))],
                          "stop_entry", valid_bars=60, max_hold=48 * 60))
    return out


def delta_market(sym: str, product: dict) -> Market:
    d = delta_load(sym)
    t = d.time.to_numpy(np.int64)
    o, h, lo, c, v = (d[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    spec = ContractSpec(sym, D(product["contract_value"]), D(product["tick_size"]), mm_pct=D(product["maintenance_margin"]) / 100, im_pct_min=D(product["initial_margin"]) / 100,
                        slope_threshold_notional=D(product.get("max_leverage_notional") or "1000000"), max_leverage=D(1) / (D(product["initial_margin"]) / 100),
                        taker_fee=D(product["taker_commission_rate"]), maker_fee=D(product["maker_commission_rate"]))
    m = Market(sym, spec, float(product["tick_size"]), float(product["contract_value"]), t, o, h, lo, c, v, o, h, lo, c, {})
    m.atr_1h = r2._atr_grid(m, "1h")
    return m


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    return {"trades": int(len(r)), "net_bps": float(r.mean()), "pf": float(w.sum() / -l.sum()) if l.sum() < 0 else None, "win_rate": float((r > 0).mean())}


def run_r3() -> dict:
    with httpx.Client(timeout=30) as c:
        products = {p["symbol"]: p for p in c.get("https://api.india.delta.exchange/v2/products", params={"contract_types": "perpetual_futures"}).json()["result"]}
    markets = {s: delta_market(s, products[s]) for s in COINS if s in products}
    grid = [(rb, q, tr) for rb in (12, 24, 48) for q in (0.05, 0.10, 0.20) for tr in (2.0, 3.0)]
    data = {}
    for rb, q, tr in grid:
        rows = []
        for s, m in markets.items():
            trades, _r, _c = simulate(m, r3_variant(m, rb, q, tr), CostModel(), RiskConfig(leverage=D(10), mode="paper"))
            rows += [{"symbol": s, "t": int(x["entry_time"]), "bps": float(x["bps_net"]), "side": x["side"]} for x in trades]
        data[f"range={rb}h|quiet<={int(q * 100)}%|target={tr:g}R" + (" (FROZEN R3)" if (rb, q, tr) == (24, 0.10, 3.0) else "")] = pd.DataFrame(rows).sort_values("t") if rows else pd.DataFrame(columns=["t", "bps"])
    allt = np.sort(np.concatenate([d.t.to_numpy() for d in data.values() if len(d)]))
    c1, c2 = allt[int(len(allt) * 0.5)], allt[int(len(allt) * 0.75)]
    res = {"coins": list(markets), "variants_added": len(grid) - 1, "cuts": [int(c1), int(c2)], "splits": {}}
    for n, d in data.items():
        h = d[d.t >= c2].bps.to_numpy()
        res["splits"][n] = {"train": stats(d[d.t < c1].bps.to_numpy()), "validation": stats(d[(d.t >= c1) & (d.t < c2)].bps.to_numpy()), "holdout": stats(h),
                            "holdout_boot90": list(boot_ci(h)) if len(h) > 4 else None,
                            "holdout_long": stats(d[(d.t >= c2) & (d.side == "long")].bps.to_numpy()), "holdout_short": stats(d[(d.t >= c2) & (d.side == "short")].bps.to_numpy())}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "r3_delta.json").write_text(json.dumps(res, indent=1, default=str))
    return res


def main() -> dict:
    from . import lower_high_volume, trend_rejection
    OUT.mkdir(parents=True, exist_ok=True)
    out = {"coins": COINS}
    out["trend_rejection"] = run_module(trend_rejection)
    out["lower_high_volume"] = run_module(lower_high_volume)
    out["r3"] = run_r3()
    return out


if __name__ == "__main__":
    o = main()
    print(json.dumps({k: (v.get("chosen") if isinstance(v, dict) else v) for k, v in o.items()}, default=str))
