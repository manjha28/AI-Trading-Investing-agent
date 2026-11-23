"""Delivery % ingest from the NSE full bhavcopy. Test data: a hand-made CSV in the real column layout."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models.delivery import DeliveryPercentage
from app.services import delivery_ingest as di

CSV = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
       "AAA, EQ, 20-Nov-2026, 10, 10, 11, 9, 10, 10, 10, 100, 1, 5, 51, 51.00\nBBB, BE, 20-Nov-2026, 10, 10, 11, 9, 10, 10, 10, 100, 1, 5, -, -\nCCC, XX, 20-Nov-2026, 10, 10, 11, 9, 10, 10, 10, 100, 1, 5, 5, 5.00\nDDD, EQ, 20-Nov-2026, 10, 10, 11, 9, 10, 10, 10, 100, 1, 5, 0, 0.00\n")


def test_parse_keeps_real_equity_percentages_and_skips_dashes_and_other_series():
    assert di.parse(CSV) == [("AAA", "EQ", Decimal("51.00")), ("DDD", "EQ", Decimal("0.00"))]          # a reported 0.00 is a real value; '-' is unknown and skipped


def test_store_only_tracked_symbols_and_is_idempotent():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    with Session(e) as db:
        rows = di.parse(CSV)
        assert di.store(db, date(2026, 11, 20), rows, {"AAA"}) == 1 and di.store(db, date(2026, 11, 20), rows, {"AAA"}) == 0
        assert [(r.symbol, str(r.deliv_per)) for r in db.scalars(select(DeliveryPercentage))] == [("AAA", "51.00")]


def test_an_outage_stores_nothing_and_never_raises():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    with Session(e) as db:
        assert di.run(db, now=datetime(2026, 11, 23, 12, tzinfo=timezone.utc), fetch=lambda d: None)["days"] == 0
