"""The card price line: last close and day change after hours, the live quote during the session, stale flag, and nothing invented when there is no price. TEST rows."""
from datetime import datetime, timezone
from decimal import Decimal as D

from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar, MarketQuote
from app.services import report_prices
from tests.test_crypto_delta import engine  # noqa: F401

AFTER = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)      # Tue 17:30 IST, closed
DURING = datetime(2026, 11, 24, 7, 35, tzinfo=timezone.utc)     # Tue 13:05 IST, open


def inst(sym):
    return Instrument(symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=AFTER, is_synthetic=False)


def bar(i, day, close):
    return MarketBar(instrument_id=i.id, interval="1d", timestamp=datetime(2026, 11, day, tzinfo=timezone.utc), open=D(1), high=D(close), low=D(1), close=D(close), volume=D(1), source="dhan", retrieved_at=AFTER, is_synthetic=False)


def test_after_hours_shows_the_last_close_with_the_change_and_during_the_session_the_live_price(engine):  # noqa: F811
    with Session(engine) as db:
        a, b = inst("AAA"), inst("BBB")
        db.add_all([a, b]); db.flush()
        db.add_all([bar(a, 20, 100), bar(a, 23, 103), bar(b, 23, 50)])
        db.add(MarketQuote(instrument_id=a.id, timestamp=DURING, open=D(100), high=D(106), low=D(99), close=D("105.5"), volume=D(10), source="dhan", retrieved_at=DURING, is_synthetic=False))
        db.commit()
        rows = [{"instrument_id": str(a.id)}, {"instrument_id": str(b.id), "stale_close": True}, {"instrument_id": "00000000-0000-0000-0000-000000000000"}]
        closed = report_prices.build(db, rows, AFTER)
        assert closed[str(a.id)] == {"value": "103.00000000", "kind": "close", "as_of": "2026-11-23T00:00:00+00:00", "change": "3.00000000", "change_pct": "3.00", "stale": False}
        assert closed[str(b.id)]["change"] is None and closed[str(b.id)]["stale"] is True           # one bar only: no change is invented
        assert "00000000-0000-0000-0000-000000000000" not in closed                                    # no price at all: no block
        live = report_prices.build(db, rows, DURING)[str(a.id)]
        assert live["kind"] == "ltp" and live["value"] == "105.50000000" and live["change"] == "2.50000000" and live["as_of"].startswith("2026-11-24T07:35")      # against the previous close 103
        old_quote = report_prices.build(db, rows, datetime(2026, 11, 24, 7, 50, tzinfo=timezone.utc))[str(a.id)]
        assert old_quote["kind"] == "close"                                                              # a quote older than 10 minutes is not "live"


def test_attach_adds_a_price_only_to_card_buckets(engine):  # noqa: F811
    with Session(engine) as db:
        a = inst("AAA")
        db.add(a); db.flush(); db.add_all([bar(a, 23, 103), bar(a, 20, 100)]); db.commit()
        out = report_prices.attach(db, {"buckets": {"buy": [{"instrument_id": str(a.id)}], "hold": [{"instrument_id": str(a.id)}]}}, AFTER)
        assert out["buckets"]["buy"][0]["price"]["value"].startswith("103") and "price" not in out["buckets"]["hold"][0]
