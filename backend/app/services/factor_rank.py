"""Nexus v2 Phase 2: the "factor rank" that replaces the confidence badge (flag ``confidence_v2_enabled``; v1 universe_rank.py is untouched).

A transparent screen, NOT a confidence and NOT a probability. One batch, one universe, one completed candle for every card: the as-of date is the latest session on which at least
``MIN_FRESH_SHARE`` of the stored universe has a bar, so a card computed while the day's candles are only partly loaded cannot see a smaller universe (NX-015).

Factors (percentiles 0-100 across that universe, last completed daily bar), merged into GROUPS so overlapping factors count once:
  trend_quality    mean of low volatility (60d), proximity to the 52-week high and, for positional / long-term only, 12-1 month momentum
  volume_flow      up-day volume / down-day volume over 20 sessions (volume is SIGNED by price direction; v1's relative volume rewarded heavy selling)
  relative_strength  mean percentile of the return minus the NIFTY 50 return over 20 and 60 sessions
  reversal         swing only: -(10-day return), applied ONLY while the 200-day average is rising (otherwise not applicable, dropped; a falling knife is not "oversold")
Composite per horizon = weighted mean of the applicable groups (0-100), weights in GROUP_WEIGHTS. Rank = Nth of M by composite; percentile = share of the universe with a lower composite.
GROUP_WEIGHTS come from the rank-IC test on a survivor-free bhavcopy panel (2019-2026, top-350 liquid names, weekly): weights were chosen on 2019-2022 and checked on 2023-2026
(diagnostics/nexus-v2/phase-2-calibration.md). Equal weights had IC +0.008 (swing, 5d) and +0.005 (positional, 20d); signed volume flow had NEGATIVE IC (t -2.2 at 20d), so it is shown
with weight 0. These are hypotheses to be re-run, not validated rules. Pure functions plus a cached loader.
"""
from __future__ import annotations

import threading
import time
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

VERSION = "factor-rank/1"
HORIZONS = ("swing", "positional", "long_term")
GROUPS = {"swing": ("trend_quality", "relative_strength", "reversal", "volume_flow"), "positional": ("trend_quality", "relative_strength", "volume_flow"),
          "long_term": ("trend_quality", "relative_strength", "volume_flow")}
GROUP_WEIGHTS = {"swing": {"trend_quality": 0.6, "relative_strength": 0.2, "reversal": 0.2, "volume_flow": 0.0},
                 "positional": {"trend_quality": 0.75, "relative_strength": 0.25, "volume_flow": 0.0}, "long_term": {"trend_quality": 0.75, "relative_strength": 0.25, "volume_flow": 0.0}}
GROUP_LABELS = {"trend_quality": "Trend quality (low volatility, near 52-week high" + ", 12-1 momentum)", "volume_flow": "Volume flow (up-day vs down-day volume, 20d)",
                "relative_strength": "Relative strength vs NIFTY 50 (20d and 60d)", "reversal": "Short-term reversal (10d, only with a rising 200-day average)"}
MIN_BARS, MIN_UNIVERSE, LOOKBACK_BARS, CACHE_SECONDS, MIN_FRESH_SHARE = 260, 40, 300, 6 * 3600, 0.90
_cache: dict[str, Any] = {"at": 0.0, "key": None, "table": None, "meta": None}
_lock = threading.Lock()


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True) * 100


