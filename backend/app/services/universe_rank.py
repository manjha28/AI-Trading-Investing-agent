"""Rank of a stock against the stored Dhan NSE universe on factors that predicted forward returns out of sample.

Evidence: backend/app/services/signal_research.py (diagnostics/signal_research/REPORT.md). The old trend-based score had no out-of-sample rank correlation with
forward returns (swing +0.009); this composite had +0.098 (t=4.4) swing and +0.086 positional. Members (each cross-sectionally percentile-ranked, equal weight):
  low volatility (60d), proximity to the 52-week high, 12-1 month momentum, short-term reversal (10d, lower is better), relative volume (20d).
The percentile is a ranking, NOT a probability of profit. Uses completed daily candles only; the result is cached per latest session date.
"""
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

MEMBERS = {"lowvol": "Low volatility (60d)", "near_52w_high": "Close to 52-week high", "mom_12_1": "12-1 month momentum", "reversal_10": "Short-term reversal (10d)", "rvol_20": "Relative volume (20d)"}
MIN_BARS, MIN_UNIVERSE, LOOKBACK_BARS, CACHE_SECONDS = 260, 40, 300, 6 * 3600
_cache: dict[str, Any] = {"at": 0.0, "key": None, "table": None}
_lock = threading.Lock()


def composite_percentiles(close: pd.DataFrame, high: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """Per-stock member percentiles (0-1, higher = better) on the LAST row, plus 'composite'. Pure function of completed candles."""
    r = close.pct_change()
    raw = pd.DataFrame({
        "lowvol": -r.rolling(60).std().iloc[-1],
        "near_52w_high": close.iloc[-1] / close.rolling(252, min_periods=200).max().iloc[-1],
        "mom_12_1": close.iloc[-22] / close.iloc[-253] - 1,
        "reversal_10": -(close.iloc[-1] / close.iloc[-11] - 1),
        "rvol_20": volume.iloc[-1] / volume.rolling(20).mean().iloc[-1],
    })
    pct = raw.rank(pct=True)
    out = pct.copy()
    out["composite"] = pct.mean(axis=1, skipna=False)
    out["composite_pct"] = out["composite"].rank(pct=True) * 100
    return out.join(raw.add_prefix("raw_"))


def _load_table(db: Session) -> tuple[Any, pd.DataFrame | None]:
    rows = db.execute(text("""
        select i.id::text as iid, b.timestamp::date as d, b.high, b.close, b.volume from market_bars b join instruments i on i.id = b.instrument_id
        where b.interval='1d' and b.source='dhan' and b.is_synthetic=false and i.is_synthetic=false and i.asset_class='equity' and i.country='IN'
          and b.timestamp >= (select max(timestamp) from market_bars where interval='1d' and source='dhan') - interval '700 days'""")).all()
    if not rows:
        return None, None
    df = pd.DataFrame(rows, columns=["iid", "d", "high", "close", "volume"]).astype({"high": float, "close": float, "volume": float})
    df["d"] = pd.to_datetime(df["d"])
    df = df.drop_duplicates(["iid", "d"])
    close, high, vol = (df.pivot(index="d", columns="iid", values=c).sort_index() for c in ("close", "high", "volume"))
    keep = close.columns[close.count() >= MIN_BARS]
    close, high, vol = close[keep], high[keep], vol[keep]
    # the stock-date must be current: names whose last bar lags the universe's last bar are excluded rather than ranked on stale data
    last = close.index[-1]
    fresh = [c for c in close.columns if pd.notna(close[c].iloc[-1])]
    close, high, vol = close[fresh].tail(LOOKBACK_BARS), high[fresh].tail(LOOKBACK_BARS), vol[fresh].tail(LOOKBACK_BARS)
    if close.shape[1] < MIN_UNIVERSE or len(close) < MIN_BARS:
        return last, None
    return last, composite_percentiles(close, high, vol)


def rank_for(db: Session, instrument_id: UUID) -> dict[str, Any]:
    """{'status': 'available', 'percentile': 0-100, ...} or {'status': 'unavailable', 'reason': ...}. Never a favourable default."""
    with _lock:
        if _cache["table"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            key, table = _load_table(db)
            _cache.update(at=time.monotonic(), key=key, table=table)
        key, table = _cache["key"], _cache["table"]
    if table is None:
        return {"status": "unavailable", "reason": "Not enough stored Dhan daily history across the universe to rank this stock."}
    row = table.loc[str(instrument_id)] if str(instrument_id) in table.index else None
    if row is None or pd.isna(row["composite_pct"]):
        return {"status": "unavailable", "reason": "This stock lacks enough current Dhan daily history (about 1 year) to be ranked against the universe."}
    return {"status": "available", "percentile": round(float(row["composite_pct"]), 1), "as_of": str(pd.Timestamp(key).date()), "universe_size": int(table["composite_pct"].notna().sum()),
            "members": [{"key": k, "label": label, "percentile": round(float(row[k]) * 100, 1)} for k, label in MEMBERS.items()],
            "note": "Rank versus the stored NSE universe (decision support, not investment advice). A ranking, not a probability of profit; edge in replays was real but thin."}
