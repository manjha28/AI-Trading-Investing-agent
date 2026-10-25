from collections.abc import Iterator
from datetime import datetime, timezone
import re
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import auth
from app.api.market_data import get_market_data_provider
from app.db import Base, get_db
from app.main import app
from app.models import Instrument, InstrumentProviderIdentifier
from market_data_fixtures import RecordedMarketDataProvider


def _get_route_cases() -> list[tuple[str, dict[str, str]]]:
    schema = app.openapi()
    prefixes = (
        "/api/v1/market-data",
        "/api/v1/instruments",
        "/api/v1/portfolios",
        "/api/v1/brokers",
    )
    cases = []
    for path, operations in schema["paths"].items():
        if not path.startswith(prefixes) or "get" not in operations:
            continue
        query: dict[str, str] = {}
        for parameter in operations["get"].get("parameters", []):
            if parameter["in"] == "query" and parameter["name"] == "portfolio_id":
                query["portfolio_id"] = "{portfolio_id}"
        if path.endswith("/market-data/instruments"):
            query["query"] = "ALPHA"
        if path.endswith("/history"):
            query.update({
                "interval": "1d",
                "start": "2026-01-05T00:00:00Z",
                "end": "2026-01-07T00:00:00Z",
                "limit": "500",
            })
        cases.append((path, query))
    return cases


ROUTE_CASES = _get_route_cases()


@pytest.fixture(scope="module")
def smoke_context() -> Iterator[tuple[TestClient, str, str]]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_market_data_provider] = RecordedMarketDataProvider
    delivered_tokens: list[str] = []
    with patch.object(
        auth,
        "send_verification_email",
        lambda _email, token: delivered_tokens.append(token),
    ), TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": "get-route-smoke@example.com",
                "password": "correct-horse-battery",
            },
        )
        assert registered.status_code == 202, registered.text
        verified = client.post(
            "/api/v1/auth/verify-email",
            json={"token": delivered_tokens[0]},
        )
        assert verified.status_code == 200, verified.text
        logged_in = client.post(
            "/api/v1/auth/login",
            json={
                "email": "get-route-smoke@example.com",
                "password": "correct-horse-battery",
            },
        )
        assert logged_in.status_code == 200, logged_in.text
        csrf = {"X-CSRF-Token": client.cookies.get("csrf_token", "")}
        portfolio = client.post(
            "/api/v1/portfolios",
            headers=csrf,
            json={"name": "Smoke portfolio", "base_currency": "USD"},
        )
        assert portfolio.status_code == 201, portfolio.text
        observed_at = datetime.now(timezone.utc)
        with Session(engine) as db:
            instrument = Instrument(
                symbol="ALPHA",
                display_name="Route smoke instrument",
                asset_class="equity",
                venue="DEMO_US",
                currency="USD",
                country="US",
                timezone="UTC",
                is_synthetic=False,
                relink_required=False,
                created_at=observed_at,
            )
            db.add(instrument)
            db.commit()
            db.add(InstrumentProviderIdentifier(
                instrument_id=instrument.id,
                provider="recorded",
                provider_instrument_id="FIXTURE-US-ALPHA-EQ-001",
                provider_symbol="DEMO_US:ALPHA",
                is_verified=True,
                last_seen_at=observed_at,
            ))
            db.commit()
            instrument_id = str(instrument.id)
        yield client, portfolio.json()["id"], instrument_id
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.parametrize(
    ("path_template", "query"),
    ROUTE_CASES,
    ids=[path for path, _ in ROUTE_CASES],
)
def test_every_scoped_get_route_avoids_server_errors(
    smoke_context: tuple[TestClient, str, str],
    path_template: str,
    query: dict[str, str],
) -> None:
    client, portfolio_id, instrument_id = smoke_context
    substitutions = {
        "portfolio_id": portfolio_id,
        "instrument_id": instrument_id,
        "thesis_id": str(uuid4()),
        "slug": "dhan",
    }
    path = re.sub(
        r"\{([a-z_]+)\}",
        lambda match: substitutions[match.group(1)],
        path_template,
    )
    params = {
        key: value.format(portfolio_id=portfolio_id)
        for key, value in query.items()
    }

    response = client.get(path, params=params)

    assert response.status_code < 500, (
        f"GET {path} returned HTTP {response.status_code}: {response.text}"
    )