def raw_factors(close: pd.DataFrame, volume: pd.DataFrame, bench: pd.Series | None) -> pd.DataFrame:
    """Raw factor values on the LAST row, one row per stock. Pure function of completed candles."""
    r = close.pct_change()
    up = (close.diff() > 0)
    dn = (close.diff() < 0)
    upv = (volume * up).rolling(20, min_periods=15).sum().iloc[-1]          # tolerate a few missing sessions (>= 15 of 20, 45 of 60, 180 of 200): a gap must not silently drop a stock
    dnv = (volume * dn).rolling(20, min_periods=15).sum().iloc[-1]
    flow = np.log((upv + 1.0) / (dnv + 1.0))                                  # symmetric, finite; > 0 = buying volume dominates
    sma200 = close.rolling(200, min_periods=180).mean()
    raw = pd.DataFrame({
        "lowvol": -r.rolling(60, min_periods=45).std().iloc[-1],
        "near_52w_high": close.iloc[-1] / close.rolling(252, min_periods=200).max().iloc[-1],
        "mom_12_1": close.iloc[-22] / close.iloc[-253] - 1,
        "reversal_10": -(close.iloc[-1] / close.iloc[-11] - 1),
        "ma200_rising": (sma200.iloc[-1] > sma200.iloc[-21]).astype(float).where(sma200.iloc[-1].notna() & sma200.iloc[-21].notna()),
        "vol_flow": flow.where((upv + dnv) > 0),
    })
    for n in (20, 60):
        stock = close.iloc[-1] / close.iloc[-1 - n] - 1
        ref = float(bench.iloc[-1] / bench.iloc[-1 - n] - 1) if bench is not None and len(bench) > n else np.nan
        raw[f"rs_{n}"] = stock - ref
    return raw


def rank_table(close: pd.DataFrame, volume: pd.DataFrame, bench: pd.Series | None) -> dict[str, pd.DataFrame]:
    """{horizon: DataFrame(group percentiles..., composite, rank, percentile)} plus raw values under key 'raw'."""
    raw = raw_factors(close, volume, bench)
    p = pd.DataFrame({k: _pct(raw[k]) for k in ("lowvol", "near_52w_high", "mom_12_1", "reversal_10", "vol_flow", "rs_20", "rs_60")})
    out: dict[str, pd.DataFrame] = {"raw": raw}
    for h in HORIZONS:
        g = pd.DataFrame(index=raw.index)
        g["trend_quality"] = (p[["lowvol", "near_52w_high"]].mean(axis=1, skipna=False) if h == "swing" else p[["lowvol", "near_52w_high", "mom_12_1"]].mean(axis=1, skipna=False))
        g["volume_flow"] = p["vol_flow"]
        g["relative_strength"] = p[["rs_20", "rs_60"]].mean(axis=1, skipna=False)
        if h == "swing":
            g["reversal"] = p["reversal_10"].where(raw["ma200_rising"] == 1.0)
        w = pd.Series(GROUP_WEIGHTS[h])
        scored = [c for c in g.columns if w[c] > 0]
        required = [c for c in scored if c != "reversal"]                  # the conditional reversal group may be not-applicable; every other weighted group must exist
        num = sum(g[c].fillna(0) * w[c] for c in scored)
        den = sum(g[c].notna() * w[c] for c in scored)
        g["composite"] = (num / den).where(g[required].notna().all(axis=1) & (den > 0))
        valid = g["composite"].notna()
        g["of"] = int(valid.sum())
        g["rank"] = g["composite"].rank(ascending=False, method="min")
        g["percentile"] = g["composite"].rank(pct=True) * 100
        out[h] = g
    return out


