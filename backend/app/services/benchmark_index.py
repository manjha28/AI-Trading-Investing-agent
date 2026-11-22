"""NIFTY 50 INDEX daily closes, Dhan first (the paid source), Yahoo (^NSEI) only for dates Dhan does not have. Used by the Nexus v2 factor rank and the calibration scripts; v1's
tracking-ETF benchmark (research_pipeline._benchmark_series) is unchanged. Source of every value is kept: bars are stored per source in market_bars (``dhan`` / ``yfinance``)."""
from __future__ import annotations

from typing import Any

from datetime import timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

SYMBOL = "^NSEI"
IST = ZoneInfo("Asia/Kolkata")


def index_series(db: Session) -> tuple[pd.Series | None, dict[str, Any]]:
    from app.models import Instrument, MarketBar
    inst_id = db.scalar(select(Instrument.id).where(Instrument.symbol == SYMBOL, Instrument.asset_class == "index"))
    rows = [] if inst_id is None else db.execute(select(MarketBar.timestamp, MarketBar.close, MarketBar.source).where(
        MarketBar.instrument_id == inst_id, MarketBar.interval == "1d", MarketBar.source.in_(("dhan", "yfinance")))).all()
    dates = lambda ts: pd.Timestamp((ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(IST).date())      # noqa: E731 - the session date in IST
    dhan = {dates(ts): float(c) for ts, c, src in rows if src == "dhan"}
    yahoo = {dates(ts): float(c) for ts, c, src in rows if src == "yfinance"}
    if not dhan and not yahoo:
        return None, {"status": "unavailable", "reason": "No NIFTY 50 index bars are stored (run scripts/backfill_nifty_index.py and scripts/backfill_nifty_index_dhan.py)."}
    merged = {**yahoo, **dhan}                                    # Dhan wins on every date it has
    s = pd.Series(merged).sort_index()
    fallback_days = sorted(d for d in yahoo if d not in dhan)
    info = {"status": "available", "primary": "dhan" if dhan else "yfinance", "dhan_days": len(dhan), "yahoo_fallback_days": len(fallback_days), "bars": len(s),
            "first": str(s.index[0].date()), "last": str(s.index[-1].date()), "first_dhan": str(min(dhan).date()) if dhan else None,
            "join": f"Yahoo used only before {min(dhan).date()} and on {len([d for d in fallback_days if d > min(dhan)])} later gap days" if dhan and fallback_days else None}
    return s, info
