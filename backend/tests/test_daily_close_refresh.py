"""The post-session daily close refresh: which instruments are stale, that it never runs during the session, and that failures are counted. Rows are TEST DATA."""
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app import daily_close_refresh as dcr
from app.models import Instrument, MarketBar
from tests.test_crypto_delta import engine  # noqa: F401

D = Decimal
AFTER_CLOSE = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)          # Mon 23 Nov 16:30 IST, session over
DURING = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)                # Mon 23 Nov 11:30 IST, session open


def inst(symbol):
    return Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=AFTER_CLOSE, is_synthetic=False)


def bar(i, day):
    return MarketBar(instrument_id=i.id, interval="1d", timestamp=datetime(2026, 11, day, tzinfo=timezone.utc), open=D(1), high=D(1), low=D(1), close=D(1), volume=D(1), source="dhan", retrieved_at=AFTER_CLOSE, is_synthetic=False)


def test_needs_refresh_only_when_the_newest_close_is_older_than_the_expected_session():
    assert dcr.needs_refresh(None, date(2026, 11, 23))
    assert dcr.needs_refresh(date(2026, 11, 20), date(2026, 11, 23))
    assert not dcr.needs_refresh(date(2026, 11, 23), date(2026, 11, 23))
    # a bar of the expected session that came from the 15:33 IST quote snapshot is provisional: it is fetched again once the candle is published
    assert dcr.needs_refresh(date(2026, 11, 23), date(2026, 11, 23), datetime(2026, 11, 23, 10, 3, tzinfo=timezone.utc))
    assert not dcr.needs_refresh(date(2026, 11, 23), date(2026, 11, 23), datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc))


def test_stale_instruments_are_found_stalest_first_and_current_ones_are_skipped(engine):  # noqa: F811
    with Session(engine) as db:
        a, b, c = inst("AAA"), inst("BBB"), inst("CCC")
        db.add_all([a, b, c]); db.flush()
        db.add_all([bar(a, 20), bar(b, 23), bar(a, 19)])                      # AAA's newest is 20 Nov (stale), BBB is current, CCC has none
        db.commit()
        out = dcr.stale_instruments(db, [a, b, c], AFTER_CLOSE)
        assert [(i.symbol, d) for i, d in out] == [("CCC", None), ("AAA", date(2026, 11, 20))]


def test_nothing_is_refreshed_while_the_session_is_open_because_the_daily_candle_is_still_forming():
    assert dcr.run_pass(DURING) == {"users": 0, "stale": 0, "refreshed": 0, "errors": 0}