def _load(db: Session) -> tuple[Any, dict[str, pd.DataFrame] | None, dict[str, Any]]:
    from app.services import research_pipeline as rp
    rows = db.execute(text("""
        select i.id::text as iid, b.timestamp::date as d, b.close, b.volume from market_bars b join instruments i on i.id = b.instrument_id
        where b.interval='1d' and b.source='dhan' and b.is_synthetic=false and i.is_synthetic=false and i.asset_class='equity' and i.country='IN'
          and b.timestamp >= (select max(timestamp) from market_bars where interval='1d' and source='dhan') - interval '700 days'""")).all()
    if not rows:
        return None, None, {}
    df = pd.DataFrame(rows, columns=["iid", "d", "close", "volume"]).astype({"close": float, "volume": float})
    df["d"] = pd.to_datetime(df["d"])
    df = df.drop_duplicates(["iid", "d"])
    close, vol = (df.pivot(index="d", columns="iid", values=c).sort_index() for c in ("close", "volume"))
    keep = close.columns[close.count() >= MIN_BARS]
    close, vol = close[keep], vol[keep]
    share = close.notna().mean(axis=1)                                         # as-of = latest session on which the universe is (nearly) fully present
    complete = share[share >= MIN_FRESH_SHARE]
    if complete.empty:
        return None, None, {}
    as_of = complete.index[-1]
    close, vol = close.loc[:as_of], vol.loc[:as_of]
    fresh = [c for c in close.columns if pd.notna(close[c].iloc[-1])]
    close, vol = close[fresh].tail(LOOKBACK_BARS), vol[fresh].tail(LOOKBACK_BARS)
    if close.shape[1] < MIN_UNIVERSE or len(close) < MIN_BARS:
        return as_of, None, {}
    from app.services import benchmark_index
    bench, bench_info = benchmark_index.index_series(db)            # NIFTY 50 INDEX, Dhan first; v1's tracking ETF only if no index bars are stored
    source = "NIFTY 50 index (Dhan)" if bench is not None and bench_info.get("primary") == "dhan" else "NIFTY 50 index (Yahoo fallback)" if bench is not None else None
    if bench is None or len(bench) < 150:
        bench, source = rp._benchmark_series(db), "NIFTY 50 tracking ETF"
    bench = None if bench is None else bench.loc[:as_of]
    return as_of, rank_table(close, vol, bench), {"universe_size": int(close.shape[1]), "bench_available": bench is not None, "benchmark_source": source, "version": VERSION}


def rank_for(db: Session, instrument_id: UUID) -> dict[str, Any]:
    """Per-card factor-rank section ({'status': 'available', 'horizons': {...}} or {'status': 'unavailable', 'reason': ...}); never a favourable default."""
    with _lock:
        if _cache["table"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            key, table, meta = _load(db)
            _cache.update(at=time.monotonic(), key=key, table=table, meta=meta)
        key, table, meta = _cache["key"], _cache["table"], _cache["meta"]
    if table is None:
        return {"status": "unavailable", "reason": "Not enough stored Dhan daily history across the universe to compute the factor rank."}
    iid = str(instrument_id)
    if iid not in table["raw"].index:
        return {"status": "unavailable", "reason": "This stock lacks enough current Dhan daily history (about 1 year) to be in the factor screen."}
    raw = table["raw"].loc[iid]
    horizons: dict[str, Any] = {}
    for h in HORIZONS:
        row = table[h].loc[iid]
        if pd.isna(row["composite"]):
            horizons[h] = {"status": "unavailable", "reason": "A required factor group could not be computed for this stock."}
            continue
        groups = [{"key": g, "label": GROUP_LABELS[g], "percentile": None if pd.isna(row[g]) else round(float(row[g]), 1), "weight": GROUP_WEIGHTS[h][g]} for g in GROUPS[h]]
        horizons[h] = {"status": "available", "rank": int(row["rank"]), "of": int(row["of"]), "composite": round(float(row["composite"]), 1), "percentile": round(float(row["percentile"]), 1),
                       "groups": groups, "applicable_groups": [g["key"] for g in groups if g["percentile"] is not None and g["weight"] > 0]}
    return {"status": "available", "version": VERSION, "as_of": str(pd.Timestamp(key).date()), "universe_size": meta["universe_size"], "benchmark": meta.get("benchmark_source") if meta["bench_available"] else None,
            "inputs": {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in raw.items()}, "horizons": horizons,
            "label": "Factor rank (factor screen)", "note": "A transparent ranking of how a stock screens on trend quality, volume flow and relative strength versus the stored NSE universe on one completed candle. "
                                                            "Not a confidence, not a probability of profit, and not a buy signal; a stock only qualifies after its setup checks pass."}
