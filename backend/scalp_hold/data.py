"""Candle loading, validation, gap reporting, resampling and the explicit ``fetch`` command (the only network code).

Schema: ts_utc, open, high, low, close, volume, [oi], [funding_rate]. The index is the bar OPEN time (UTC).
Validation raises; it never warns and never repairs. Gaps are reported, and sessions with more than 1% missing bars are excluded and logged.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

from scalp_hold.venues import VenueProfile

log = logging.getLogger("scalp_hold.data")
REQUIRED = ["open", "high", "low", "close", "volume"]
MAX_MISSING_PCT = 1.0
DELTA_BASE = "https://api.india.delta.exchange"
MAX_PER_REQUEST = 1500


class DataError(ValueError):
    pass


def bar_minutes(index: pd.DatetimeIndex) -> int:
    if len(index) < 2:
        raise DataError("need at least two bars to infer the bar size")
    deltas = ((index[1:] - index[:-1]) / pd.Timedelta(minutes=1)).to_numpy()
    return int(pd.Series(deltas).mode().iloc[0])


def load_candles(path: str | Path) -> pd.DataFrame:
    """Load csv/parquet. Lines starting with '#' in a CSV are comments (used to label test fixtures)."""
    p = Path(path)
    df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p, comment="#")
    if "ts_utc" in df.columns:
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
        df = df.set_index("ts_utc")
    df.index = pd.DatetimeIndex(df.index, tz="UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    df.index.name = "ts_utc"
    return df


def session_keys(index: pd.DatetimeIndex, venue: VenueProfile) -> pd.Series:
    """Anchor/session identity per bar, from the venue's anchor rule."""
    rule = venue.anchor_rule
    if rule == "SESSION_OPEN" or rule == "LOCAL_MIDNIGHT":
        keys = index.tz_convert(venue.tz).strftime("%Y-%m-%d")
    elif rule == "UTC_MIDNIGHT":
        keys = index.strftime("%Y-%m-%d")
    else:                                   # ROLLING: no anchor; one group, windows applied by the caller
        keys = np.full(len(index), "rolling")
    return pd.Series(keys, index=index, name="session")


def in_session_mask(index: pd.DatetimeIndex, venue: VenueProfile) -> np.ndarray:
    if venue.session_open is None:
        return np.ones(len(index), dtype=bool)
    local = index.tz_convert(venue.tz)
    t = local.hour * 60 + local.minute
    o = venue.session_open.hour * 60 + venue.session_open.minute
    c = venue.session_close.hour * 60 + venue.session_close.minute      # type: ignore[union-attr]
    return (t >= o) & (t < c) & (local.dayofweek < 5)


