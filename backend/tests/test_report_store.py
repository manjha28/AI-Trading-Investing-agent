"""Stored daily report, request timeout, liveness. Test data: a stub report builder and an in-memory database."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import User
from app.request_timeout import RequestTimeoutMiddleware
from app.services import report_store as rs

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


@pytest.fixture
def user(db):
    u = User(id=uuid4(), email=f"{uuid4()}@example.invalid", password_hash="x", email_verified=True, created_at=NOW)
    db.add(u)
    db.commit()
    return u.id


@pytest.fixture
def builder(monkeypatch):
    calls = []

    def build(db, user_id, now=None):
        calls.append(1)
        return {"as_of": "now", "buckets": {"buy": [{"symbol": f"S{i}"} for i in range(5)], "hold": []}, "counts": {"buy": 5, "hold": 0}, "coverage": {}}
    from app.services import research_report
    monkeypatch.setattr(research_report, "build_report", build)
    monkeypatch.setattr(rs, "refresh_in_background", lambda uid: calls.append("bg") or True)
    return calls


def test_the_first_request_builds_and_stores_then_later_requests_serve_the_stored_copy(db, user, builder):
    a = rs.serve(db, user, NOW)
    b = rs.serve(db, user, NOW + timedelta(minutes=1))
    assert builder == [1] and a["etag"] == b["etag"] and b["payload"]["buckets"]["buy"][0]["symbol"] == "S0" and b["rebuilding"] is False          # built once, never inside the second request


def test_a_stale_stored_copy_is_served_immediately_and_rebuilt_in_the_background(db, user, builder):
    rs.serve(db, user, NOW)
    old = rs.serve(db, user, NOW + timedelta(minutes=11))
    assert builder == [1, "bg"] and old["payload"]["counts"]["buy"] == 5                                         # served at once, background rebuild requested


def test_a_newer_research_run_marks_the_stored_report_stale(db, user, builder):
    from app.models import Instrument, InstrumentResearchRun
    rs.serve(db, user, NOW)
    inst = Instrument(id=uuid4(), symbol="X", display_name="X", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(inst)
    db.commit()
    db.add(InstrumentResearchRun(id=uuid4(), user_id=user, instrument_id=inst.id, status="complete", started_at=NOW + timedelta(minutes=2), finished_at=NOW + timedelta(minutes=3)))
    db.commit()
    rs.serve(db, user, NOW + timedelta(minutes=4))
    assert builder[-1] == "bg"


def test_the_endpoint_returns_etag_304_and_pages_a_bucket(db, user, builder, monkeypatch):
    from app import research_daily
    from app.api import research as api
    monkeypatch.setattr(research_daily, "kick_missing", lambda uid: [])
    sess = SimpleNamespace(user_id=user)
    req = lambda inm=None: SimpleNamespace(headers={"if-none-match": inm} if inm else {})  # noqa: E731
    first = api.get_daily_report(req(), None, None, 0, sess, db)
    etag = first.headers["etag"]
    assert first.status_code == 200 and "must-revalidate" in first.headers["cache-control"]
    again = api.get_daily_report(req(etag), None, None, 0, sess, db)
    assert again.status_code == 304
    page = api.get_daily_report(req(), "buy", 2, 1, sess, db)
    import json
    body = json.loads(page.body)
    assert [r["symbol"] for r in body["buckets"]["buy"]] == ["S1", "S2"] and body["page"] == {"bucket": "buy", "offset": 1, "returned": 2, "total": 5}


def _call(app, path="/api/v1/x"):
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request"}
    asyncio.run(app({"type": "http", "path": path}, receive, send))
    return sent


def test_a_request_longer_than_the_limit_gets_a_504_and_a_fast_one_is_untouched():
    async def slow(scope, receive, send):
        await asyncio.sleep(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def fast(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    out = _call(RequestTimeoutMiddleware(slow, 0.05))
    assert out[0]["status"] == 504 and b"request_timeout" in out[1]["body"]
    assert _call(RequestTimeoutMiddleware(fast, 0.05))[0]["status"] == 200
    assert _call(RequestTimeoutMiddleware(fast, 0.0))[0]["status"] == 200                                       # 0 = off


def test_liveness_does_not_touch_the_database():
    from app.api.health import liveness_check
    assert liveness_check() == {"status": "ok"}
