"""NIFTY 50 index loader: Dhan first, Yahoo only for dates Dhan does not have, source reported. Test data only: hand-made bars in an in-memory database."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, MarketBar
from app.services import benchmark_index as bi

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 22, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _index(db):
    i = Instrument(id=uuid4(), symbol="^NSEI", display_name="NIFTY 50 index", asset_class="index", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _bar(db, inst, day, close, source):
    ts = datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(timezone.utc)
    db.add(MarketBar(id=uuid4(), instrument_id=inst.id, interval="1d", timestamp=ts, open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
                     volume=Decimal("0"), source=source, retrieved_at=NOW, is_synthetic=False))


def test_no_bars_is_unavailable_not_a_default(db):
    s, info = bi.index_series(db)
    assert s is None and info["status"] == "unavailable"


def test_dhan_wins_on_every_date_it_has_and_yahoo_only_fills_earlier_gaps(db):
    i = _index(db)
    d0 = datetime(2026, 1, 1).date()
    for k in range(10):                                     # Yahoo has 10 days
        _bar(db, i, d0 + timedelta(days=k), 100 + k, "yfinance")
    for k in range(4, 10):                                  # Dhan has the last 6 days, with a different close on the overlap
        _bar(db, i, d0 + timedelta(days=k), 200 + k, "dhan")
    db.commit()
    s, info = bi.index_series(db)
    assert len(s) == 10 and info["primary"] == "dhan" and info["dhan_days"] == 6 and info["yahoo_fallback_days"] == 4 and info["first_dhan"] == "2026-01-05"
    assert list(s.iloc[:4]) == [100, 101, 102, 103]         # Yahoo only before Dhan's first bar
    assert list(s.iloc[4:]) == [204, 205, 206, 207, 208, 209]   # Dhan's values on the overlap, never Yahoo's
    assert "Yahoo used only before 2026-01-05" in info["join"]


def test_dhan_only_has_no_fallback_days(db):
    i = _index(db)
    for k in range(5):
        _bar(db, i, datetime(2026, 1, 1).date() + timedelta(days=k), 300 + k, "dhan")
    db.commit()
    s, info = bi.index_series(db)
    assert len(s) == 5 and info["yahoo_fallback_days"] == 0 and info["join"] is None
