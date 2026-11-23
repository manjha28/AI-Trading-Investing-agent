"""Multibagger screen API: list fields the screen needs, the per-stock detail, and owner scoping. Rows are TEST DATA written in this file."""
from datetime import date, datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, User
from app.models.multibagger import MultibaggerAssessment
from tests.test_crypto_delta import client, engine, login  # noqa: F401  (fixtures and helper)

CHECKS = [
    {"id": "liquidity", "stage": "gate", "value": 50, "reason": "Rs 50 crore vs the floor of Rs 2 crore", "result": "pass"},
    {"id": "pledge", "stage": "veto", "value": None, "reason": "pledge of promoter holding not stored", "result": "unknown"},
    {"id": "leverage_veto", "stage": "veto", "value": 3, "reason": "D/E 3 (max 1)", "result": "fail"},
]


def seed(engine, email: str, symbol: str, klass: str):
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        inst = Instrument(symbol=symbol, display_name=symbol, asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=datetime.now(timezone.utc), is_synthetic=False)
        db.add(inst)
        db.flush()
        db.add(MultibaggerAssessment(instrument_id=inst.id, user_id=user.id, as_of=date(2026, 11, 23), computed_at=datetime(2026, 11, 23, tzinfo=timezone.utc), klass=klass, q=70.0, g=None, v=55.5, t=0.0, x=None,
                                     rank=None, model_version="test", checks={"kind": "default", "checks": CHECKS, "checks_total": 3, "checks_passed": 1, "checklist": "1 of 3 checks passed", "reasons": ["capped: pledge"],
                                                                              "timing": "wait_for_chart"}))
        db.commit()


def test_list_carries_counts_and_unknown_ids_and_detail_carries_every_check(client, engine):  # noqa: F811
    login(client, engine, "mb@example.com")
    seed(engine, "mb@example.com", "ALPHA", "watch")
    items = client.get("/api/v1/research/multibagger").json()["items"]
    row = items[0]
    assert row["checks_passed"] == 1 and row["checks_total"] == 3 and row["unknown_count"] == 1 and row["unknown_ids"] == ["pledge"] and row["fail_count"] == 1
    assert row["scores"]["G"] is None                               # unknown stays null, never 0
    detail = client.get("/api/v1/research/multibagger/alpha").json()
    assert detail["symbol"] == "ALPHA" and [c["result"] for c in detail["checks"]] == ["pass", "unknown", "fail"] and detail["checks"][1]["reason"].startswith("pledge")
    assert client.get("/api/v1/research/multibagger/NOPE").status_code == 404


def test_detail_is_owner_scoped(client, engine):  # noqa: F811
    login(client, engine, "owner1@example.com")
    seed(engine, "owner1@example.com", "BETA", "avoid")
    client.cookies.clear()
    login(client, engine, "owner2@example.com")
    assert client.get("/api/v1/research/multibagger/BETA").status_code == 404
