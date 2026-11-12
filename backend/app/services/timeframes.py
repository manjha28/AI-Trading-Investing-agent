"""Derive higher-timeframe candles from 1-minute OHLCV, with exchange-session alignment and no look-ahead.

Conventions
- A 1-minute row's ``time`` is the candle OPEN time (UTC, tz-aware); it is known only at ``time + 1m`` (its close).
- A derived candle covers ``[time, bar_end)`` and is *completed* only once the clock reaches ``bar_end``. ``bar_end`` is the
  instant its information becomes available; ``align_to_base`` exposes a higher-timeframe value to a base bar only if
  ``bar_end <= base_bar_close``. A forming candle is never exposed.
- Indian equity buckets are anchored to the session open (09:15 IST), so 60m candles run 09:15, 10:15, ... and the final 15:15
  candle is truncated at the 15:30 close (``truncated``). They never cross a session boundary. Crypto is continuous 24/7 and
  buckets are anchored to the UTC epoch.
- Missing 1-minute rows are never invented: a candle built from fewer rows than expected carries ``minutes_present < minutes_expected``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Any

import numpy as np
import pandas as pd

BASE = "1m"
TIMEFRAME_MINUTES: dict[str, int] = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "60m": 60}
FRAME_COLUMNS = ("time", "open", "high", "low", "close", "volume")
MINUTE = pd.Timedelta(minutes=1)
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


class UnsupportedMarket(ValueError):
    pass


def register_timeframe(label: str, minutes: int) -> None:
    """Adding a timeframe is one line: every service reads TIMEFRAME_MINUTES."""
    if minutes < 1 or label in TIMEFRAME_MINUTES and TIMEFRAME_MINUTES[label] != minutes:
        raise ValueError("invalid timeframe registration")
    TIMEFRAME_MINUTES[label] = minutes


def minutes_of(timeframe: str) -> int:
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except KeyError:
        raise ValueError(f"unsupported timeframe {timeframe!r}; supported: {', '.join(TIMEFRAME_MINUTES)}") from None


@dataclass(frozen=True)
class MarketProfile:
    key: str
    tz: str
    session_open: dtime | None
    session_close: dtime | None
    weekdays_only: bool

    @property
    def continuous(self) -> bool:
        return self.session_open is None

    @property
    def session_minutes(self) -> int:
        if self.continuous:
            return 1440
        return (self.session_close.hour * 60 + self.session_close.minute) - (self.session_open.hour * 60 + self.session_open.minute)

    @property
    def open_minute(self) -> int:
        return 0 if self.continuous else self.session_open.hour * 60 + self.session_open.minute


EQUITY_IN = MarketProfile("equity_in", "Asia/Kolkata", dtime(9, 15), dtime(15, 30), True)
CRYPTO = MarketProfile("crypto", "UTC", None, None, False)
PROFILES = {EQUITY_IN.key: EQUITY_IN, CRYPTO.key: CRYPTO}


def profile_for(instrument: Any) -> MarketProfile:
    asset = (getattr(instrument, "asset_class", "") or "").lower()
    if asset == "crypto":
        return CRYPTO
    if asset in ("equity", "etf", "index", "future", "option") and getattr(instrument, "country", None) == "IN":
        return EQUITY_IN
    raise UnsupportedMarket(f"{getattr(instrument, 'symbol', '?')}: only Indian equities and crypto are supported for intraday signals")


def local_minutes(times: pd.Series, profile: MarketProfile) -> tuple[pd.Series, pd.Series]:
    """(local calendar date, minute of local day) for each UTC timestamp."""
    local = times.dt.tz_convert(profile.tz)
    return local.dt.strftime("%Y-%m-%d"), local.dt.hour * 60 + local.dt.minute


def session_keys(times: pd.Series, profile: MarketProfile) -> pd.Series:
    """Session identity used to reset session-anchored indicators (local date; UTC date for crypto)."""
    return local_minutes(times, profile)[0]


def in_session_mask(times: pd.Series, profile: MarketProfile) -> np.ndarray:
    if profile.continuous:
        return np.ones(len(times), dtype=bool)
    local = times.dt.tz_convert(profile.tz)
    rel = (local.dt.hour * 60 + local.dt.minute) - profile.open_minute
    return ((rel >= 0) & (rel < profile.session_minutes) & (local.dt.dayofweek < 5)).to_numpy()


def _blank(extra: tuple[str, ...] = ()) -> pd.DataFrame:
    return pd.DataFrame(columns=[*FRAME_COLUMNS, "bar_end", "minutes_present", "minutes_expected", "truncated", *extra])


def aggregate(frame: pd.DataFrame, timeframe: str, profile: MarketProfile) -> pd.DataFrame:
    """Aggregate clean 1-minute rows into ``timeframe`` candles. Only candles completed by the end of ``frame`` are returned."""
    minutes = minutes_of(timeframe)
    if frame.empty:
        return _blank()
    f = frame.sort_values("time").reset_index(drop=True)
    if minutes == 1:
        out = f.loc[:, list(FRAME_COLUMNS)].copy()
        out["bar_end"] = out["time"] + MINUTE
        out["minutes_present"], out["minutes_expected"], out["truncated"] = 1, 1, False
        return out
    times = f["time"]
    if profile.continuous:
        epoch_min = ((times - EPOCH) // MINUTE).to_numpy(dtype="int64")
        start_min = epoch_min // minutes * minutes
        start = EPOCH + pd.to_timedelta(start_min, unit="m")
        expected = np.full(len(f), minutes)
        keep = np.ones(len(f), dtype=bool)
    else:
        keep = in_session_mask(times, profile)
        f, times = f.loc[keep].reset_index(drop=True), times.loc[keep].reset_index(drop=True)
        if f.empty:
            return _blank()
        _, minute_of_day = local_minutes(times, profile)
        rel = (minute_of_day - profile.open_minute).to_numpy(dtype="int64")
        bucket_rel = rel // minutes * minutes
        start = times - pd.to_timedelta(rel - bucket_rel, unit="m")
        expected = np.minimum(minutes, profile.session_minutes - bucket_rel)
    g = f.assign(_start=pd.Series(pd.DatetimeIndex(start), index=f.index), _expected=expected)
    grouped = g.groupby("_start", sort=True)
    out = grouped.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
                      volume=("volume", "sum"), minutes_present=("open", "size"), minutes_expected=("_expected", "first"))
    out = out.reset_index().rename(columns={"_start": "time"})
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out["bar_end"] = out["time"] + pd.to_timedelta(out["minutes_expected"], unit="m")
    out["truncated"] = out["minutes_expected"] < minutes
    data_end = f["time"].iloc[-1] + MINUTE
    # a candle whose period has not finished by the end of the data is still forming: never return it
    out = out.loc[out["bar_end"] <= data_end].reset_index(drop=True)
    return out[[*FRAME_COLUMNS, "bar_end", "minutes_present", "minutes_expected", "truncated"]]


def completed_until(frame: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Candles whose information is available at ``as_of`` (bar_end <= as_of)."""
    return frame.loc[frame["bar_end"] <= as_of].reset_index(drop=True)


