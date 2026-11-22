"""Fundamentals backfill and the pipeline's rate-limited vs no-statements distinction (test data only: hand-made instruments and rows in an in-memory database; no network)."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import yahoo_guard
from app.db import Base
from app.models import Instrument, InstrumentFundamental, InstrumentSnapshot
from app.models.fundamentals_ops import FundamentalsFetchLog
from app.services import fundamentals_backfill as fb
from app.services import research_pipeline as rp

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


@pytest.fixture(autouse=True)
def _clean_guard():
    yahoo_guard.reset()
    yield
    yahoo_guard.reset()


def _inst(db, sym):
    i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _stmt(db, inst, n_annual, fetched=NOW):
    for k in range(n_annual):
        db.add(InstrumentFundamental(id=uuid4(), instrument_id=inst.id, period_type="annual", period_end=date(2026 - k, 3, 31), currency="INR", revenue=Decimal("1"), source="yfinance", fetched_at=fetched))
    db.commit()


def test_error_classification():
    assert fb.classify_error(RuntimeError("HTTP Error 429: Too Many Requests")) == "rate_limited"
    assert fb.classify_error(ValueError("401 Invalid Crumb")) == "rate_limited"
    assert fb.classify_error(KeyError("revenue")) == "failed"


def test_queue_selects_missing_and_stale_orders_by_kind_and_skips_recent_no_statements(eng):
    with Session(eng) as db:
        a, b, c, d, e_, f = (_inst(db, s) for s in "ABCDEF")
        _stmt(db, a, 4)                                                    # healthy: never queued
        _stmt(db, b, 3, fetched=NOW - timedelta(days=200))                 # stale statements
        fb.record_attempt(db, c.id, "rate_limited", now=NOW - timedelta(hours=5))          # retry queued
        fb.record_attempt(db, d.id, "no_statements", now=NOW - timedelta(days=3))          # Yahoo has none: skip for 30 days
        fb.record_attempt(db, f.id, "no_statements", now=NOW - timedelta(days=45))         # old answer: ask again
        q = fb.candidates(db, [a, b, c, d, e_, f], NOW)
        assert [(i.symbol, k) for i, k in q] == [("C", "rate_limited retry"), ("E", "never attempted"), ("F", "no annual statements"), ("B", "stale statements")]


def test_backoff_cooloff_and_requeue(eng):
    with Session(eng) as db:
        insts = [_inst(db, s) for s in "ABCDE"]
    plan = {"A": ["rate_limited", "refreshed"], "B": ["rate_limited", "refreshed"], "C": ["rate_limited", "refreshed"], "D": ["no_statements"], "E": ["refreshed"]}
    sleeps, calls = [], []

    def fetcher(db, inst):
        out = plan[inst.symbol].pop(0)
        calls.append((inst.symbol, out))
        if out == "refreshed":
            _stmt(db, inst, 3)
        record = fb.record_attempt
        record(db, inst.id, out, annual=3 if out == "refreshed" else None, origin="backfill")
        return out

    res = fb.run_backfill(lambda: Session(eng), lambda db: list(db.scalars(select(Instrument).order_by(Instrument.symbol))), pause=4.0, backoff_base=8.0, cooloff_seconds=1800.0, max_consecutive_rate_limits=3,
                          fetcher=fetcher, sleep=sleeps.append)
    # pass 1: A, B, C rate limited in a row -> 8 s, 16 s, then the 30-minute rest; then D (no statements) and E (refreshed); pass 2 retries A, B, C
    assert sleeps[:3] == [8.0, 16.0, 1800.0]
    assert [c for c in calls[:5]] == [("A", "rate_limited"), ("B", "rate_limited"), ("C", "rate_limited"), ("D", "no_statements"), ("E", "refreshed")]
    assert res["rate_limited"] == 3 and res["refreshed"] == 4 and res["no_statements"] == 1 and res["cooloffs"] == 1 and res["passes"] == 2
    assert sleeps.count(4.0) >= 5                                                 # one pause after every non-rate-limited request
    with Session(eng) as db:                                                     # resumable: a second run finds nothing left to do
        assert fb.candidates(db, list(db.scalars(select(Instrument)))) == []


def test_an_open_breaker_rests_and_resets_instead_of_hammering(eng):
    with Session(eng) as db:
        _inst(db, "A")
    for _ in range(yahoo_guard.FAILURES_TO_TRIP):
        yahoo_guard.record(False)
    assert not yahoo_guard.allowed()
    sleeps = []

    def fetcher(db, inst):
        assert yahoo_guard.allowed()                                              # the job rested and cleared its own breaker before asking
        _stmt(db, inst, 3)
        return "refreshed"

    res = fb.run_backfill(lambda: Session(eng), lambda db: list(db.scalars(select(Instrument))), fetcher=fetcher, sleep=sleeps.append)
    assert sleeps[0] == 1800.0 and res["cooloffs"] == 1 and res["refreshed"] == 1


# ───────────── the pipeline distinguishes the states ─────────────
def _patch_provider(monkeypatch, sync):
    monkeypatch.setattr(rp, "create_market_data_provider_chain", lambda names: names)
    monkeypatch.setattr(rp, "_providers", lambda chain: [SimpleNamespace(name="yfinance")])
    monkeypatch.setattr(rp, "_resolve_provider_candidate", lambda db, inst, prov: (SimpleNamespace(provider_symbol="X.NS"), None))
    monkeypatch.setattr(rp.fundamentals_service, "sync_instrument_fundamentals", sync)


def test_breaker_open_is_rate_limited_and_queued_not_cached(eng, monkeypatch):
    with Session(eng) as db:
        i = _inst(db, "A")
        db.add(InstrumentSnapshot(id=uuid4(), instrument_id=i.id, source="yfinance", market_cap=Decimal("1"), fetched_at=datetime.now(timezone.utc)))
        db.commit()                                                              # a fresh snapshot but NO statements: this used to read as 'cached'
        for _ in range(yahoo_guard.FAILURES_TO_TRIP):
            yahoo_guard.record(False)
        state, reason, _ = rp._refresh_yahoo_fundamentals(db, i)
        assert state == "rate_limited" and "not fetched (rate limited)" in reason and "retry is queued" in reason
        assert fb.latest_log(db, i.id).outcome == "rate_limited"
        assert [x.symbol for x, _ in fb.candidates(db, [i])] == ["A"]            # and it is in the retry queue


def test_empty_yahoo_answer_is_no_statements_and_is_not_asked_again(eng, monkeypatch):
    calls = []

    def sync(db, inst, sym):
        calls.append(sym)
        return [], SimpleNamespace(fetched_at=datetime.now(timezone.utc))

    _patch_provider(monkeypatch, sync)
    with Session(eng) as db:
        i = _inst(db, "A")
        state, reason, _ = rp._refresh_yahoo_fundamentals(db, i)
        assert state == "no_statements" and "no financial statements" in reason and fb.latest_log(db, i.id).outcome == "no_statements"
        state2, _r, _t = rp._refresh_yahoo_fundamentals(db, i)
        assert state2 == "no_statements" and calls == ["X.NS"]                   # the second run did not call Yahoo again


def test_a_429_is_rate_limited_not_failed(eng, monkeypatch):
    from app.broker_adapters import BrokerAdapterError

    def sync(db, inst, sym):
        raise BrokerAdapterError("HTTP 429 Too Many Requests")

    _patch_provider(monkeypatch, sync)
    with Session(eng) as db:
        i = _inst(db, "A")
        state, reason, _ = rp._refresh_yahoo_fundamentals(db, i)
        assert state == "rate_limited" and "retry is queued" in reason
        assert db.scalar(select(FundamentalsFetchLog.outcome).where(FundamentalsFetchLog.instrument_id == i.id)) == "rate_limited"


def test_statements_present_and_fresh_are_cached(eng, monkeypatch):
    _patch_provider(monkeypatch, lambda *a: pytest.fail("must not call Yahoo"))
    monkeypatch.setattr(rp, "_refresh_yahoo_valuation_inputs", lambda *a, **k: None)
    with Session(eng) as db:
        i = _inst(db, "A")
        _stmt(db, i, 3, fetched=datetime.now(timezone.utc))
        db.add(InstrumentSnapshot(id=uuid4(), instrument_id=i.id, source="yfinance", market_cap=Decimal("1"), fetched_at=datetime.now(timezone.utc)))
        db.commit()
        assert rp._refresh_yahoo_fundamentals(db, i)[0] == "cached"
