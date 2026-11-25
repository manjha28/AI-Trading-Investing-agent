"""Download Delta India 1-minute last-price candles (public, no credentials) for the 2026-11-26 research: BTC/ETH/SOL/LISTA + Delta's top-10 crypto perps by 24h turnover."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
import pandas as pd

from .data import fetch_candles
from .round5_r3_breadth import NOT_CRYPTO

DIR = Path(__file__).resolve().parents[3] / "data" / "delta_1m"
EXTRA = ["BTCUSD", "ETHUSD", "SOLUSD", "LISTAUSD"]


def symbols() -> list[str]:
    t = httpx.get("https://api.india.delta.exchange/v2/tickers", params={"contract_types": "perpetual_futures"}, timeout=30).json()["result"]
    top = [x["symbol"] for x in sorted((x for x in t if x["symbol"].endswith("USD") and x["symbol"][:-3] not in NOT_CRYPTO), key=lambda x: -float(x.get("turnover_usd") or 0))][:10]
    return list(dict.fromkeys(EXTRA + top))


def main() -> None:
    syms = symbols()
    print(syms, flush=True)
    end = int(time.time()) // 60 * 60 - 60
    for s in syms:
        p = DIR / f"{s}_1m.pkl"
        if p.exists():
            continue
        df = fetch_candles(s, "1m", 1672531200, end)
        df.to_pickle(p)
        print(s, len(df), pd.to_datetime(df.time.min(), unit="s") if len(df) else None, flush=True)


if __name__ == "__main__":
    main()


def one(sym: str) -> None:
    p = DIR / f"{sym}_1m.pkl"
    if not p.exists():
        df = fetch_candles(sym, "1m", 1672531200, int(time.time()) // 60 * 60 - 60)
        df.to_pickle(p)
        print(sym, len(df), flush=True)
