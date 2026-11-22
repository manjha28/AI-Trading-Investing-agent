"""Dhan-vs-bhavcopy cross-check (test data: hand-made bars and a fake bhavcopy fetcher; no network)."""
import io
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, MarketBar
from app.services import price_crosscheck as pc

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _inst(db, sym):
    i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _bar(db, inst, day, close):
    t = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    db.add(MarketBar(id=uuid4(), instrument_id=inst.id, interval="1d", timestamp=t, open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=Decimal("1"), source="dhan", retrieved_at=t, is_synthetic=False))
    db.commit()


def test_a_close_more_than_one_percent_off_is_flagged_and_a_clean_stock_is_not(db):
    from datetime import date
    a, b = _inst(db, "AAA"), _inst(db, "BBB")
    d = date(2026, 11, 20)
    _bar(db, a, d, "100.00")
    _bar(db, b, d, "200.00")
    out = pc.run(db, now=NOW, fetch=lambda day: {"AAA": 102.5, "BBB": 200.9})          # AAA: -2.4%; BBB: -0.45%
    assert out["compared"] == 2 and out["flagged"] == 1
    assert pc.flagged_instrument_ids(db) == {a.id}


def test_a_flag_resolves_when_the_closes_agree_again_and_a_reference_outage_changes_nothing(db):
    from datetime import date
    a = _inst(db, "AAA")
    d = date(2026, 11, 20)
    _bar(db, a, d, "100.00")
    pc.run(db, now=NOW, fetch=lambda day: {"AAA": 110.0})
    assert pc.flagged_instrument_ids(db) == {a.id}
    out = pc.run(db, now=NOW, fetch=lambda day: None)                                  # reference unavailable: neither flags nor clears
    assert out["reference_missing"] == 1 and pc.flagged_instrument_ids(db) == {a.id}
    out = pc.run(db, now=NOW, fetch=lambda day: {"AAA": 100.4})
    assert out["resolved"] == 1 and pc.flagged_instrument_ids(db) == set()


def test_bhavcopy_parser_keeps_equity_series_only():
    csv_text = "TradDt,TckrSymb,SctySrs,ClsPric\n2026-11-20,TCS,EQ,3500.5\n2026-11-20,SGBJUN28,GB,6000\n2026-11-20,XYZ,BE,10\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("b.csv", csv_text)
    from datetime import date
    got = pc.fetch_bhavcopy_closes(date(2026, 11, 20), http_get=lambda url: (200, buf.getvalue()))
    assert got == {"TCS": 3500.5, "XYZ": 10.0}
    assert pc.fetch_bhavcopy_closes(date(2026, 11, 20), http_get=lambda url: (404, b"")) is None


def test_the_nightly_run_never_resolves_an_audit_flag(db):
    from datetime import date
    from app.models.price_crosscheck import PriceCrosscheck
    a = _inst(db, "AAA")
    d = date(2026, 11, 20)
    _bar(db, a, d, "100.00")
    db.add(PriceCrosscheck(instrument_id=a.id, session_date=d, dhan_close=100.0, reference_close=100.0, deviation=0.0, reference_source="yahoo_close_audit", status="flagged", first_flagged_at=NOW, checked_at=NOW))
    db.commit()
    out = pc.run(db, now=NOW, fetch=lambda day: {"AAA": 100.0})
    assert out["resolved"] == 0 and pc.flagged_instrument_ids(db) == {a.id}
