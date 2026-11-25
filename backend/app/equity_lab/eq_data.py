"""NSE daily panel from the app's own database (Dhan bars, source 'dhan', real bars only), for the 2026-11-26 literature round. Hypothetical; decision support, not investment advice.
Universe: the app's 264 stock instruments (not a Nifty list: index membership is not stored). 'Nifty 200 proxy' = the 200 highest by trailing 120-day median turnover (close x volume) at each date
(point in time, shifted one day); 'large' = the top 50 of those, 'mid' = ranks 51-200. Benchmark: NIFTYBEES (a Nifty 50 ETF, Dhan bars) because the Dhan index series ends 19 Nov.
Bars are as stored (adjusted status unknown): a close-to-close move above 35% is treated as a probable split/bonus and set to no-return, and counted."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

ENV = Path(__file__).resolve().parents[2] / ".env"


def engine():
    return create_engine(re.search(r"DATABASE_URL=(.*)", ENV.read_text()).group(1).strip())


@lru_cache(maxsize=1)
def panel() -> dict:
    e = engine()
    d = pd.read_sql(text("""select i.symbol, b.timestamp::date as d, b.open, b.high, b.low, b.close, b.volume from instruments i join market_bars b on b.instrument_id=i.id
        where b.interval='1d' and b.source='dhan' and b.is_synthetic is false and i.instrument_kind='stock' order by 1,2"""), e)
    nb = pd.read_sql(text("""select b.timestamp::date as d, b.close from instruments i join market_bars b on b.instrument_id=i.id
        where b.interval='1d' and b.source='dhan' and i.symbol='NIFTYBEES' order by 1"""), e).set_index("d").close
    nsei = pd.read_sql(text("""select (b.timestamp at time zone 'Asia/Kolkata')::date as d, b.open, b.close from instruments i join market_bars b on b.instrument_id=i.id
        where b.interval='1d' and b.source='dhan' and i.symbol='^NSEI' order by 1"""), e).set_index("d")
    w = {k: d.pivot(index="d", columns="symbol", values=k) for k in ("open", "high", "low", "close", "volume")}
    n = w["close"].notna().sum(1)
    days = n[n >= 100].index
    w = {k: v.loc[days].astype(float) for k, v in w.items()}
    C = w["close"]
    r = C / C.shift(1) - 1
    bad = (r.abs() > 0.35)
    n_bad = int(bad.sum().sum())
    ratio = (1 + r.fillna(0)).where(bad, 1.0)
    scale = ratio.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)       # product of the jump ratios AFTER each date: earlier prices are rescaled so the jump vanishes
    out = {k: (w[k] * scale).to_numpy() for k in ("open", "high", "low", "close")}
    out["volume"] = w["volume"].to_numpy()
    out["names"] = np.array(C.columns)
    out["dates"] = pd.to_datetime(np.array(days))
    out["t"] = (out["dates"].astype("datetime64[s]").astype("int64"))
    out["nifty"] = nb.reindex(days).ffill().to_numpy(dtype=float)
    out["nsei"] = nsei
    out["n_jumps"] = n_bad
    to = pd.DataFrame(out["close"] * out["volume"]).rolling(120, min_periods=60).median().shift(1)
    rank = to.rank(axis=1, ascending=False).to_numpy()
    out["rank"] = rank
    return out
