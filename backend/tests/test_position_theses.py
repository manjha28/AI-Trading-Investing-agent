from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.market_data import get_market_data_provider
from app.api.position_theses import _market
from app.db import Base, get_db
from app.main import app
from app.models import (
    Instrument,
    InstrumentProviderIdentifier,
    MarketQuote,
    PositionThesis,
    PositionThesisRevision,
    User,
)
from market_data_fixtures import RecordedMarketDataProvider


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def client(test_engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(test_engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_market_data_provider] = RecordedMarketDataProvider
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, test_engine, email: str, verified: bool = True) -> None:
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    if verified:
        with Session(test_engine) as db:
            user = db.scalar(select(User).where(User.email == email))
            user.email_verified = True
            db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def create_holding(client: TestClient) -> tuple[dict, dict]:
    portfolio = client.post(
        "/api/v1/portfolios", headers=csrf(client), json={"name": "Main", "base_currency": "USD"}
    ).json()
    pid = portfolio["id"]
    for body in (
        {"transaction_type": "deposit", "amount": "5000.00", "occurred_at": "2026-01-01T00:00:00Z"},
        {"transaction_type": "buy", "symbol": "ALPHA", "quantity": "10", "unit_price": "100",
         "occurred_at": "2026-01-02T00:00:00Z"},
        {"transaction_type": "buy", "symbol": "BETA", "quantity": "5", "unit_price": "10",
         "occurred_at": "2026-01-03T00:00:00Z"},
    ):
        assert client.post(
            f"/api/v1/portfolios/{pid}/transactions", headers=csrf(client), json=body
        ).status_code == 201
    instruments = client.get("/api/v1/market-data/instruments?query=ALPHA").json()
    instrument = next(item for item in instruments if item["venue"] == "DEMO_US")
    assert client.post(
        f"/api/v1/portfolios/{pid}/positions/ALPHA/instrument",
        headers=csrf(client),
        json={"instrument_id": instrument["id"]},
    ).status_code == 200
    return portfolio, instrument


def thesis_body(instrument: dict, **overrides) -> dict:
    body = {
        "symbol": "ALPHA",
        "instrument_id": instrument["id"],
        "currency": "USD",
        "entry_reason": "Durable margin expansion.",
        "horizon_days": 365,
        "conviction": 4,
        "target_levels": [
            {"price": "120", "trim_pct": "25"},
            {"price": "150", "trim_pct": "50", "note": "Book most"},
        ],
        "stop_level": "90",
        "invalidation_conditions": [{"type": "price_below", "value": "85"}],
        "invalidation_notes": "Thesis fails if margins reverse.",
        "max_loss_pct": "10",
    }
    body.update(overrides)
    return body


def url(portfolio: dict, suffix: str = "") -> str:
    return f"/api/v1/portfolios/{portfolio['id']}/theses{suffix}"


def add_quote(test_engine, instrument_id: str, *, synthetic: bool, age_hours: float = 0) -> None:
    moment = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == UUID(instrument_id),
                InstrumentProviderIdentifier.provider == "recorded",
            )
        )
        if mapping is not None:
            mapping.is_verified = True
        db.add(
            MarketQuote(
                instrument_id=UUID(instrument_id), timestamp=moment, open=Decimal("100"),
                high=Decimal("100"), low=Decimal("100"), close=Decimal("100"),
                volume=Decimal("1"), source="recorded", retrieved_at=moment, is_synthetic=synthetic,
            )
        )
        db.commit()


def test_create_returns_distances_and_flags(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    add_quote(test_engine, instrument["id"], synthetic=False)
    response = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))
    assert response.status_code == 201, response.text
    thesis = response.json()
    assert [t["distance_pct"] for t in thesis["target_levels"]] == ["20.00", "50.00"]
    assert thesis["stop_distance_pct"] == "-10.00"
    assert thesis["market"]["freshness"] == "fresh"
    assert thesis["market"]["actionable"] is True
    assert "not investment advice" in thesis["disclaimer"]


