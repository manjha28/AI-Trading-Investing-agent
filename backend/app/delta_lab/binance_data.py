"""Binance USD-M perpetual history from data.binance.vision (public, no key): 1m klines, mark-price klines, funding history monthly; aggTrades daily
reduced to 1-second bars (price OHLC, volume, signed volume) so the multi-GB tick files are never kept. Every zip is verified against its .CHECKSUM (sha256)
before use. Stored under data/binance/ (gitignored)."""
from __future__ import annotations

import hashlib
import io
import sys
import time
import zipfile
from datetime import date, timedelta
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = "https://data.binance.vision/data/futures/um"
OUT = Path(__file__).resolve().parents[3] / "data" / "binance"
START = date(2020, 1, 1)


def _get(client: httpx.Client, url: str) -> bytes | None:
    for attempt in range(4):
        try:
            r = client.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except httpx.HTTPError:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}")


def fetch_verified(client: httpx.Client, url: str) -> bytes | None:
    blob = _get(client, url)
    if blob is None:
        return None
    check = _get(client, url + ".CHECKSUM")
    if check is None:
        raise RuntimeError(f"no checksum published for {url}")
    want = check.decode().split()[0]
    if hashlib.sha256(blob).hexdigest() != want:
        raise RuntimeError(f"checksum mismatch: {url}")
    return blob


def _csv(blob: bytes, names: list[str]) -> pd.DataFrame:
    """Columns are taken by position (the files carry extra columns, and a header row from 2022 on)."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0 if raw[:1].isalpha() else None)
    df = df.iloc[:, : len(names)]
    df.columns = names
    return df


def months(start: date, end: date):
    cur = date(start.year, start.month, 1)
    while cur <= end:
        yield cur
        cur = date(cur.year + (cur.month == 12), 1 if cur.month == 12 else cur.month + 1, 1)


def klines(client, symbol: str, kind: str, start: date, end: date) -> pd.DataFrame:
    """kind: 'klines' (last price) or 'markPriceKlines'. Monthly files for full months, daily files for the current month."""
    frames = []
    for m in months(start, end):
        url = f"{ROOT}/monthly/{kind}/{symbol}/1m/{symbol}-1m-{m:%Y-%m}.zip"
        blob = fetch_verified(client, url)
        if blob is not None:
            frames.append(_csv(blob, ["open_time", "open", "high", "low", "close", "volume"]))
        elif m.year == end.year and m.month == end.month:
            d = m
            while d <= end - timedelta(days=1):
                b = fetch_verified(client, f"{ROOT}/daily/{kind}/{symbol}/1m/{symbol}-1m-{d:%Y-%m-%d}.zip")
                if b is not None:
                    frames.append(_csv(b, ["open_time", "open", "high", "low", "close", "volume"]))
                d += timedelta(days=1)
    df = pd.concat(frames, ignore_index=True)[["open_time", "open", "high", "low", "close", "volume"]]
    df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
    df = df.dropna(subset=["open_time"])
    df["time"] = (df["open_time"].astype("int64") // (1000 if df["open_time"].iloc[0] < 10**14 else 10**6)).astype("int64")      # ms (or us from 2025) to seconds
    return df.drop(columns="open_time").drop_duplicates("time").sort_values("time").reset_index(drop=True)


def funding(client, symbol: str, start: date, end: date) -> pd.DataFrame:
    frames = []
    for m in months(start, end):
        blob = fetch_verified(client, f"{ROOT}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{m:%Y-%m}.zip")
        if blob is not None:
            frames.append(_csv(blob, ["calc_time", "funding_interval_hours", "last_funding_rate"]))
    df = pd.concat(frames, ignore_index=True)
    df["time"] = (pd.to_numeric(df["calc_time"]).astype("int64") // 1000).astype("int64")
    return df[["time", "last_funding_rate"]].rename(columns={"last_funding_rate": "rate"}).drop_duplicates("time").sort_values("time").reset_index(drop=True)


def aggtrades_day_to_1s(blob: bytes) -> pd.DataFrame:
    """One day of aggTrades -> 1-second bars: open/high/low/close, volume, buy volume (taker buys), sell volume (taker sells), trade count."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0])
    df = pd.read_csv(io.BytesIO(raw), header=0 if raw[:1].isalpha() else None).iloc[:, [1, 2, 5, 6]]
    df.columns = ["price", "qty", "time", "is_buyer_maker"]
    sec = (pd.to_numeric(df["time"]).astype("int64") // 1000)
    sec = sec.where(sec < 10**11, sec // 1000)       # microsecond timestamps (2025 on)
    maker = df["is_buyer_maker"].astype(str).str.lower().isin(["true", "1"])
    df["sec"] = sec
    df["buy"] = np.where(~maker, df["qty"], 0.0)
    g = df.groupby("sec")
    out = pd.DataFrame({"open": g["price"].first(), "high": g["price"].max(), "low": g["price"].min(), "close": g["price"].last(), "volume": g["qty"].sum(), "buy": g["buy"].sum(), "n": g["price"].count()})
    out["sell"] = out["volume"] - out["buy"]
    return out.reset_index().rename(columns={"sec": "time"}).astype({"time": "int64"})


def download(symbols=("BTCUSDT", "ETHUSDT"), tick_from=date(2025, 9, 1)) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    end = date.today()
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        for sym in symbols:
            for name, kind in (("last_1m", "klines"), ("mark_1m", "markPriceKlines")):
                f = OUT / f"{sym}_{name}.pkl"
                if not f.exists():
                    klines(c, sym, kind, START, end).to_pickle(f); print(sym, name, "done", flush=True)
            f = OUT / f"{sym}_funding.pkl"
            if not f.exists():
                funding(c, sym, START, end).to_pickle(f); print(sym, "funding done", flush=True)
            d = OUT / f"{sym}_1s"
            d.mkdir(exist_ok=True)
            day = tick_from
            while day < end:
                p = d / f"{day:%Y-%m-%d}.pkl"
                if not p.exists():
                    blob = fetch_verified(c, f"{ROOT}/daily/aggTrades/{sym}/{sym}-aggTrades-{day:%Y-%m-%d}.zip")
                    if blob is not None:
                        aggtrades_day_to_1s(blob).to_pickle(p)
                day += timedelta(days=1)
                if day.day == 1:
                    print(sym, "aggTrades through", day, flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    download()