def validate(df: pd.DataFrame, venue: VenueProfile) -> None:
    """Raise on any structural problem. Order matters: index first, then values."""
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise DataError(f"missing columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        raise DataError("index must be a tz-aware DatetimeIndex (bar open time, UTC)")
    if not df.index.is_monotonic_increasing:
        raise DataError("index is not monotonic increasing")
    if df.index.has_duplicates:
        raise DataError(f"{int(df.index.duplicated().sum())} duplicated timestamps")
    if df[REQUIRED].isna().any().any():
        raise DataError("NaN in OHLCV")
    if ((df[["open", "high", "low", "close"]] <= 0).any()).any():
        raise DataError("non-positive price")
    if (df["high"] < df[["open", "close"]].max(axis=1)).any():
        raise DataError("high < max(open, close)")
    if (df["low"] > df[["open", "close"]].min(axis=1)).any():
        raise DataError("low > min(open, close)")
    if (df["volume"] < 0).any():
        raise DataError("negative volume")
    if venue.is_india:
        inside = in_session_mask(df.index, venue)
        if ((df["volume"] == 0) & inside).any():
            raise DataError("zero-volume bar inside the session")


def gap_report(df: pd.DataFrame, venue: VenueProfile, max_missing_pct: float = MAX_MISSING_PCT) -> pd.DataFrame:
    """Expected vs actual bars per session. The first and last sessions of a series are 'edge' (they may be cut by the data window)."""
    minutes = bar_minutes(df.index)
    per_session = (375 if venue.is_india else 1440) // minutes
    keys = session_keys(df.index, venue)
    counts = keys.groupby(keys).size()
    ordered = sorted(counts.index)
    rows = []
    for i, k in enumerate(ordered):
        actual = int(counts[k])
        missing_pct = 100.0 * max(0, per_session - actual) / per_session
        edge = i in (0, len(ordered) - 1)
        rows.append({"session": k, "expected": per_session, "actual": actual, "missing_pct": round(missing_pct, 3), "edge": edge,
                     "excluded": (not edge) and missing_pct > max_missing_pct})
    return pd.DataFrame(rows)


def exclude_bad_sessions(df: pd.DataFrame, venue: VenueProfile, max_missing_pct: float = MAX_MISSING_PCT) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop sessions with more than ``max_missing_pct`` (default 1%) missing bars, and log them. Bars are never filled."""
    report = gap_report(df, venue, max_missing_pct)
    bad = set(report.loc[report["excluded"], "session"])
    dropped = report[report["excluded"]]
    for _, row in dropped.head(3).iterrows():
        log.warning("excluding session %s: %d of %d bars (%.2f%% missing)", row["session"], row["actual"], row["expected"], row["missing_pct"])
    if len(dropped) > 3:
        log.warning("... and %d more sessions excluded (%.2f%% - %.2f%% missing); full list in the gap report", len(dropped) - 3, dropped["missing_pct"].min(), dropped["missing_pct"].max())
    keys = session_keys(df.index, venue)
    return df.loc[~keys.isin(bad).to_numpy()], report


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Left-labelled, left-closed bars (index = bar open). Incomplete trailing buckets are kept as they are; callers lag by one bar where needed."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    for extra in ("oi", "funding_rate"):
        if extra in df.columns:
            agg[extra] = "last"
    out = df.resample(f"{minutes}min", label="left", closed="left").agg(agg)
    return out.dropna(subset=["open"])


# ───────────────────────── fetch (explicit network command) ─────────────────────────
_RES_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


def fetch_delta(symbol: str, res: str, days: int, *, base: str = DELTA_BASE, now_ts: int | None = None, opener=urllib.request.urlopen) -> pd.DataFrame:
    """GET {base}/v2/history/candles (resolution, symbol, start, end in unix seconds), 1500 candles per request, deduplicated, ascending.

    The first three RAW rows of the first response are printed so you can verify whether ``time`` is the bar open or close
    (this engine assumes OPEN; if the rows show close times, shift before use).
    """
    step = _RES_SECONDS[res]
    end = int(now_ts if now_ts is not None else pd.Timestamp.now(tz="UTC").timestamp())
    start = end - days * 86400
    rows: list[dict] = []
    cursor = start
    first = True
    while cursor < end:
        chunk_end = min(end, cursor + MAX_PER_REQUEST * step)
        query = urllib.parse.urlencode({"resolution": res, "symbol": symbol, "start": cursor, "end": chunk_end})
        with opener(f"{base}/v2/history/candles?{query}", timeout=30) as response:        # nosec - explicit user-invoked fetch
            payload = json.loads(response.read())
        result = payload.get("result") or []
        if first:
            print(f"[fetch] first 3 raw rows for {symbol} {res}:")
            for raw in result[:3]:
                print("   ", raw)
            first = False
        rows.extend(result)
        cursor = chunk_end
    if not rows:
        raise DataError(f"no candles returned for {symbol} {res}")
    df = pd.DataFrame(rows).rename(columns={"time": "ts"})
    df["ts_utc"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.drop_duplicates("ts_utc").sort_values("ts_utc").set_index("ts_utc")[REQUIRED]
    return df.astype(float)


def default_path(root: str | Path, venue_key: str, symbol: str, res: str) -> Path:
    return Path(root) / f"{venue_key}_{symbol}_{res}.csv"


__all__ = ["DataError", "bar_minutes", "load_candles", "session_keys", "in_session_mask", "validate", "gap_report", "exclude_bad_sessions", "resample", "fetch_delta"]
