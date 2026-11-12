"""Recovery of the latest completed NSE daily candle through the existing history sync (never a parallel fetcher)."""
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, MarketBar, User
from app.services import research_pipeline as rp
from app.services import research_scoring as rs
from test_research_pipeline import client, csrf, get_instrument_id, register, test_engine  # noqa: F401

NOW = datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc)  # Thursday 19:30 IST: the Nov 5 session has closed
SESSION = date(2026, 11, 5)
AFTER_CLOSE = datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)  # 16:30 IST, after the 15:30 close
BEFORE_CLOSE = datetime(2026, 11, 5, 8, 0, tzinfo=timezone.utc)  # 13:30 IST, session still open


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    rp._SESSION_FETCH_FAILURES.clear()
    rp._SESSION_FETCH_LOCKS.clear()
    real = rs.daily_bar_staleness
    # freeze "now" inside the technical section so the test is not tied to the wall clock
    monkeypatch.setattr(rp, "daily_bar_staleness", lambda ts, _now, **kw: real(ts, NOW, **kw))
    monkeypatch.setattr(rp, "_exclusive_selected_source", lambda *_a: None)
    monkeypatch.setattr(rp, "market_data_provider_for_user", lambda *_a: object())


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def make_instrument(db, **over):
    data = dict(symbol="TESTCO", display_name="Test Co", asset_class="equity", venue="NSE", currency="INR", country="IN",
                timezone="Asia/Kolkata", created_at=NOW)
    data.update(over)
    instrument = Instrument(**data)
    db.add(instrument)
    db.flush()
    return instrument


def bar(instrument, day, *, close="100", retrieved_at=AFTER_CLOSE, source="dhan", **over):
    close = Decimal(close)
    values = dict(instrument_id=instrument.id, interval="1d", timestamp=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
                  open=close, high=close + 1, low=close - 1, close=close, volume=Decimal("1000"), source=source,
                  retrieved_at=retrieved_at, is_synthetic=False)
    values.update(over)
    return MarketBar(**values)


def seed_history(db, instrument, count=250, last=date(2026, 11, 3)):
    day, added = last, 0
    while added < count:
        if day.weekday() < 5:
            db.add(bar(instrument, day, close=str(100 + added % 7), retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc)))
            added += 1
        day -= timedelta(days=1)
    db.flush()


def used(provider="dhan"):
    return SimpleNamespace(provider_attempts=[SimpleNamespace(provider=provider, outcome="used", reason=None, reason_category=None)])


def stub_sync(monkeypatch, behaviour):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return behaviour(kwargs)

    monkeypatch.setattr(rp, "sync_history", fake)
    return calls


def test_missing_candle_is_fetched_persisted_and_immediately_drives_technical_scoring(db, monkeypatch):
    instrument = make_instrument(db)
    seed_history(db, instrument)
    before = rp._technicals_section(db, instrument.id, [])
    assert before["status"] == "stale" and rs.score_technical(before)["status"] == "blocked"  # the old behaviour: no score

    def persist_candle(kwargs):
        kwargs["db"].add(bar(instrument, SESSION, close="150"))  # what the real sync persists (validated bar)
        kwargs["db"].commit()
        return used("dhan")

    calls = stub_sync(monkeypatch, persist_candle)
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["action"] == "fetch" and result["persisted"] is True and result["provider"] == "dhan" and result["reason"] is None
    assert result["state_before"] == "missing" and result["session"] == "2026-11-05"
    assert len(calls) == 1 and calls[0]["interval"] == "1d" and calls[0]["background_tasks"] is None  # existing sync path, daily interval
    assert calls[0]["start"] >= datetime(2026, 10, 29, tzinfo=timezone.utc) and calls[0]["end"] == NOW  # narrow recent window only

    after = rp._technicals_section(db, instrument.id, [])  # reloaded from the store, same code path as the report
    assert after["status"] == "available" and after["latest_bar"].startswith("2026-11-05") and Decimal(after["latest_close"]) == Decimal("150")
    scored = rs.score_technical(after)
    assert scored["status"] == "scored" and scored["score"] is not None  # technical intelligence now computed from the new candle