def test_synthetic_and_stale_prices_are_not_actionable(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    synthetic_market = _market(
        SimpleNamespace(
            timestamp=datetime.now(timezone.utc),
            retrieved_at=datetime.now(timezone.utc),
            source="recorded",
            close=Decimal("100"),
            is_synthetic=True,
        ),
        SimpleNamespace(currency="USD", provider_identifiers=[]),
        "USD",
    )
    assert synthetic_market.is_synthetic is True
    assert synthetic_market.actionable is False
    assert "Synthetic" in synthetic_market.note

    add_quote(test_engine, instrument["id"], synthetic=False, age_hours=72)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    stale = client.get(url(portfolio, f"/{thesis['id']}")).json()
    assert stale["market"]["freshness"] == "stale"
    assert stale["market"]["actionable"] is False


def test_unavailable_price_hides_distances(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    assert thesis["market"]["freshness"] == "unavailable"
    assert thesis["stop_distance_pct"] is None
    assert all(t["distance_pct"] is None for t in thesis["target_levels"])


def test_unverified_mapping_hides_thesis_distances(client, test_engine) -> None:
    register(client, test_engine, "unverified-thesis@example.com")
    portfolio, instrument = create_holding(client)
    add_quote(test_engine, instrument["id"], synthetic=False)
    with Session(test_engine) as db:
        mapping = db.scalar(
            select(InstrumentProviderIdentifier).where(
                InstrumentProviderIdentifier.instrument_id == UUID(instrument["id"]),
                InstrumentProviderIdentifier.provider == "recorded",
            )
        )
        assert mapping is not None
        mapping.is_verified = False
        db.commit()

    response = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))

    assert response.status_code == 201, response.text
    thesis = response.json()
    assert thesis["market"]["freshness"] == "stale"
    assert thesis["market"]["actionable"] is False
    assert thesis["market"]["last_price"] is None
    assert thesis["stop_distance_pct"] is None
    assert all(level["distance_pct"] is None for level in thesis["target_levels"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_levels": [{"price": "120", "trim_pct": "60"}, {"price": "150", "trim_pct": "50"}]},
        {"target_levels": [{"price": "150", "trim_pct": "10"}, {"price": "120", "trim_pct": "10"}]},
        {"target_levels": [{"price": "120", "trim_pct": "101"}]},
        {"target_levels": [{"price": "120", "trim_pct": "0"}]},
        {"stop_level": "125"},
        {"conviction": 6},
        {"conviction": 0},
    ],
)
def test_ladder_and_field_validation(client, test_engine, overrides) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    response = client.post(
        url(portfolio), headers=csrf(client), json=thesis_body(instrument, **overrides)
    )
    assert response.status_code == 422, response.text


def test_full_ladder_of_exactly_100_is_accepted(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    body = thesis_body(
        instrument,
        target_levels=[{"price": "120", "trim_pct": "40"}, {"price": "150", "trim_pct": "60"}],
    )
    assert client.post(url(portfolio), headers=csrf(client), json=body).status_code == 201


def test_currency_mismatch_is_rejected(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    wrong = client.post(
        url(portfolio), headers=csrf(client), json=thesis_body(instrument, currency="INR")
    )
    assert wrong.status_code == 422
    assert "no FX conversion" in wrong.text
    missing = thesis_body(instrument)
    del missing["currency"]
    assert client.post(url(portfolio), headers=csrf(client), json=missing).status_code == 422
    with Session(test_engine) as db:
        db.get(Instrument, UUID(instrument["id"])).currency = "INR"
        db.commit()
    mismatch = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))
    assert mismatch.status_code == 422
    assert "does not match portfolio currency" in mismatch.text
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(PositionThesis)) == 0


def test_update_rechecks_currency_when_prices_change(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    path = url(portfolio, f"/{thesis['id']}")
    assert client.patch(path, headers=csrf(client), json={"stop_level": "80"}).status_code == 422
    assert client.patch(
        path, headers=csrf(client), json={"stop_level": "80", "currency": "EUR"}
    ).status_code == 422
    ok = client.patch(path, headers=csrf(client), json={"stop_level": "80", "currency": "USD"})
    assert ok.status_code == 200
    assert Decimal(ok.json()["stop_level"]) == Decimal("80")
    bad_ladder = client.patch(
        path, headers=csrf(client),
        json={"stop_level": "200", "currency": "USD"},
    )
    assert bad_ladder.status_code == 422


def test_one_active_thesis_per_position(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    first = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))
    assert first.status_code == 201
    second = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))
    assert second.status_code == 409
    closed = client.patch(
        url(portfolio, f"/{first.json()['id']}"), headers=csrf(client), json={"status": "closed"}
    )
    assert closed.status_code == 200
    replacement = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument))
    assert replacement.status_code == 201
    reopen = client.patch(
        url(portfolio, f"/{first.json()['id']}"), headers=csrf(client), json={"status": "active"}
    )
    assert reopen.status_code == 409
    with Session(test_engine) as db:
        active = db.scalar(
            select(func.count()).select_from(PositionThesis).where(PositionThesis.status == "active")
        )
        assert active == 1


