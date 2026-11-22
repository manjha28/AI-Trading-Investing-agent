"""Seed price_crosscheck flags from the Phase 4c offline audit: stocks whose Dhan history disagrees with Yahoo's split-adjusted close (15 stocks, likely rights-issue/demerger adjustments that need the NSE
corporate-actions record to settle). They are excluded from backtests until the owner resolves them. Reads diagnostics/nexus-v2/phase4c_affected.json (list "B"). Run from backend/."""
import json
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Instrument, MarketBar  # noqa: E402
from app.models.price_crosscheck import PriceCrosscheck  # noqa: E402

syms = json.load(open("../diagnostics/nexus-v2/phase4c_affected.json"))["B"]
now = datetime.now(timezone.utc)
with Session(engine) as db:
    for sym in syms:
        inst = db.scalar(select(Instrument).where(Instrument.symbol == sym, Instrument.venue == "NSE"))
        bar = db.scalar(select(MarketBar).where(MarketBar.instrument_id == inst.id, MarketBar.interval == "1d", MarketBar.source == "dhan").order_by(MarketBar.timestamp.desc()).limit(1))
        d: date = bar.timestamp.date()
        if db.scalar(select(PriceCrosscheck.id).where(PriceCrosscheck.instrument_id == inst.id, PriceCrosscheck.reference_source == "yahoo_close_audit")):
            continue
        db.add(PriceCrosscheck(instrument_id=inst.id, session_date=d, dhan_close=float(bar.close), reference_close=float(bar.close), deviation=0.0, reference_source="yahoo_close_audit", status="flagged", first_flagged_at=now, checked_at=now))
    db.commit()
    print("flagged", len(syms))
