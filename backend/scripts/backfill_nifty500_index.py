"""Backfill the NIFTY 500 INDEX (price index, not total return) daily bars: Dhan first (segment IDX_I, security id 19, instrument INDEX), Yahoo (^CRSLDX) only for dates Dhan does not return. Stored as its own
instrument (symbol ^CRSLDX, asset_class 'index') with the source kept per bar. Read-only market-data calls (no order endpoints). Run from backend/:  python scripts/backfill_nifty500_index.py [--from 2015-01-01]"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import yahoo_guard  # noqa: E402
from app.api.market_data import _market_data_credentials  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import BrokerCredential, Instrument, MarketBar  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SYMBOL = "^CRSLDX"


def _insert(db: Session, rows: list[dict]) -> int:
    done = 0
    for i in range(0, len(rows), 500):
        res = db.execute(insert(MarketBar).values(rows[i:i + 500]).on_conflict_do_nothing(constraint="uq_market_bars_instrument_interval_time_source"))
        done += res.rowcount or 0
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2015-01-01")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        inst = db.scalar(select(Instrument).where(Instrument.symbol == SYMBOL, Instrument.asset_class == "index"))
        if inst is None:
            inst = Instrument(id=uuid4(), symbol=SYMBOL, display_name="NIFTY 500 index (price index, Dhan IDX_I 19)", asset_class="index", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata",
                              multiplier=None, is_synthetic=False, relink_required=False, created_at=now)
            db.add(inst)
            db.flush()
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        dhan_days: set = set()
        if cred is None:
            print("No Dhan credential is stored; Dhan step skipped.")
        else:
            try:
                _, provider, _ = _market_data_credentials(db, cred.user_id, "dhan")
            except Exception as exc:  # noqa: BLE001
                provider = None
                print(f"Dhan session unavailable: {type(exc).__name__}")
            if provider is not None:
                adapter, token, tokens = provider.adapter, provider.tokens["access_token"], provider.tokens
                start = datetime.fromisoformat(args.start).replace(tzinfo=IST)
                end = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
                bars, cur, errors = [], start, []
                while cur < end:
                    nxt = min(cur + timedelta(days=600), end)
                    try:
                        bars += adapter.get_historical_bars(token, tokens, "IDX_I", "19", "1d", cur.astimezone(timezone.utc), nxt.astimezone(timezone.utc), instrument="INDEX")
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{cur.date()}..{nxt.date()}: {type(exc).__name__}")
                    cur = nxt
                uniq = {b.timestamp: b for b in bars}
                rows = [{"id": uuid4(), "instrument_id": inst.id, "interval": "1d", "timestamp": b.timestamp, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
                         "source": "dhan", "retrieved_at": now, "is_synthetic": False} for b in sorted(uniq.values(), key=lambda b: b.timestamp)]
                dhan_days = {b.timestamp.astimezone(IST).date() for b in uniq.values()}
                print(f"Dhan returned {len(uniq)} NIFTY 500 daily bars" + (f" ({min(dhan_days)}..{max(dhan_days)})" if dhan_days else "") + f"; window errors: {len(errors)} {errors[:4]}; inserted {_insert(db, rows)}")
        if yahoo_guard.allowed():
            import yfinance as yf
            try:
                hist = yf.Ticker(SYMBOL).history(start=args.start, end=datetime.now(timezone.utc).date().isoformat(), interval="1d", auto_adjust=False)
                yahoo_guard.record(hist is not None and len(hist) > 0)
            except Exception as exc:  # noqa: BLE001
                yahoo_guard.record(False)
                hist = None
                print(f"Yahoo fetch failed: {type(exc).__name__}")
            if hist is not None and len(hist):
                hist = hist.dropna(subset=["Open", "High", "Low", "Close"])
                rows = []
                for ts, r in hist.iterrows():
                    if ts.date() in dhan_days:
                        continue                                    # Yahoo only for dates Dhan did not return
                    o, h, l, c = (Decimal(str(round(float(r[k]), 2))) for k in ("Open", "High", "Low", "Close"))
                    rows.append({"id": uuid4(), "instrument_id": inst.id, "interval": "1d", "timestamp": ts.to_pydatetime(), "open": o, "high": max(h, o, c), "low": min(l, o, c), "close": c,
                                 "volume": Decimal(str(float(r.get("Volume", 0) or 0))), "source": "yfinance", "retrieved_at": now, "is_synthetic": False})
                print(f"Yahoo fallback: {len(rows)} dates Dhan did not have; inserted {_insert(db, rows)}")
        else:
            print(f"Yahoo circuit breaker open for {yahoo_guard.seconds_left()}s; no Yahoo fallback.")
        db.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
