"""Provider-unserved windows: excluded from the refusal share, disclosed, threshold unchanged. Frames below are TEST DATA only."""
import pandas as pd

from app.services.bar_quality import DEFAULT_REFUSE_MISSING_PCT, clean_and_validate
from app.services.timeframes import EQUITY_IN

DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]


def _frame(last_minute: int, days=DAYS, drop_minutes: int = 0) -> pd.DataFrame:
    """One bar per minute from 09:15 IST to ``last_minute`` (exchange-local minute of day) on each day; test fixture."""
    times = []
    for day in days:
        start = pd.Timestamp(f"{day} 09:15", tz="Asia/Kolkata")
        n = last_minute - 9 * 60 - 15 + 1
        keep = [start + pd.Timedelta(minutes=i) for i in range(n - drop_minutes)]
        times += [t.tz_convert("UTC") for t in keep]
    return pd.DataFrame({"time": times, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0})


NOW = pd.Timestamp("2026-10-20 12:00", tz="UTC")
TAIL = [(15 * 60 + 15, 15 * 60 + 30)]


def test_structural_tail_without_exclusion_is_refused():
    _, q = clean_and_validate(_frame(15 * 60 + 14), EQUITY_IN, now=NOW)
    assert q["missing_pct"] == 4.0 and q["status"] == "warn"
    # one more genuinely missing slice pushes the unexcluded share over the unchanged 5% limit
    _, q2 = clean_and_validate(_frame(15 * 60 + 14, drop_minutes=5), EQUITY_IN, now=NOW)
    assert q2["status"] == "refuse"


def test_unserved_tail_excluded_from_missing_share_and_disclosed():
    _, q = clean_and_validate(_frame(15 * 60 + 14), EQUITY_IN, now=NOW, unserved=TAIL)
    assert q["missing_minutes"] == 0 and q["missing_pct"] == 0.0 and q["status"] == "ok"
    assert q["expected_minutes"] == 5 * 360
    unserved = q["provider_unserved"]
    assert unserved["excluded_minutes"] == 5 * 15
    assert unserved["ranges_local"] == [{"start": "15:15", "end": "15:29"}]


def test_real_fetch_failures_in_served_minutes_still_count_and_threshold_is_unchanged():
    _, q = clean_and_validate(_frame(15 * 60 + 14, drop_minutes=30), EQUITY_IN, now=NOW, unserved=TAIL)
    assert q["missing_minutes"] == 5 * 30
    assert q["missing_pct"] == round(100 * 150 / (5 * 360), 3)
    assert DEFAULT_REFUSE_MISSING_PCT == 5.0
    assert q["status"] == "refuse"  # 8.333% > 5%


def test_no_data_session_counts_only_served_minutes():
    frame = _frame(15 * 60 + 14, days=["2026-10-12", "2026-10-14"])  # 2026-10-13 is a trading day with no rows
    _, q = clean_and_validate(frame, EQUITY_IN, now=NOW, unserved=TAIL)
    assert q["sessions_no_data"] == ["2026-10-13"]
    assert q["missing_minutes"] == 360
    assert q["expected_minutes"] == 3 * 360


# ───────── batch backfill: rate-limit pause, before/after, unserved detection (TEST DATA in an in-memory database) ─────────
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, MarketBar
from app.services import intraday_backfill
from app.services.provider_coverage import describe, detect_unserved_tail

SESSIONS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-21"]


def _db_with_bars(last_minute: int):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    inst = Instrument(symbol="TESTSBIN", display_name="Test", venue="NSE", country="IN", currency="INR", timezone="Asia/Kolkata", asset_class="equity", is_synthetic=False,
                      relink_required=False, created_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    db.add(inst)
    db.commit()
    for t in _frame(last_minute, days=SESSIONS)["time"]:
        db.add(MarketBar(instrument_id=inst.id, interval="1m", timestamp=t.to_pydatetime(), open=100, high=101, low=99, close=100, volume=1,
                         source="dhan", retrieved_at=datetime(2026, 10, 22, tzinfo=timezone.utc), is_synthetic=False))
    db.commit()
    return db, inst


def test_detection_records_consistent_tail_and_describes_it():
    db, inst = _db_with_bars(15 * 60 + 14)
    window = detect_unserved_tail(db, inst, "dhan", datetime(2026, 10, 25, tzinfo=timezone.utc))
    assert window is not None and (window.start_minute, window.end_minute) == (915, 930)
    assert describe(db, inst.id)["label"] == "15:15-15:29 not served by dhan"


def test_detection_requires_a_consistent_cut_not_a_one_off():
    db, inst = _db_with_bars(15 * 60 + 29)
    assert detect_unserved_tail(db, inst, "dhan", datetime(2026, 10, 25, tzinfo=timezone.utc)) is None


def test_batch_backfill_pauses_on_rate_limit_then_reports_before_after(monkeypatch):
    db, inst = _db_with_bars(15 * 60 + 14)
    calls, pauses, rows = [], [], {}

    def sync_history(**_kw):
        calls.append(1)
        if len(calls) == 1:
            attempt = {"provider": "dhan", "outcome": "rate_limited", "http_status": 429, "bars": 0, "reason": "rate_limited"}
            return SimpleNamespace(sync_status="unavailable", provider_attempts=[SimpleNamespace(model_dump=lambda a=attempt: a)])
        return SimpleNamespace(sync_status="success", provider_attempts=[SimpleNamespace(model_dump=lambda: {"provider": "dhan", "bars": 7, "outcome": "used"})])

    job = {"pause": None}
    fake_md = SimpleNamespace(
        sync_history=sync_history, MAX_DHAN_1M_BACKFILL_BARS=250_000, MAX_DHAN_1M_CHUNK_BARS=30_000,
        market_data_provider_for_user=lambda *_a: object(), _history_sync_jobs_lock=__import__("threading").Lock(),
        _update_history_sync_job=lambda _id, **kw: (job.update(kw), kw.get("pause") and pauses.append(kw["pause"])))
    monkeypatch.setattr(intraday_backfill.time, "sleep", lambda _s: None)
    # 2026-10-20 is an NSE holiday and 10-21 has data; leave one real hole on 10-21 so the fetch is attempted
    start, end = datetime(2026, 10, 11, 18, 30, tzinfo=timezone.utc), datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
    store = [{"status": "queued"}]
    intraday_backfill._backfill_one(fake_md, db, "j", 0, SimpleNamespace(id=uuid4()), inst, start, end, lambda i, **f: store[i].update(f))
    row = store[0]
    assert len(calls) == 2 and pauses and pauses[0]["seconds"] == 20          # paused 20 s, then retried
    assert row["before"]["missing_pct"] > 0.0 and row["after"]["missing_pct"] == 0.0   # structural tail excluded after detection
    assert row["provider_coverage"]["label"] == "15:15-15:29 not served by dhan"
    assert row["status"] == "completed" and row["reason"] is None
