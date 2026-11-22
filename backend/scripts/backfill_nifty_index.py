"""Backfill NIFTY 50 INDEX daily bars from Yahoo (^NSEI) into market_bars (additive, idempotent). Real Yahoo data only; stored as its own instrument (symbol ^NSEI, asset_class 'index',
source 'yfinance') so the existing tracking-ETF benchmark used by v1 is untouched. Respects the Yahoo circuit breaker. Run from backend/:  python scripts/backfill_nifty_index.py [--from 2015-01-01]"""
import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import yahoo_guard  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Instrument, MarketBar  # noqa: E402

SYMBOL, SOURCE = "^NSEI", "yfinance"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2015-01-01")
    args = ap.parse_args()
    if not yahoo_guard.allowed():
        print(f"Yahoo circuit breaker is open for {yahoo_guard.seconds_left()}s; not fetching.")
        return 1
    import yfinance as yf
    try:
        hist = yf.Ticker(SYMBOL).history(start=args.start, end=datetime.now(timezone.utc).date().isoformat(), interval="1d", auto_adjust=False)
        yahoo_guard.record(hist is not None and len(hist) > 0)
    except Exception as exc:  # noqa: BLE001
        yahoo_guard.record(False)
        print(f"Yahoo fetch failed: {type(exc).__name__}")
        return 1
    hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
    hist = hist[(hist[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        inst = db.scalar(select(Instrument).where(Instrument.symbol == SYMBOL, Instrument.asset_class == "index"))
        if inst is None:
            inst = Instrument(id=uuid4(), symbol=SYMBOL, display_name="NIFTY 50 index (Yahoo ^NSEI)", asset_class="index", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata",
                              multiplier=None, is_synthetic=False, relink_required=False, created_at=now)
            db.add(inst)
            db.flush()
        rows = []
        for ts, r in hist.iterrows():
            o, h, l, c = (Decimal(str(round(float(r[k]), 2))) for k in ("Open", "High", "Low", "Close"))
            h, l = max(h, o, c), min(l, o, c)
            rows.append({"id": uuid4(), "instrument_id": inst.id, "interval": "1d", "timestamp": ts.to_pydatetime(), "open": o, "high": h, "low": l, "close": c,
                         "volume": Decimal(str(float(r.get("Volume", 0) or 0))), "source": SOURCE, "retrieved_at": now, "is_synthetic": False})
        done = 0
        for i in range(0, len(rows), 500):
            res = db.execute(insert(MarketBar).values(rows[i:i + 500]).on_conflict_do_nothing(constraint="uq_market_bars_instrument_interval_time_source"))
            done += res.rowcount or 0
        db.commit()
        print(f"{SYMBOL}: fetched {len(rows)} daily bars {hist.index[0].date()}..{hist.index[-1].date()}; inserted {done} new (existing rows untouched).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
