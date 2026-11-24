"""Public Delta India history (no credentials): last-price candles, MARK:<symbol> candles, FUNDING:<symbol> candles, contract specs.
Stored as .npz under diagnostics/delta_lab_data (gitignored). Only candles exist publicly: there are no historical ticks, quotes or trades."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

BASE = "https://api.india.delta.exchange"
DATA_DIR = Path(__file__).resolve().parents[3] / "diagnostics" / "delta_lab_data"
RES_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
PAGE_BARS = 3500


def fetch_candles(symbol: str, resolution: str, start: int, end: int, client: httpx.Client | None = None) -> pd.DataFrame:
    """Pages backwards in windows the API accepts (it caps a response at 4000 bars). Returns an ascending, de-duplicated frame."""
    own = client is None
    client = client or httpx.Client(base_url=BASE, timeout=30)
    step = RES_SECONDS[resolution] * PAGE_BARS
    rows: list[dict] = []
    hi = end
    try:
        while hi > start:
            lo = max(start, hi - step)
            for attempt in range(5):
                try:
                    r = client.get("/v2/history/candles", params={"symbol": symbol, "resolution": resolution, "start": lo, "end": hi})
                    r.raise_for_status()
                    rows.extend(r.json().get("result") or [])
                    break
                except (httpx.HTTPError, ValueError):
                    time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"Delta candle download failed for {symbol} {resolution} {lo}-{hi}")
            hi = lo
            time.sleep(0.05)
    finally:
        if own:
            client.close()
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df[["time", "open", "high", "low", "close", "volume"]]


def save(df: pd.DataFrame, name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.pkl"
    df.to_pickle(path)
    return path


def load(name: str) -> pd.DataFrame:
    return pd.read_pickle(DATA_DIR / f"{name}.pkl")


def product_spec(symbol: str) -> dict:
    r = httpx.get(f"{BASE}/v2/products/{symbol}", timeout=30).json()["result"]
    return r


def download_all(symbols=("BTCUSD", "ETHUSD"), days: int = 1100) -> None:
    now = int(time.time()) // 60 * 60
    start = now - days * 86400
    with httpx.Client(base_url=BASE, timeout=30) as client:
        for sym in symbols:
            first = None
            for series, prefix, res in (("last_1m", "", "1m"), ("mark_1m", "MARK:", "1m"), ("funding_1h", "FUNDING:", "1h")):
                df = fetch_candles(prefix + sym, res, start, now, client)
                save(df, f"{sym}_{series}")
                first = first or (df["time"].min() if len(df) else None)
                print(sym, series, len(df), flush=True)
            save(pd.DataFrame([product_spec(sym)]).drop(columns=["ui_config", "product_specs", "settling_asset", "quoting_asset", "underlying_asset", "spot_index", "contract_unit_currency", "tick_size"], errors="ignore"), f"{sym}_spec_flat")
            import json
            (DATA_DIR / f"{sym}_spec.json").write_text(json.dumps(product_spec(sym), default=str))


if __name__ == "__main__":
    download_all()