def test_existing_valid_candle_triggers_no_request_at_all(db, monkeypatch):
    instrument = make_instrument(db)
    seed_history(db, instrument)
    db.add(bar(instrument, SESSION))
    db.flush()
    calls = stub_sync(monkeypatch, lambda _k: pytest.fail("must not request history when the candle is stored"))
    monkeypatch.setattr(rp, "market_data_provider_for_user", lambda *_a: pytest.fail("must not even build a provider"))
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["action"] == "none" and result["state_before"] == "present" and calls == []


def test_partial_candle_stored_before_the_close_is_not_treated_as_completed(db, monkeypatch):
    instrument = make_instrument(db)
    db.add(bar(instrument, SESSION, retrieved_at=BEFORE_CLOSE))  # retrieved while the session was still open
    db.flush()
    assert rp.latest_session_candle_state(db, instrument, SESSION) == "incomplete"

    def replace_with_final(kwargs):
        row = kwargs["db"].scalar(select(MarketBar).where(MarketBar.instrument_id == instrument.id))
        row.close, row.high, row.retrieved_at = Decimal("101"), Decimal("102"), AFTER_CLOSE
        kwargs["db"].commit()
        return used("angel_one")

    stub_sync(monkeypatch, replace_with_final)
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["state_before"] == "incomplete" and result["persisted"] is True and result["provider"] == "angel_one"


def test_provider_failure_is_reported_with_the_real_reasons_and_is_not_retried_immediately(db, monkeypatch):
    instrument = make_instrument(db)
    seed_history(db, instrument)

    def fail(_kwargs):
        raise HTTPException(status_code=503, detail={"stage": "provider_history_fetch", "message": "Historical market data is temporarily unavailable.",
                                                     "providers": [{"provider": "angel_one", "reason": "authentication_expired"},
                                                                   {"provider": "dhan", "reason": "dh_907_no_data"}]})

    calls = stub_sync(monkeypatch, fail)
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["action"] == "failed" and result["persisted"] is False
    assert "angel_one: authentication_expired" in result["reason"] and "dhan: dh_907_no_data" in result["reason"]
    assert "2026-11-05" in result["reason"] and "temporarily unavailable" in result["reason"]
    again = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert len(calls) == 1 and "retry suppressed" in again["reason"]  # no provider hammering


def test_provider_returning_nothing_for_the_session_is_a_failure_not_a_fabricated_candle(db, monkeypatch):
    instrument = make_instrument(db)
    seed_history(db, instrument)
    stub_sync(monkeypatch, lambda _k: SimpleNamespace(provider_attempts=[
        SimpleNamespace(provider="dhan", outcome="unavailable", reason="no_data_for_session", reason_category=None)]))
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["action"] == "failed" and "dhan: no_data_for_session" in result["reason"] and "no candle returned" in result["reason"]
    assert db.scalar(select(func.count()).select_from(MarketBar).where(MarketBar.timestamp == datetime(2026, 11, 5))) == 0
    assert rp.latest_session_candle_state(db, instrument, SESSION) == "missing"


def test_fallback_provider_supplies_the_candle_when_the_first_cannot(db, monkeypatch):
    instrument = make_instrument(db)

    def second_provider_works(kwargs):
        kwargs["db"].add(bar(instrument, SESSION, source="yfinance"))
        kwargs["db"].commit()
        return SimpleNamespace(provider_attempts=[
            SimpleNamespace(provider="angel_one", outcome="unavailable", reason="authentication_expired", reason_category=None),
            SimpleNamespace(provider="dhan", outcome="unavailable", reason="no_data", reason_category=None),
            SimpleNamespace(provider="yfinance", outcome="used", reason=None, reason_category=None)])

    stub_sync(monkeypatch, second_provider_works)
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)
    assert result["persisted"] is True and result["provider"] == "yfinance"
    assert [a["provider"] for a in result["attempts"]] == ["angel_one", "dhan", "yfinance"]  # existing priority order, reasons kept


