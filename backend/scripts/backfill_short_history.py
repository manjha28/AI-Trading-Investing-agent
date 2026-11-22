"""Backfill daily history (Dhan only) for stocks that have fewer than 20 stored sessions. Root cause it works around: a long request window (700 days) that starts before a recent listing makes Dhan return
no rows, so ``research_pipeline.ensure_daily_history`` stored nothing and then marked the stock failed. Narrow windows (60/200/500 days back from today) succeed. Never fabricates data; Dhan only.
Run from backend/:  python scripts/backfill_short_history.py [--min 20]"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.market_data import sync_history  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Instrument, MarketBar, User  # noqa: E402
from app.services import research_pipeline as rp  # noqa: E402

MIN = int(sys.argv[sys.argv.index("--min") + 1]) if "--min" in sys.argv else 20
WINDOWS = ((60, 0), (200, 60), (500, 200), (1000, 500))
with Session(engine) as db:
    uid = db.scalar(select(User.id).where(User.email_verified.is_(True)))
    user, prov, now = db.get(User, uid), rp.dhan_only_provider(db, uid), datetime.now(timezone.utc)
    count = lambda iid: int(db.scalar(select(func.count(MarketBar.id)).where(MarketBar.instrument_id == iid, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False))) or 0)  # noqa: E731
    short = [i for i in db.scalars(select(Instrument).where(Instrument.asset_class == "equity", Instrument.country == "IN", Instrument.is_synthetic.is_(False), Instrument.venue == "NSE")) if 0 < count(i.id) < MIN]
    print(len(short), "instruments with fewer than", MIN, "stored sessions", flush=True)
    for i in short:
        before = count(i.id)
        for a, b in WINDOWS:
            try:
                sync_history(instrument_id=i.id, start=now - timedelta(days=a), end=now - timedelta(days=b), background_tasks=None, interval="1d", limit=500, provider=prov, _user=user, db=db)
            except Exception as exc:  # noqa: BLE001 - a window before the listing date returns nothing; stop going further back
                db.rollback()
                print(f"  {i.symbol} window {a}-{b}d: {str(getattr(exc, 'detail', exc))[:140]}", flush=True)
                break
        first = db.scalar(select(func.min(MarketBar.timestamp)).where(MarketBar.instrument_id == i.id, MarketBar.interval == "1d"))
        print(f"{i.symbol}: {before} -> {count(i.id)} sessions, first {first.date() if first else None}", flush=True)
