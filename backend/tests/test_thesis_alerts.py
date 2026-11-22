"""Stage 8 thesis-break alerts for the owner's Dhan long-term holdings (test data: hand-made rows in an in-memory database)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import BrokerMirror, Instrument, InstrumentFundamental, User
from app.models.alerts import AlertEvent
from app.models.multibagger import MultibaggerAssessment
from app.services import research_alerts as ra

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _setup(db, *, held=True, slow=True):
    u = User(id=uuid4(), email=f"{uuid4()}@example.invalid", password_hash="x", email_verified=True, created_at=NOW)
    held_i = Instrument(id=uuid4(), symbol="HELDCO", display_name="Held", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    other = Instrument(id=uuid4(), symbol="OTHERCO", display_name="Other", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add_all([u, held_i, other])
    db.commit()
    if held:
        db.add(BrokerMirror(user_id=u.id, broker="dhan", kind="holding", symbol="HELDCO", exchange="ALL", product="", quantity=Decimal("5"), synced_at=NOW))
    for inst in (held_i, other):
        for k in range(8):                                    # 8 consecutive quarters; revenue flat (slow) or +30% a year
            end = (date(2024, 12, 31) + timedelta(days=91 * k))
            rev = Decimal("100") * (Decimal("1.0") if slow else Decimal("1.3") ** (k // 4))
            db.add(InstrumentFundamental(instrument_id=inst.id, period_type="quarterly", period_end=end, currency="INR", revenue=rev, net_income=Decimal("10"), source="nse_results", fetched_at=NOW))
        db.add(MultibaggerAssessment(instrument_id=inst.id, user_id=u.id, as_of=date(2026, 11, 20), computed_at=NOW, klass="quality_compounder", checks={}, model_version="t"))
        db.add(MultibaggerAssessment(instrument_id=inst.id, user_id=u.id, as_of=date(2026, 11, 22), computed_at=NOW, klass="avoid", checks={}, model_version="t"))
    db.commit()
    return u.id


def test_nothing_happens_with_the_flag_off(db, monkeypatch):
    uid = _setup(db)
    monkeypatch.setattr(settings, "multibagger_screen_enabled", False)
    assert ra.notify_thesis_breaks(db, uid, NOW) == [] and db.scalar(select(AlertEvent.id)) is None


def test_a_held_stock_with_a_broken_thesis_alerts_once_a_day_and_an_unheld_one_never(db, monkeypatch):
    uid = _setup(db)
    monkeypatch.setattr(settings, "multibagger_screen_enabled", True)
    fired = ra.notify_thesis_breaks(db, uid, NOW)
    assert {(a["symbol"], a["kind"]) for a in fired} == {("HELDCO", "revenue_slowdown"), ("HELDCO", "class_avoid")}       # flat revenue for 2 quarters; class dropped to avoid; OTHERCO is not held
    events = list(db.scalars(select(AlertEvent)))
    assert len(events) == 2 and all(e.evidence["symbol"] == "HELDCO" and "never an order" in e.evidence["note"] for e in events)
    assert ra.notify_thesis_breaks(db, uid, NOW) == [] and len(list(db.scalars(select(AlertEvent)))) == 2                # the same day again: no duplicates
    later = ra.notify_thesis_breaks(db, uid, NOW + timedelta(days=1))
    assert len(later) == 2                                                                                             # a new day alerts again while the thesis stays broken


def test_a_healthy_holding_and_a_price_fall_alone_do_not_alert(db, monkeypatch):
    uid = _setup(db, slow=False)
    from sqlalchemy import update
    db.execute(update(MultibaggerAssessment).values(klass="quality_compounder"))
    db.commit()
    monkeypatch.setattr(settings, "multibagger_screen_enabled", True)
    assert ra.notify_thesis_breaks(db, uid, NOW) == []                                                                 # growth fine, class unchanged: no alert (no price input exists at all)