def align_to_base(htf: pd.DataFrame, columns: list[str], base_times: pd.Series) -> dict[str, np.ndarray]:
    """For each base bar, the value of the most recent higher-timeframe candle COMPLETED by that base bar's close.

    ``htf`` must carry ``bar_end``. A base bar at open ``t`` closes at ``t + 1m``; a candle is visible iff ``bar_end <= t + 1m``.
    """
    n = len(base_times)
    if htf.empty:
        return {c: np.full(n, np.nan) for c in columns}
    ends = htf["bar_end"].to_numpy(dtype="datetime64[ns]")
    base_close = (base_times + MINUTE).to_numpy(dtype="datetime64[ns]")
    idx = np.searchsorted(ends, base_close, side="right") - 1
    ok = idx >= 0
    out: dict[str, np.ndarray] = {}
    for c in columns:
        values = htf[c].to_numpy(dtype=float)
        col = np.full(n, np.nan)
        col[ok] = values[idx[ok]]
        out[c] = col
    return out


def decision_mask(base_times: pd.Series, htf: pd.DataFrame) -> np.ndarray:
    """True on base bars whose close completes a candle of the decision timeframe."""
    if htf.empty:
        return np.zeros(len(base_times), dtype=bool)
    return np.isin((base_times + MINUTE).to_numpy(dtype="datetime64[ns]"), htf["bar_end"].to_numpy(dtype="datetime64[ns]"))


def to_frame(rows: list[Any]) -> pd.DataFrame:
    """ORM/namespace bars (open-time ``timestamp``) to the float OHLCV frame. Money stays Decimal in the caller's rows."""
    if not rows:
        return pd.DataFrame(columns=list(FRAME_COLUMNS))
    return pd.DataFrame({
        "time": pd.to_datetime([r.timestamp for r in rows], utc=True),
        "open": [float(r.open) for r in rows], "high": [float(r.high) for r in rows],
        "low": [float(r.low) for r in rows], "close": [float(r.close) for r in rows],
        "volume": [float(r.volume) for r in rows]})