@pytest.mark.parametrize("over", [dict(is_synthetic=True), dict(country="US", currency="USD", venue="NASDAQ"), dict(asset_class="crypto", venue="DELTA", country="IN")])
def test_synthetic_foreign_and_continuous_instruments_are_not_fetched(db, monkeypatch, over):
    instrument = make_instrument(db, **over)
    calls = stub_sync(monkeypatch, lambda _k: pytest.fail("not applicable"))
    assert rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=NOW)["reason"] == "not_applicable" and calls == []


def test_calendar_not_covered_means_no_guessing(db, monkeypatch):
    instrument = make_instrument(db)
    calls = stub_sync(monkeypatch, lambda _k: pytest.fail("session unknown"))
    result = rp.ensure_latest_daily_candle(db, instrument, uuid4(), now=datetime(2027, 6, 1, 14, 0, tzinfo=timezone.utc))
    assert result["action"] == "none" and result["reason"].startswith("nse_calendar_unavailable") and calls == []


def test_simultaneous_requests_for_one_instrument_and_session_fetch_once(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'c.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as setup:
        instrument = make_instrument(setup)
        setup.commit()
        instrument_id = instrument.id
    started = threading.Event()
    calls = []

    def slow_persist(kwargs):
        calls.append(1)
        started.set()
        time.sleep(0.3)  # the second request arrives while this one is in flight
        kwargs["db"].add(bar(kwargs["db"].get(Instrument, instrument_id), SESSION))
        kwargs["db"].commit()
        return used("dhan")

    monkeypatch.setattr(rp, "sync_history", lambda **kw: slow_persist(kw))
    results = []

    def worker():
        with Session(engine) as session:
            results.append(rp.ensure_latest_daily_candle(session, session.get(Instrument, instrument_id), uuid4(), now=NOW))

    first = threading.Thread(target=worker)
    first.start()
    started.wait(2)
    second = threading.Thread(target=worker)
    second.start()
    first.join(5)
    second.join(5)
    assert len(calls) == 1  # exactly one provider request
    assert sorted(r["action"] for r in results) == ["fetch", "none"]
    assert any(r["reason"] == "stored_by_concurrent_request" and r["persisted"] for r in results)
    engine.dispose()


def test_failed_recovery_appears_in_the_report_data_quality_and_scoring_still_runs(client, test_engine, monkeypatch):  # noqa: F811
    register(client, test_engine, "recovery@example.com")
    instrument_id = get_instrument_id(client)
    failure = {"action": "failed", "session": "2026-11-05", "state_before": "missing", "persisted": False, "provider": None,
              "attempts": [{"provider": "dhan", "outcome": "unavailable", "reason": "dh_907_no_data"}],
              "reason": "Latest completed NSE session 2026-11-05 candle could not be stored (no candle returned): dhan: dh_907_no_data"}
    monkeypatch.setattr(rp, "ensure_latest_daily_candle", lambda *_a, **_k: failure)
    response = client.post(f"/api/v1/research/instruments/{instrument_id}/run", headers=csrf(client))
    run = client.get(f"/api/v1/research/runs/{response.json()['run_id']}").json()
    brief = run["brief"]
    assert brief["sections"]["technicals"]["latest_session_recovery"]["attempts"][0]["reason"] == "dh_907_no_data"
    items = [i for i in brief["sections"]["data_quality"]["items"] if i["kind"] == "failed_fetch" and i["field_path"].endswith("latest_session")]
    assert items and "dh_907_no_data" in items[0]["reason"]
    assert "assessment" in brief["sections"]  # scoring was not blocked by the failed recovery
    assert any(step["component"] == "market_data_recovery" and step["state"] == "failed" for step in run["progress"])
