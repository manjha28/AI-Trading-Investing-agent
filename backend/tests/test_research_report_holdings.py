"""Research report: Dhan holdings count as held, and "not enough data" is split into not current vs insufficient evidence. Test data: hand-made instruments, mirror rows and briefs in an in-memory database."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import BrokerMirror, Instrument, User
from app.services import research_report as rr

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _user(db):
    u = User(id=uuid4(), email=f"{uuid4()}@example.invalid", password_hash="x", email_verified=True, created_at=NOW)
    db.add(u)
    db.commit()
    return u.id


def _inst(db, sym):
    i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _mirror(db, uid, sym, qty):
    db.add(BrokerMirror(user_id=uid, broker="dhan", kind="holding", symbol=sym, exchange="ALL", product="", quantity=Decimal(qty), synced_at=NOW))
    db.commit()


def _brief(recs, *, technicals="available", annual=True, finished=NOW):
    horizons = {h: {"recommendation": r, "score": 70, "confidence": 70, "blockers": ["Fundamental: no annual statements stored"] if r == "INSUFFICIENT_EVIDENCE" else []} for h, r in recs.items()}
    sections = {"technicals": {"status": technicals, "message": None if technicals == "available" else "Stored daily bars are stale; do not use for current decisions.", "latest_close": "100"},
                "assessment": {"horizons": horizons, "components": {}, "thresholds": {"buy": 65}}, "levels": {"horizons": {h: {"status": "available", "ready_to_buy": True, "entry_status": "ENTER_NOW"} for h in recs}},
                "fundamentals": {"periods": [{"period_type": "annual"}] if annual else []}}
    run = SimpleNamespace(finished_at=finished, started_at=finished)
    return run, SimpleNamespace(sections=sections)


def test_a_dhan_only_holding_is_held_with_its_source(db):
    uid = _user(db)
    _mirror(db, uid, "JAMNAAUTO", "7")
    d = rr.held_details(db, uid)
    assert d["JAMNAAUTO"]["quantity"] == Decimal("7") and d["JAMNAAUTO"]["sources"] == {"dhan": "7.00000000"} and d["JAMNAAUTO"]["text"] == "Held at Dhan: 7"
    assert rr.held_quantities(db, uid) == {"JAMNAAUTO": Decimal("7")}


def test_a_ready_to_buy_card_for_a_dhan_holding_moves_to_hold_with_already_held_text(db, monkeypatch):
    uid = _user(db)
    held, fresh = _inst(db, "JAMNAAUTO"), _inst(db, "NEWCO")
    _mirror(db, uid, "JAMNAAUTO", "7")
    monkeypatch.setattr(rr, "user_universe", lambda d, u: [held, fresh])
    buy = {"swing": "BUY", "positional": "HOLD", "long_term": "HOLD"}
    monkeypatch.setattr(rr.research_pipeline, "latest_brief", lambda d, u, iid: _brief(buy))
    rep = rr.build_report(db, uid, NOW)
    by = {r["symbol"]: r for r in rep["buckets"]["hold"] + rep["buckets"]["buy"]}
    assert by["JAMNAAUTO"]["bucket"] == "hold" and "Already held (Held at Dhan: 7)" in by["JAMNAAUTO"]["action"] and by["JAMNAAUTO"]["levels"].get("swing")
    assert by["NEWCO"]["bucket"] == "buy"                                                      # an unheld stock with the same research stays in Ready to buy


def test_not_enough_data_is_split_into_not_current_and_insufficient_evidence(db, monkeypatch):
    uid = _user(db)
    stale, thin = _inst(db, "STALECO"), _inst(db, "THINCO")
    monkeypatch.setattr(rr, "user_universe", lambda d, u: [stale, thin])
    insuff = {"swing": "INSUFFICIENT_EVIDENCE", "positional": "INSUFFICIENT_EVIDENCE", "long_term": "INSUFFICIENT_EVIDENCE"}
    briefs = {stale.id: _brief({"swing": "BUY", "positional": "BUY", "long_term": "BUY"}, technicals="stale"), thin.id: _brief(insuff, annual=False)}
    monkeypatch.setattr(rr.research_pipeline, "latest_brief", lambda d, u, iid: briefs[iid])
    rep = rr.build_report(db, uid, NOW)
    assert [r["symbol"] for r in rep["buckets"]["stale_data"]] == ["STALECO"] and [r["symbol"] for r in rep["buckets"]["not_enough_data"]] == ["THINCO"]       # stale and thin are separate buckets
    rows = {r["symbol"]: r for r in rep["buckets"]["stale_data"] + rep["buckets"]["not_enough_data"]}
    assert rows["STALECO"]["not_enough_kind"] == "not_current" and rows["STALECO"]["action"].startswith("Not current: Stored daily bars are stale")
    assert rows["THINCO"]["not_enough_kind"] == "insufficient_evidence" and "no annual statements stored" in rows["THINCO"]["action"] and rows["THINCO"]["insufficient_reasons"]
    assert rep["coverage"]["not_current"] == 1 and rep["coverage"]["insufficient_evidence"] == 1


def test_a_short_history_horizon_says_why_instead_of_no_data():
    sections = {"technicals": {"bars_available": 10}, "technical_engine": {"horizons": {
        "positional": {"status": "unavailable", "reason": "Only 2 completed weekly candles are stored; at least 40 are needed."},
        "swing": {"status": "available"}}}}
    assert rr.horizon_data_note(sections, "positional") == "Listed recently: only 2 weekly candles stored; positional needs 40"
    assert rr.horizon_data_note(sections, "swing") is None
    sections["technicals"]["bars_available"] = 900                                                           # an old listing with a data gap is not "listed recently"
    assert rr.horizon_data_note(sections, "positional") == "only 2 weekly candles stored; positional needs 40"


def test_with_the_stock_verdict_flag_on_the_buckets_follow_the_verdicts(db, monkeypatch):
    from datetime import date

    from app.config import settings
    from app.models.stock_verdict import StockVerdict
    uid = _user(db)
    a, b, c = _inst(db, "NOWCO"), _inst(db, "TRIGCO"), _inst(db, "NOCO")
    monkeypatch.setattr(rr, "user_universe", lambda d, u: [a, b, c])
    buy = {"swing": "BUY", "positional": "BUY", "long_term": "HOLD"}
    monkeypatch.setattr(rr.research_pipeline, "latest_brief", lambda d, u, iid: _brief(buy))
    for inst, verdicts in ((a, ("buy_now", "watch", "not_now")), (b, ("buy_on_trigger", "not_now", "avoid")), (c, ("not_now", "avoid", "watch"))):
        for h, v in zip(("swing", "positional", "long_term"), verdicts):
            db.add(StockVerdict(instrument_id=inst.id, user_id=uid, as_of=date(2026, 11, 22), computed_at=NOW, horizon=h, verdict=v, evidence_score=50.0, lens={}, reasons={}, model_version="t"))
    db.commit()
    monkeypatch.setattr(settings, "stock_verdict_enabled", False)
    off = rr.build_report(db, uid, NOW)
    assert {r["symbol"] for r in off["buckets"]["buy"]} == {"NOWCO", "TRIGCO", "NOCO"}                  # v1: all three carry a BUY horizon
    monkeypatch.setattr(settings, "stock_verdict_enabled", True)
    on = rr.build_report(db, uid, NOW)
    where = {r["symbol"]: b_ for b_, rows in on["buckets"].items() for r in rows}
    assert where["NOWCO"] == "buy" and where["TRIGCO"] == "setup_waiting" and where["NOCO"] == "no_new_entry"


def test_a_stale_stored_close_marks_the_card_not_current_and_builds_no_levels(db, monkeypatch):
    from datetime import datetime as dt, timezone as tz
    from decimal import Decimal

    from app.models import MarketBar
    uid = _user(db)
    old, current = _inst(db, "OLDCLOSE"), _inst(db, "FRESHCLOSE")
    monkeypatch.setattr(rr, "user_universe", lambda d, u: [old, current])
    for inst, day in ((old, 20), (current, 23)):
        db.add(MarketBar(instrument_id=inst.id, interval="1d", timestamp=dt(2026, 11, day, tzinfo=tz.utc), open=Decimal(1), high=Decimal(1), low=Decimal(1), close=Decimal(1), volume=Decimal(1), source="dhan", retrieved_at=NOW, is_synthetic=False))
    db.commit()
    buy = {"swing": "BUY", "positional": "BUY", "long_term": "BUY"}
    briefs = {old.id: _brief(buy), current.id: _brief(buy)}
    monkeypatch.setattr(rr.research_pipeline, "latest_brief", lambda d, u, iid: briefs[iid])
    after_close = dt(2026, 11, 23, 12, 0, tzinfo=tz.utc)                       # Mon 23 Nov 17:30 IST: the 23 Nov candle should be stored
    rep = rr.build_report(db, uid, after_close)
    stale = {r["symbol"]: r for r in rep["buckets"]["stale_data"]}
    assert "OLDCLOSE" in stale and stale["OLDCLOSE"]["levels"] == {} and "2026-11-20" in stale["OLDCLOSE"]["action"] and "2026-11-23" in stale["OLDCLOSE"]["action"] and stale["OLDCLOSE"]["stale_close"] is True
    assert "FRESHCLOSE" not in stale and rr.last_completed_session(dt(2026, 11, 23, 5, 0, tzinfo=tz.utc)).isoformat() == "2026-11-20"       # before 15:45 IST the last completed session is Friday


def test_last_completed_session_skips_weekends_and_waits_for_the_candle():
    from datetime import datetime as dt, timezone as tz
    assert rr.last_completed_session(dt(2026, 11, 23, 11, 0, tzinfo=tz.utc)).isoformat() == "2026-11-23"
    assert rr.last_completed_session(dt(2026, 11, 22, 11, 0, tzinfo=tz.utc)).isoformat() == "2026-11-20"
    assert rr.last_completed_session(dt(2026, 11, 23, 10, 0, tzinfo=tz.utc)).isoformat() == "2026-11-20"           # 15:30 IST: the candle is not published yet
