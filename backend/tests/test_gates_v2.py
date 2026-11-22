"""Nexus v2 Phase 3 gates (flag pipeline_v2_enabled). Test data only: hand-built bars, the NSE filing wording copied from stored real headlines, and an in-memory database."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base
from app.models import CorporateAction, Instrument, NewsItem, User
from app.services import gates_v2 as G
from app.services import research_pipeline as rp

IST = G.IST


def _bars(n=30, close=100.0, volume=1_000_000, end="2026-11-18"):
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": float(volume)}, index=idx)


# ───────────── liquidity ─────────────
@pytest.mark.parametrize("close,volume,horizon,expect", [
    (100.0, 1_200_000, "swing", True),       # Rs 12 crore a day vs the Rs 10 crore floor
    (100.0, 900_000, "swing", False),        # Rs 9 crore: below the swing floor ...
    (100.0, 900_000, "positional", True),    # ... but above the positional Rs 5 crore floor
    (100.0, 400_000, "positional", False),   # Rs 4 crore
    (100.0, 250_000, "long_term", True),     # Rs 2.5 crore vs Rs 2 crore
    (100.0, 150_000, "long_term", False),    # Rs 1.5 crore
])
def test_liquidity_floors_per_horizon(close, volume, horizon, expect):
    g = G.liquidity_gate(_bars(close=close, volume=volume), horizon)
    assert g["pass"] is expect and g["status"] == ("pass" if expect else "fail")
    assert g["floor_crore"] == {"swing": 10.0, "positional": 5.0, "long_term": 2.0}[horizon]


def test_liquidity_uses_the_median_so_one_spike_does_not_pass_an_illiquid_stock():
    df = _bars(volume=100_000)
    df.iloc[-1, df.columns.get_loc("volume")] = 500_000_000.0
    assert G.liquidity_gate(df, "long_term")["pass"] is False


def test_too_few_usable_sessions_is_unknown_and_blocks():
    g = G.liquidity_gate(_bars(n=10, volume=5_000_000), "swing")
    assert g["status"] == "unknown" and g["pass"] is False and "cannot be confirmed" in g["reason"]
    df = _bars(volume=5_000_000)
    df.iloc[-18:, df.columns.get_loc("volume")] = 0.0                              # zero-volume sessions are not usable sessions
    assert G.liquidity_gate(df, "swing")["status"] == "unknown"


# ───────────── event dates from NSE wording ─────────────
@pytest.mark.parametrize("headline,category,expect", [
    ("POWER MECH PROJECTS LIMITED has informed the Exchange about Board Meeting to be held on 11-Nov-2026 to consider and approve the Quarterly Unaudited Financial results of the Company for the period ended September 2026 and Other business.",
     "Board Meeting Intimation", [("results", date(2026, 11, 11))]),
    ("Esab India Limited has informed the Exchange that Record date for the purpose of Dividend  is 10-Nov-2026.", "Record Date", [("record_date", date(2026, 11, 10))]),
    ("Ashapura Intimates Fashion Limited had intimated the 16-Oct-2026 as the record date. The Company has now informed the Exchange that the Record date has been revised to 26-Oct-2026 for the purpose of RC.",
     "Revised Record date", [("record_date", date(2026, 10, 26))]),
    ("Zenith Steel Pipes has submitted to the Exchange, the financial results for the period ended September 30, 2026.", "Outcome of Board Meeting", []),      # an outcome, not an upcoming event
    ("Board Meeting to be held on 11-Nov-2026 to consider fund raising", "Board Meeting Intimation", []),                                                    # not a results agenda
    ("nothing parseable here", "Record Date", []),
])
def test_parse_event_dates(headline, category, expect):
    assert G.parse_event_dates(headline, category) == expect


def test_sessions_between_counts_trading_days_not_calendar_days():
    assert G.sessions_between(date(2026, 11, 20), date(2026, 11, 23)) == 1          # Fri -> Mon is ONE session ahead
    assert G.sessions_between(date(2026, 11, 20), date(2026, 11, 20)) == 0
    assert G.sessions_between(date(2026, 11, 18), date(2026, 11, 20)) == 2


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


def _inst(db, sym="AAA"):
    i = Instrument(id=uuid4(), symbol=sym, display_name=sym, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False,
                   created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    db.add(i)
    db.commit()
    return i


def _filing(db, inst, headline, category, published):
    db.add(NewsItem(id=uuid4(), external_id=str(uuid4()), source="nse", source_url="https://example.invalid/x", instrument_id=inst.id, symbol_raw=inst.symbol, published_at=published, retrieved_at=published,
                    publisher="NSE", category=category, headline=headline, body="", raw_payload={}))
    db.commit()


def test_event_inside_three_sessions_is_found_and_outside_is_not(eng):
    pub = datetime(2026, 11, 10, tzinfo=timezone.utc)
    with Session(eng) as db:
        i = _inst(db)
        _filing(db, i, "X has informed the Exchange about Board Meeting to be held on 25-Nov-2026 to consider and approve the Quarterly Unaudited Financial results", "Board Meeting Intimation", pub)
        _filing(db, i, "X has informed the Exchange that Record date for the purpose of Dividend is 30-Nov-2026.", "Record Date", pub)
        # 24 Nov 2026 is an NSE holiday: from Fri 20 Nov the sessions are Mon 23 (1) and Wed 25 (2); the 30th is 5 sessions away
        ev = G.upcoming_events(db, i.id, None, date(2026, 11, 20))
        assert [(e["kind"], e["date"], e["sessions_ahead"]) for e in ev] == [("results", "2026-11-25", 2)]
        assert G.upcoming_events(db, i.id, None, date(2026, 11, 23))[0]["sessions_ahead"] == 1
        assert [e["kind"] for e in G.upcoming_events(db, i.id, None, date(2026, 11, 25))] == ["results", "record_date"]   # results today (0 sessions), record date 3 sessions ahead (26, 27, 30)
        assert [e["kind"] for e in G.upcoming_events(db, i.id, None, date(2026, 11, 26))] == ["record_date"]
        assert G.upcoming_events(db, i.id, None, date(2026, 12, 1)) == []                              # both already passed


def test_ex_date_comes_from_the_owners_corporate_actions_only(eng):
    with Session(eng) as db:
        i = _inst(db)
        uid, other = uuid4(), uuid4()
        for u in (uid, other):
            db.add(User(id=u, email=f"{u}@example.invalid", password_hash="x", email_verified=True, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        db.commit()
        db.add(CorporateAction(id=uuid4(), user_id=uid, instrument_id=i.id, action_type="dividend", ex_date=date(2026, 11, 25), amount=Decimal("5"), source="yfinance", created_at=datetime(2026, 11, 1, tzinfo=timezone.utc)))
        db.commit()
        assert [e["kind"] for e in G.upcoming_events(db, i.id, uid, date(2026, 11, 20))] == ["ex_date"]
        assert G.upcoming_events(db, i.id, other, date(2026, 11, 20)) == []                          # owner-scoped


# ───────────── still-forming bar ─────────────
def _now(h, m, day=date(2026, 11, 18)):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).astimezone(timezone.utc)


@pytest.mark.parametrize("h,m,kept", [(10, 0, False), (15, 45, False), (17, 29, False), (17, 31, True), (19, 0, True)])
def test_todays_forming_daily_bar_is_never_treated_as_completed(h, m, kept):
    df = _bars(end="2026-11-18")                                                   # last bar is dated Wed 18 Nov 2026
    out = G.drop_forming_bar(df, _now(h, m, date(2026, 11, 18)))
    assert (len(out) == len(df)) is kept
    if not kept:
        assert out.index[-1] == df.index[-2]


def test_an_older_last_bar_is_kept_overnight():
    df = _bars(end="2026-11-18")
    assert len(G.drop_forming_bar(df, _now(9, 30, date(2026, 11, 19)))) == len(df)


# ───────────── application to levels ─────────────
def _levels(status="ENTER_NOW"):
    card = {"status": "available", "entry_status": status, "ready_to_buy": status == "ENTER_NOW", "entry_label": "In buy band now", "reference_close": "100.00"}
    return {"horizons": {"swing": dict(card), "positional": dict(card), "long_term": dict(card)}}


def test_failing_liquidity_removes_only_that_horizon_and_events_stop_enter_now():
    gates = {"liquidity": {"swing": {"pass": False, "status": "fail", "reason": "below floor"}, "positional": {"pass": True, "status": "pass", "reason": "ok"}, "long_term": {"pass": True, "status": "pass", "reason": "ok"}},
             "events": [{"kind": "results", "date": "2026-11-25", "sessions_ahead": 2}], "event_calendar_note": "partial"}
    out = G.apply_to_levels(_levels(), gates)["horizons"]
    assert out["swing"]["status"] == "gated" and "Liquidity gate" in out["swing"]["reason"]
    for h in ("positional", "long_term"):
        assert out[h]["entry_status"] == "WAIT_FOR_ENTRY" and out[h]["ready_to_buy"] is False and "results on 2026-11-25" in out[h]["event_gate"] and out[h]["gates"]["event_calendar_note"] == "partial"
    assert G.apply_to_levels(_levels(), None)["horizons"]["swing"]["entry_status"] == "ENTER_NOW"          # no gates object = untouched (flag off)


def test_assessment_text_drops_the_liquidity_and_event_entries_only_with_the_flag(monkeypatch):
    from app.services import research_scoring as rs
    assert rs.NOT_IMPLEMENTED_GATES and any("liquidity" in g for g in rs.NOT_IMPLEMENTED_GATES)
    sections = {"fundamentals": {"periods": [], "snapshot": {}}, "technicals": {"status": "unavailable"}, "financials_trend": {}, "news_sentiment": None, "valuation": None}
    monkeypatch.setattr(settings, "pipeline_v2_enabled", False)
    assert rs.build_assessment(sections, instrument_id="x")["not_implemented_gates"] == rs.NOT_IMPLEMENTED_GATES
    monkeypatch.setattr(settings, "pipeline_v2_enabled", True)
    assert rs.build_assessment(sections, instrument_id="x")["not_implemented_gates"] == []


def test_brief_with_unavailable_technicals_still_builds_levels_without_a_name_error(eng, monkeypatch):
    """Regression: price_basis / gates were defined only inside the 'technicals available' branch."""
    with Session(eng) as db:
        i = _inst(db)
        sections, _quality = rp.render_brief(db, i, [], [], user_id=None)
        assert "error" not in (sections.get("levels") or {}), sections.get("levels")
        assert sections["levels"]["horizons"]["swing"]["status"] in ("not_applicable", "stale_or_missing_data", "insufficient_history")


def test_a_stale_or_missing_filings_feed_is_reported_on_the_gate_output(eng):
    from app.models import NewsSourcePoll
    now = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
    with Session(eng) as db:
        i = _inst(db)
        assert G.filings_feed_status(db, now)["stale"] is True                      # never polled: unknown, not fine
        db.add(NewsSourcePoll(source="nse", last_attempted_at=now, last_successful_at=datetime(2026, 11, 5, tzinfo=timezone.utc), last_error_category="upstream_failure", rate_limited=False, articles_discovered=0))
        db.commit()
        feed = G.filings_feed_status(db, now)
        assert feed["stale"] and feed["age_days"] == 17 and feed["last_error"] == "upstream_failure"
        out = G.compute(db, i.id, None, _bars(), now)
        assert out["event_calendar_note"].startswith("EVENT CALENDAR UNRELIABLE") and "2026-11-05" in out["event_calendar_note"]
        db.get(NewsSourcePoll, "nse") if False else None
        poll = db.query(NewsSourcePoll).filter_by(source="nse").one()
        poll.last_successful_at = now - timedelta(hours=6)
        db.commit()
        assert G.filings_feed_status(db, now)["stale"] is False
