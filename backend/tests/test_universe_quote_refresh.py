"""The minute refresh stores only quotes traded today, one row per instrument. All rows here are TEST DATA."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, InstrumentProviderIdentifier, MarketQuote
from app.universe_quote_refresh import market_hours, refresh_universe_quotes

NOW = datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc)          # Thursday 11:30 IST


def bar(at, close):
    d = Decimal(close)
    return SimpleNamespace(timestamp=at, open=d, high=d, low=d, close=d, volume=Decimal("10"))


def test_stores_todays_trades_only_and_updates_in_place():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        items = []
        for n, symbol in enumerate(("AAA", "BBB"), start=1):
            i = Instrument(id=uuid4(), symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata",
                           is_synthetic=False, relink_required=False, created_at=NOW)
            db.add(i)
            db.flush()
            db.add(InstrumentProviderIdentifier(instrument_id=i.id, provider="dhan", provider_instrument_id=f"{n}|NSE_EQ", provider_symbol=symbol, is_verified=True, last_seen_at=NOW))
            items.append(i)
        db.commit()
        ids = {i.id for i in items}
        first = lambda req: {"NSE_EQ": {"1": bar(NOW - timedelta(minutes=5), "100"), "2": bar(NOW - timedelta(days=1), "50")}}   # BBB has not traded today
        assert refresh_universe_quotes(db, ids, first, NOW) == 1
        assert refresh_universe_quotes(db, ids, lambda req: {"NSE_EQ": {"1": bar(NOW, "101")}}, NOW) == 1
        rows = list(db.scalars(select(MarketQuote)))
        assert len(rows) == 1 and rows[0].instrument_id == items[0].id and rows[0].close == Decimal("101")


def test_runs_only_in_nse_hours():
    assert market_hours(NOW)
    assert not market_hours(datetime(2026, 11, 25, 23, 30, tzinfo=timezone.utc))      # 05:00 IST
    assert not market_hours(datetime(2026, 11, 28, 6, 0, tzinfo=timezone.utc))        # Saturday
