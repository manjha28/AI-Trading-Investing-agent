"""Turn the recorder's files into measured execution costs: quoted spread, and the cost of a market order of my size walked through the recorded book
(half-spread plus impact), in bps, per symbol and hour of day. Run after the recorder has collected a while:
    python -m app.delta_lab.measure_costs --dir data/delta_book --notional 500
Read-only; decision support, not investment advice."""
from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

CONTRACT_VALUE = {"BTCUSD": 0.001, "ETHUSD": 0.01}


def walk(levels: list[list], qty_contracts: float) -> float | None:
    """Average fill price for qty contracts across [[price, size], ...] (size in contracts); None if the visible depth is too thin."""
    need, cost = qty_contracts, 0.0
    for price, size in levels:
        take = min(need, float(size))
        cost += take * float(price)
        need -= take
        if need <= 1e-9:
            return cost / qty_contracts
    return None


def rows(path: Path):
    with gzip.open(path, "rt") as fh:
        for line in fh:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def measure(root: Path, notional_usd: float) -> dict:
    out: dict = {}
    for sym_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        sym = sym_dir.name
        cv = CONTRACT_VALUE.get(sym)
        if cv is None:
            continue
        spreads, buy_cost, sell_cost, by_hour = [], [], [], {}
        for f in sorted(sym_dir.glob("book-*.jsonl.gz")):
            for r in rows(f):
                if not r["bids"] or not r["asks"]:
                    continue
                bid, ask = float(r["bids"][0][0]), float(r["asks"][0][0])
                mid = (bid + ask) / 2
                qty = max(1.0, round(notional_usd / (mid * cv)))
                spread_bps = (ask - bid) / mid * 1e4
                b, s = walk(r["asks"], qty), walk(r["bids"], qty)
                if b is None or s is None:
                    continue
                spreads.append(spread_bps); buy_cost.append((b - mid) / mid * 1e4); sell_cost.append((mid - s) / mid * 1e4)
                by_hour.setdefault(datetime.fromtimestamp(r["t"], tz=timezone.utc).hour, []).append((b - mid) / mid * 1e4)
        if spreads:
            both = np.array(buy_cost + sell_cost)
            out[sym] = {"snapshots": len(spreads), "contracts_at_size": qty, "spread_bps_median": float(np.median(spreads)), "spread_bps_p95": float(np.percentile(spreads, 95)),
                        "market_order_cost_bps_median": float(np.median(both)), "market_order_cost_bps_p95": float(np.percentile(both, 95)),
                        "by_hour_median_bps": {h: float(np.median(v)) for h, v in sorted(by_hour.items())}}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[3] / "data" / "delta_book")); ap.add_argument("--notional", type=float, default=500.0)
    a = ap.parse_args()
    res = measure(Path(a.dir), a.notional)
    print("| Symbol | Snapshots | Contracts at size | Median spread (bps) | p95 spread | Market-order cost, median (bps) | p95 |\n|---|---|---|---|---|---|---|")
    for s, r in res.items():
        print(f"| {s} | {r['snapshots']:,} | {r['contracts_at_size']:.0f} | {r['spread_bps_median']:.2f} | {r['spread_bps_p95']:.2f} | {r['market_order_cost_bps_median']:.2f} | {r['market_order_cost_bps_p95']:.2f} |")
    print("\nThe round-2 simulator assumes 1.0 bp per taker fill (never below one tick): replace it with the median market-order cost above once 2+ weeks are recorded.")


if __name__ == "__main__":
    main()