def test_symbol_must_match_linked_holding(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    response = client.post(
        url(portfolio), headers=csrf(client), json=thesis_body(instrument, symbol="BETA")
    )
    assert response.status_code == 422


def test_edits_write_revision_history(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    path = url(portfolio, f"/{thesis['id']}")
    assert client.patch(path, headers=csrf(client), json={"conviction": 2}).status_code == 200
    assert client.patch(path, headers=csrf(client), json={"conviction": 2}).status_code == 200
    assert client.patch(
        path, headers=csrf(client), json={"entry_reason": "Changed view", "conviction": 1}
    ).status_code == 200
    revisions = client.get(f"{path}/revisions").json()
    assert [r["revision_number"] for r in revisions] == [1, 2, 3]
    assert [r["snapshot"]["conviction"] for r in revisions] == [4, 2, 1]
    assert revisions[1]["changed_fields"] == ["conviction"]
    assert set(revisions[2]["changed_fields"]) == {"entry_reason", "conviction"}


def test_delete_removes_thesis_and_history(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    path = url(portfolio, f"/{thesis['id']}")
    assert client.delete(path, headers=csrf(client)).status_code == 204
    assert client.get(path).status_code == 404
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(PositionThesis)) == 0


def test_coverage_reports_managed_and_unmanaged(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    before = client.get(url(portfolio, "/coverage")).json()
    assert before["managed_count"] == 0
    by_symbol = {e["symbol"]: e for e in before["unmanaged"]}
    assert by_symbol["ALPHA"]["state"] == "unmanaged position"
    assert by_symbol["ALPHA"]["reason"] == "no_thesis"
    assert by_symbol["BETA"]["reason"] == "no_instrument_linked"

    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    after = client.get(url(portfolio, "/coverage")).json()
    assert [e["symbol"] for e in after["managed"]] == ["ALPHA"]
    assert after["managed"][0]["thesis_id"] == thesis["id"]
    assert after["managed"][0]["state"] == "managed"
    assert [e["symbol"] for e in after["unmanaged"]] == ["BETA"]
    assert (after["managed_count"], after["unmanaged_count"]) == (1, 1)

    client.patch(
        url(portfolio, f"/{thesis['id']}"), headers=csrf(client), json={"status": "invalidated"}
    )
    invalidated = client.get(url(portfolio, "/coverage")).json()
    alpha = next(e for e in invalidated["unmanaged"] if e["symbol"] == "ALPHA")
    assert alpha["reason"] == "thesis_not_active"
    assert alpha["thesis_status"] == "invalidated"


def test_coverage_ignores_fully_sold_positions(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, _ = create_holding(client)
    assert client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={"transaction_type": "sell", "symbol": "BETA", "quantity": "5", "unit_price": "10",
              "occurred_at": "2026-01-04T00:00:00Z"},
    ).status_code == 201
    symbols = [e["symbol"] for e in client.get(url(portfolio, "/coverage")).json()["unmanaged"]]
    assert symbols == ["ALPHA"]


def test_cross_user_isolation(client, test_engine) -> None:
    register(client, test_engine, "a@example.com")
    portfolio, instrument = create_holding(client)
    thesis = client.post(url(portfolio), headers=csrf(client), json=thesis_body(instrument)).json()
    client.cookies.clear()
    register(client, test_engine, "b@example.com")
    assert client.get(url(portfolio)).status_code == 404
    assert client.get(url(portfolio, "/coverage")).status_code == 404
    assert client.get(url(portfolio, f"/{thesis['id']}")).status_code == 404
    assert client.get(url(portfolio, f"/{thesis['id']}/revisions")).status_code == 404
    assert client.patch(
        url(portfolio, f"/{thesis['id']}"), headers=csrf(client), json={"conviction": 1}
    ).status_code == 404
    assert client.delete(url(portfolio, f"/{thesis['id']}"), headers=csrf(client)).status_code == 404
    assert client.post(
        url(portfolio), headers=csrf(client), json=thesis_body(instrument)
    ).status_code == 404
    own = client.post(
        "/api/v1/portfolios", headers=csrf(client), json={"name": "Mine", "base_currency": "USD"}
    ).json()
    assert client.get(url(own)).json() == []
    # A's thesis id is not reachable through B's own portfolio either.
    assert client.get(url(own, f"/{thesis['id']}")).status_code == 404
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(PositionThesis)) == 1


def test_authentication_verification_and_csrf(client, test_engine) -> None:
    register(client, test_engine, "a@example.com", verified=False)
    portfolio = client.post(
        "/api/v1/portfolios", headers=csrf(client), json={"name": "Main", "base_currency": "USD"}
    ).json()
    blocked = client.get(url(portfolio))
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "email_verification_required"
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        user.email_verified = True
        db.commit()
    assert client.get(url(portfolio)).status_code == 200
    body = {"symbol": "X", "instrument_id": "00000000-0000-0000-0000-000000000001"}
    assert client.post(url(portfolio), json=body).status_code == 403
    client.cookies.clear()
    assert client.get(url(portfolio)).status_code == 401
