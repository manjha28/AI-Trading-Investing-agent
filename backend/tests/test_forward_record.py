"""Weekly forward record: shadow verdicts played forward with simulate_card, episodes vs all nights, long-term vs the NIFTY 500. Test data: hand-made bars and verdicts in an in-memory database."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, MarketBar, User
from app.models.multibagger import MultibaggerAssessment
from app.models.stock_verdict import StockVerdict
from app.services import forward_record as fr

NOW = datetime(2026, 4, 1, 12, tzinfo=timezone.utc)
D0 = date(2026, 1, 1)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _inst(db, symbol, asset="equity"):
    i = Instrument(id=uuid4(), symbol=symbol, display_name=symbol, asset_class=asset, venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _bar(db, inst, day, o, h, lo, c, source="dhan"):
    ts = datetime(day.year, day.month, day.day, 3, 45, tzinfo=timezone.utc)
    db.add(MarketBar(id=uuid4(), instrument_id=inst.id, interval="1d", timestamp=ts, open=Decimal(str(o)), high=Decimal(str(h)), low=Decimal(str(lo)), close=Decimal(str(c)), volume=Decimal("1000"), source=source, retrieved_at=NOW, is_synthetic=False))


def _user(db):
    u = User(id=uuid4(), email=f"{uuid4()}@example.invalid", password_hash="x", email_verified=True, created_at=NOW)
    db.add(u)
    db.commit()
    return u.id


def _verdict(db, uid, inst, as_of, horizon="swing", verdict="buy_now", pred=0.4):
    lv = {"entry_status": "ENTER_NOW", "planned_entry": "100.00", "stop": "95.00", "targets": [{"price": "110.00", "basis": "Resistance zone"}]}
    db.add(StockVerdict(instrument_id=inst.id, user_id=uid, as_of=as_of, computed_at=NOW, horizon=horizon, verdict=verdict, lens={}, reasons={"levels": lv, "confidence": {"win_probability": pred}}, model_version="t"))
    db.commit()


def test_a_buy_verdict_is_played_forward_and_old_enough_ones_only(db):
    uid, i = _user(db), _inst(db, "AAA")
    for k in range(1, 40):                                                 # sessions after the verdict date: opens at 101, T1 (110) is reached on session 3
        d = D0 + timedelta(days=k)
        _bar(db, i, d, 101, 111 if k == 3 else 104, 100, 103)
    db.commit()
    _verdict(db, uid, i, D0)                                              # old enough (39 bars after, swing needs 15)
    _verdict(db, uid, i, D0 + timedelta(days=30))                         # only 9 bars after: too recent
    out = fr.short_horizon(db, uid, NOW)
    sw = out["by_horizon"]["swing"]["episodes"]["all"]
    assert sw["trades"] == 1 and sw["t1_hits"] == 1 and sw["net_r"] > 0 and out["not_a_trade"]["too_recent"] == 1


def test_consecutive_buy_nights_are_one_episode_but_two_all_night_observations(db):
    uid, i = _user(db), _inst(db, "BBB")
    for k in range(1, 40):
        _bar(db, i, D0 + timedelta(days=k), 101, 111 if k == 4 else 104, 100, 103)
    db.commit()
    _verdict(db, uid, i, D0)
    _verdict(db, uid, i, D0 + timedelta(days=1))
    h = fr.short_horizon(db, uid, NOW)["by_horizon"]["swing"]
    assert h["episodes"]["all"]["trades"] == 1 and h["all_nights"]["all"]["trades"] == 2


def test_a_verdict_with_no_levels_is_not_a_trade_and_is_counted(db):
    uid, i = _user(db), _inst(db, "CCC")
    db.add(StockVerdict(instrument_id=i.id, user_id=uid, as_of=D0, computed_at=NOW, horizon="swing", verdict="buy_now", lens={}, reasons={}, model_version="t"))
    db.commit()
    for k in range(1, 30):
        _bar(db, i, D0 + timedelta(days=k), 101, 104, 100, 103)
    db.commit()
    out = fr.short_horizon(db, uid, NOW)
    assert out["not_a_trade"].get("no_levels") == 1 and out["by_horizon"]["swing"]["episodes"]["all"]["trades"] == 0


def test_long_term_with_no_matured_class_says_when_the_first_reading_arrives(db):
    uid, i, ix = _user(db), _inst(db, "DDD"), _inst(db, "^CRSLDX", "index")
    for k in range(0, 40):
        _bar(db, i, D0 + timedelta(days=k), 100 + k, 100 + k, 100 + k, 100 + k)
        _bar(db, ix, D0 + timedelta(days=k), 1000, 1000, 1000, 1000)
    db.add(MultibaggerAssessment(instrument_id=i.id, user_id=uid, as_of=D0, computed_at=NOW, klass="quality_compounder", checks={}, model_version="t"))
    db.commit()
    out = fr.long_term(db, uid, NOW)
    assert out["available"] and "No class observation is 3 months old yet" in out["by_months"]["3"]["note"]


def test_run_if_due_stores_one_report_a_week_and_latest_reads_it(db):
    uid = _user(db)
    assert fr.run_if_due(db, uid, NOW) is True and fr.run_if_due(db, uid, NOW + timedelta(days=2)) is False
    rep = fr.latest(db, uid)
    assert rep["as_of"] == "2026-04-01" and "short_horizon" in rep and "Decision support, not investment advice" in rep["disclaimer"]
