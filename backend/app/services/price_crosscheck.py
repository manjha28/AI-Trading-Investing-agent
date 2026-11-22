"""Nightly cross-check: stored Dhan daily close vs the NSE bhavcopy close for the same session (Dhan is primary; the bhavcopy is only the reference, never a replacement). Over the last ``LOOKBACK``
completed sessions, any stock whose two closes differ by more than 1% is FLAGGED (``price_crosscheck``) and excluded from backtests until the next run finds it within 1% again (then 'resolved').
Recent sessions are compared as traded, so a legitimate split or bonus is not a false alarm: Dhan history is split-adjusted but its latest closes are the actual prices. A reference outage records
nothing (never flags, never clears). History-wide adjustment disagreements are examined by the offline audit (diagnostics/nexus-v2/phase4c_*)."""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)
TOLERANCE = 0.01
LOOKBACK = 5
SERIES = {"EQ", "BE", "BZ", "SM", "ST"}
URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip"


def fetch_bhavcopy_closes(day: date, *, http_get: Any = None) -> dict[str, float] | None:
    """{symbol: close} for one session from the public NSE bhavcopy (equity series only); None if the file is not available."""
    import csv
    try:
        if http_get is None:
            import httpx
            r = httpx.get(URL.format(d=day.strftime("%Y%m%d")), headers={"User-Agent": "Mozilla/5.0 (compatible; research-client)", "Accept": "*/*"}, timeout=30, follow_redirects=True)
            status, content = r.status_code, r.content
        else:
            status, content = http_get(URL.format(d=day.strftime("%Y%m%d")))
        if status != 200:
            return None
        z = zipfile.ZipFile(io.BytesIO(content))
        rows = csv.DictReader(io.TextIOWrapper(z.open(z.namelist()[0]), encoding="utf-8"))
        return {r["TckrSymb"].strip().upper(): float(r["ClsPric"]) for r in rows if r.get("SctySrs", "").strip() in SERIES and r.get("ClsPric")}
    except Exception as exc:  # noqa: BLE001 - an unreachable reference must never break the nightly cycle
        logger.warning("bhavcopy_fetch_failed error_class=%s", type(exc).__name__)
        return None


def deviation(dhan: float, ref: float) -> float:
    return dhan / ref - 1


def run(db: Any, *, now: datetime | None = None, fetch: Any = None, lookback: int = LOOKBACK) -> dict[str, int]:
    """Compare the latest ``lookback`` stored sessions. Returns counts. Writes only flagged/resolved rows."""
    from sqlalchemy import select

    from app.models import Instrument, MarketBar
    from app.models.price_crosscheck import PriceCrosscheck
    now = now or datetime.now(timezone.utc)
    fetch = fetch or fetch_bhavcopy_closes
    stamps = db.scalars(select(MarketBar.timestamp).where(MarketBar.interval == "1d", MarketBar.source == "dhan").order_by(MarketBar.timestamp.desc()).limit(4000))
    days = sorted({t.date() for t in stamps}, reverse=True)[:lookback]
    out = {"sessions": 0, "compared": 0, "flagged": 0, "resolved": 0, "reference_missing": 0}
    inst = {i.symbol.upper(): i for i in db.scalars(select(Instrument).where(Instrument.venue == "NSE", Instrument.asset_class == "equity", Instrument.is_synthetic.is_(False)))}
    for d in days:
        ref = fetch(d)
        if ref is None:
            out["reference_missing"] += 1
            continue
        out["sessions"] += 1
        lo, hi = datetime(d.year, d.month, d.day, tzinfo=timezone.utc), datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
        by_id = {x.id: s for s, x in inst.items()}
        for b in db.scalars(select(MarketBar).where(MarketBar.interval == "1d", MarketBar.source == "dhan", MarketBar.timestamp >= lo, MarketBar.timestamp < hi, MarketBar.instrument_id.in_(list(by_id)))):
            sym = by_id[b.instrument_id]
            r = ref.get(sym)
            if r is None or r <= 0:
                continue
            dev = deviation(float(b.close), r)
            out["compared"] += 1
            row = db.scalar(select(PriceCrosscheck).where(PriceCrosscheck.instrument_id == b.instrument_id, PriceCrosscheck.session_date == d, PriceCrosscheck.reference_source == "nse_bhavcopy"))
            if abs(dev) > TOLERANCE:
                if row is None:
                    db.add(PriceCrosscheck(instrument_id=b.instrument_id, session_date=d, dhan_close=float(b.close), reference_close=r, deviation=dev, reference_source="nse_bhavcopy", status="flagged", first_flagged_at=now, checked_at=now))
                    out["flagged"] += 1
                else:
                    row.dhan_close, row.reference_close, row.deviation, row.checked_at, row.status = float(b.close), r, dev, now, "flagged"
            else:
                if row is not None and row.status == "flagged":
                    row.status, row.checked_at, row.dhan_close, row.reference_close, row.deviation = "resolved", now, float(b.close), r, dev
                    out["resolved"] += 1
    db.commit()
    return out


def flagged_instrument_ids(db: Any) -> set[Any]:
    """Instruments with an open flag (a Dhan close more than 1% from the bhavcopy close that has not been found within 1% since). Backtests skip them."""
    from sqlalchemy import select

    from app.models.price_crosscheck import PriceCrosscheck
    return set(db.scalars(select(PriceCrosscheck.instrument_id).where(PriceCrosscheck.status == "flagged")))


def log_nightly(db: Any) -> None:
    try:
        r = run(db)
        (logger.warning if r["flagged"] else logger.info)("price_crosscheck %s", r)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("price_crosscheck_failed error_class=%s", type(exc).__name__)
