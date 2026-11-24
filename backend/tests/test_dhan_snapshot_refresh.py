"""The Dhan snapshot is refreshed by a background job in market hours only, skips an expired session, and no page triggers it."""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import dhan_snapshot_refresh as dsr
from app.models import BrokerCredential, User
from tests.test_crypto_delta import engine  # noqa: F401

OPEN = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)            # Mon 11:30 IST
NIGHT = datetime(2026, 11, 23, 19, 0, tzinfo=timezone.utc)          # Tue 00:30 IST
SUNDAY = datetime(2026, 11, 22, 6, 0, tzinfo=timezone.utc)


def test_it_refreshes_unexpired_sessions_in_market_hours_and_skips_expired_ones_and_closed_hours(engine, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr("app.api.brokers.sync_broker", lambda **kw: calls.append((kw["slug"], kw["snapshot_only"], kw["active_session"].user_id)))
    monkeypatch.setattr(dsr, "engine", engine)
    with Session(engine) as db:
        a, b = User(email="a@example.com", password_hash="x", email_verified=True, created_at=OPEN), User(email="b@example.com", password_hash="x", email_verified=True, created_at=OPEN)
        db.add_all([a, b]); db.flush()
        db.add_all([BrokerCredential(user_id=a.id, broker="dhan", encrypted_tokens="x", expires_at=OPEN + timedelta(hours=3), connected_at=OPEN),
                    BrokerCredential(user_id=b.id, broker="dhan", encrypted_tokens="x", expires_at=OPEN - timedelta(hours=1), connected_at=OPEN)])
        db.commit()
        alive = a.id
    assert dsr.run_pass(OPEN) == {"users": 1, "refreshed": 1, "skipped_expired": 1, "errors": 0}
    assert calls == [("dhan", True, alive)]
    assert dsr.run_pass(NIGHT)["refreshed"] == 0 and dsr.run_pass(SUNDAY)["refreshed"] == 0 and len(calls) == 1      # nights and weekends never sync
