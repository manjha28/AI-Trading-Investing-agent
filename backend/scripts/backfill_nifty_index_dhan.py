"""Backfill the NIFTY 50 INDEX daily bars from DHAN (segment IDX_I, security id 13, instrument INDEX) into market_bars as source 'dhan', for the same instrument (^NSEI) that holds the Yahoo
fallback series. Dhan is the paid primary source; Yahoo rows stay as a labelled fallback. Read-only market-data calls with the owner's stored Dhan session (no order endpoints).
Compares Dhan with Yahoo on overlapping dates and reports every day whose close differs by more than 0.1%. Run from backend/:  python scripts/backfill_nifty_index_dhan.py [--from 2015-01-01]"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.market_data import _market_data_credentials  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import BrokerCredential, Instrument, MarketBar  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2015-01-01")
    args = ap.parse_args()
    with Session(engine) as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if cred is None:
            print("No Dhan credential is stored.")
            return 1
        try:
            _, provider, _ = _market_data_credentials(db, cred.user_id, "dhan")
        except Exception as exc:  # noqa: BLE001
            print(f"Dhan session unavailable: {type(exc).__name__}")
            return 1
        if provider is None:
            print("Dhan is not connected or its session has expired.")
            return 1
        adapter, token, tokens = provider.adapter, provider.tokens["access_token"], provider.tokens
        inst = db.scalar(select(Instrument).where(Instrument.symbol == "^NSEI", Instrument.asset_class == "index"))
        if inst is None:
            print("Run scripts/backfill_nifty_index.py first (creates the ^NSEI instrument).")
            return 1
        start = datetime.fromisoformat(args.start).replace(tzinfo=IST)
        end = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        bars, cur, errors = [], start, []
        while cur < end:
            nxt = min(cur + timedelta(days=600), end)
            try:
                got = adapter.get_historical_bars(token, tokens, "IDX_I", "13", "1d", cur.astimezone(timezone.utc), nxt.astimezone(timezone.utc), instrument="INDEX")
                bars += got
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{cur.date()}..{nxt.date()}: {type(exc).__name__}: {str(exc)[:100]}")
            cur = nxt
        uniq = {b.timestamp: b for b in bars}
        print(f"Dhan returned {len(uniq)} daily index bars" + (f" ({min(uniq).astimezone(IST).date()}..{max(uniq).astimezone(IST).date()})" if uniq else "") + f"; window errors: {len(errors)}")
        for e in errors[:6]:
            print("  ", e)
        if not uniq:
            return 1
        now = datetime.now(timezone.utc)
        rows = [{"id": uuid4(), "instrument_id": inst.id, "interval": "1d", "timestamp": b.timestamp, "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
                 "source": "dhan", "retrieved_at": now, "is_synthetic": False} for b in sorted(uniq.values(), key=lambda b: b.timestamp)]
        for i in range(0, len(rows), 500):
            db.execute(insert(MarketBar).values(rows[i:i + 500]).on_conflict_do_nothing(constraint="uq_market_bars_instrument_interval_time_source"))
        db.commit()
        cmp = db.execute(text("""select (d.timestamp at time zone 'Asia/Kolkata')::date as day, d.close as dhan, y.close as yahoo, abs(d.close / y.close - 1) * 100 as diff_pct
            from market_bars d join market_bars y on y.instrument_id = d.instrument_id and y.interval = '1d' and y.source = 'yfinance' and (y.timestamp at time zone 'Asia/Kolkata')::date = (d.timestamp at time zone 'Asia/Kolkata')::date
            where d.instrument_id = :i and d.interval = '1d' and d.source = 'dhan' order by 1"""), {"i": inst.id}).all()
        big = [(str(r[0]), float(r[1]), float(r[2]), round(float(r[3]), 3)) for r in cmp if float(r[3]) > 0.1]
        only_y = db.scalar(text("select count(*) from market_bars y where y.instrument_id=:i and y.source='yfinance' and (y.timestamp at time zone 'Asia/Kolkata')::date < (select min((timestamp at time zone 'Asia/Kolkata')::date) from market_bars where instrument_id=:i and source='dhan')"), {"i": inst.id})
        print(f"overlap {len(cmp)} days; days with |Dhan/Yahoo - 1| > 0.1%: {len(big)}; Yahoo-only days before Dhan's first bar: {only_y}")
        for r in big[:15]:
            print("  ", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
