from collections.abc import Iterator
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    CorporateActionCoverage,
    Instrument,
    MarketBar,
    PortfolioTransaction,
    User,
)
from app.corporate_action_service import (
    CorporateActionCoverageError,
    adjusted_bars_for_backtest,
    sync_yfinance_corporate_actions,
)


@pytest.fixture
def test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def register(client: TestClient, email: str) -> None:
    registration = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert registration.status_code == 202
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct-horse-battery"},
    )
    assert login.status_code == 200


def verify_email(test_engine, email: str) -> None:
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        user.email_verified = True
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def create_position(client: TestClient, test_engine, name: str) -> tuple[dict, dict]:
    portfolio_response = client.post(
        "/api/v1/portfolios",
        headers=csrf(client),
        json={"name": name, "base_currency": "USD"},
    )
    assert portfolio_response.status_code == 201, portfolio_response.text
    portfolio = portfolio_response.json()
    portfolio_id = portfolio["id"]
    deposit = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "deposit",
            "amount": "5000.00",
            "occurred_at": "2026-01-01T00:00:00Z",
        },
    )
    assert deposit.status_code == 201, deposit.text
    buy = client.post(
        f"/api/v1/portfolios/{portfolio_id}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "buy",
            "symbol": "ALPHA",
            "quantity": "10",
            "unit_price": "100",
            "occurred_at": "2026-01-02T00:00:00Z",
        },
    )
    assert buy.status_code == 201, buy.text
    with Session(test_engine) as db:
        user = db.scalar(select(User).order_by(User.created_at.desc()))
        assert user is not None
        row = Instrument(
            id=uuid4(), symbol="ALPHA", display_name="Alpha Test", asset_class="equity",
            venue="DEMO_US", currency="USD", country="US", timezone="America/New_York",
            is_synthetic=False, relink_required=False, created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        instrument = {"id": str(row.id), "symbol": row.symbol, "venue": row.venue}
    linked = client.post(
        f"/api/v1/portfolios/{portfolio_id}/positions/ALPHA/instrument",
        headers=csrf(client),
        json={"instrument_id": instrument["id"]},
    )
    assert linked.status_code == 200, linked.text
    return portfolio, instrument


def seed_adjustment_instrument(client: TestClient, test_engine) -> tuple[str, str]:
    email = "adjustment@example.com"
    register(client, email)
    verify_email(test_engine, email)
    _, instrument = create_position(client, test_engine, "Adjustment portfolio")
    with Session(test_engine) as db:
        row = db.get(Instrument, UUID(instrument["id"]))
        assert row is not None
        row.venue = "NSE"
        row.country = "IN"
        row.timezone = "Asia/Kolkata"
        row.currency = "INR"
        user_id = db.scalar(select(User.id).where(User.email == email))
        assert user_id is not None
        db.add(CorporateActionCoverage(
            user_id=user_id,
            instrument_id=row.id,
            source="yfinance",
            status="verified",
            checked_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            source_reference="ALPHA.NS",
        ))
        db.add_all([
            MarketBar(
                instrument_id=row.id, interval="1d",
                timestamp=datetime(2026, 1, 9, tzinfo=timezone.utc),
                open=Decimal("100"), high=Decimal("110"), low=Decimal("90"),
                close=Decimal("100"), volume=Decimal("10"), source="yfinance",
                retrieved_at=datetime(2026, 1, 10, tzinfo=timezone.utc), is_synthetic=False,
            ),
            MarketBar(
                instrument_id=row.id, interval="1d",
                timestamp=datetime(2026, 1, 10, tzinfo=timezone.utc),
                open=Decimal("50"), high=Decimal("55"), low=Decimal("45"),
                close=Decimal("50"), volume=Decimal("20"), source="yfinance",
                retrieved_at=datetime(2026, 1, 11, tzinfo=timezone.utc), is_synthetic=False,
            ),
        ])
        db.commit()
        return str(user_id), str(row.id)


def create_action(
    client: TestClient,
    instrument_id: str,
    action_type: str,
    *,
    ratio: str | None = None,
    amount: str | None = None,
    ex_date: str = "2026-01-10",
) -> dict:
    response = client.post(
        "/api/v1/corporate-actions",
        headers=csrf(client),
        json={
            "instrument_id": instrument_id,
            "action_type": action_type,
            "ex_date": ex_date,
            "ratio": ratio,
            "amount": amount,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["verified_at"] is None
    assert response.json()["verified_by"] is None
    return response.json()


def apply_action(client: TestClient, portfolio_id: str, action_id: str):
    return client.post(
        f"/api/v1/corporate-actions/portfolios/{portfolio_id}/{action_id}/apply",
        headers=csrf(client),
    )


def test_split_application_is_idempotent_reversible_and_preserves_cost_and_start(
    client: TestClient, test_engine
) -> None:
    email = "split@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, test_engine, "Split portfolio")
    action = create_action(client, instrument["id"], "split", ratio="2")

    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    adjustment = applied.json()
    assert adjustment["affected_quantity"] == "10.00000000"
    assert adjustment["quantity_adjustment"] == "10.00000000"
    assert adjustment["cash_adjustment"] == "0.00"
    repeated = apply_action(client, portfolio["id"], action["id"])
    assert repeated.status_code == 200
    assert repeated.json()["id"] == adjustment["id"]

    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    position = summary["positions"][0]
    assert position["quantity"] == "20.00000000"
    assert position["cost_basis"] == "1000.00"
    assert position["average_cost"] == "50.00"
    assert position["holding_period_start"] == "2026-01-02T00:00:00Z"

    reversed_response = client.post(
        f"/api/v1/corporate-actions/portfolios/{portfolio['id']}/applications/{adjustment['id']}/reverse",
        headers=csrf(client),
    )
    assert reversed_response.status_code == 200, reversed_response.text
    assert reversed_response.json()["active"] is False
    restored = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    assert restored["positions"][0]["quantity"] == "10.00000000"
    assert restored["positions"][0]["cost_basis"] == "1000.00"
    with Session(test_engine) as db:
        assert db.scalar(select(func.count()).select_from(PortfolioTransaction)) == 2


def test_one_to_one_bonus_creates_new_holding_period_without_changing_total_cost(
    client: TestClient, test_engine
) -> None:
    email = "bonus@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, test_engine, "Bonus portfolio")
    action = create_action(client, instrument["id"], "bonus", ratio="1")
    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["quantity_adjustment"] == "10.00000000"

    sale = client.post(
        f"/api/v1/portfolios/{portfolio['id']}/transactions",
        headers=csrf(client),
        json={
            "transaction_type": "sell",
            "symbol": "ALPHA",
            "quantity": "10",
            "unit_price": "60",
            "occurred_at": "2026-01-15T00:00:00Z",
        },
    )
    assert sale.status_code == 201, sale.text
    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    position = summary["positions"][0]
    assert position["quantity"] == "10.00000000"
    assert position["cost_basis"] == "500.00"
    assert position["average_cost"] == "50.00"
    assert position["holding_period_start"] == "2026-01-10T00:00:00Z"


def test_cash_dividend_uses_ex_date_shares_and_updates_cash_and_realized_income(
    client: TestClient, test_engine
) -> None:
    email = "dividend@example.com"
    register(client, email)
    verify_email(test_engine, email)
    portfolio, instrument = create_position(client, test_engine, "Dividend portfolio")
    action = create_action(client, instrument["id"], "dividend", amount="2")

    applied = apply_action(client, portfolio["id"], action["id"])
    assert applied.status_code == 200, applied.text
    assert applied.json()["affected_quantity"] == "10.00000000"
    assert applied.json()["quantity_adjustment"] == "0E-8"
    assert applied.json()["cash_adjustment"] == "20.00"

    summary = client.get(f"/api/v1/portfolios/{portfolio['id']}/summary").json()
    assert summary["cash_balance"] == "4020.00"
    assert summary["realized_pnl"] == "20.00"
    assert summary["positions"][0]["quantity"] == "10.00000000"


def test_corporate_actions_require_verified_email_and_are_user_scoped(
    client: TestClient, test_engine
) -> None:
    register(client, "owner@example.com")
    portfolio, instrument = create_position(client, test_engine, "Owner portfolio")

    assert client.get("/api/v1/corporate-actions").status_code == 403
    assert client.get("/api/v1/corporate-actions").json()["detail"]["code"] == "email_verification_required"

    verify_email(test_engine, "owner@example.com")
    action = create_action(client, instrument["id"], "split", ratio="2")
    assert client.get("/api/v1/corporate-actions").json()[0]["id"] == action["id"]

    other = TestClient(app)
    try:
        register(other, "other@example.com")
        verify_email(test_engine, "other@example.com")
        assert other.get("/api/v1/corporate-actions").json() == []
        other_portfolio_response = other.post(
            "/api/v1/portfolios",
            headers=csrf(other),
            json={"name": "Other portfolio", "base_currency": "USD"},
        )
        assert other_portfolio_response.status_code == 201
        attempted = apply_action(
            other,
            other_portfolio_response.json()["id"],
            action["id"],
        )
        assert attempted.status_code == 404
        hidden_applications = other.get(
            f"/api/v1/corporate-actions/portfolios/{portfolio['id']}/applications"
        )
        assert hidden_applications.status_code == 404
    finally:
        other.close()


def test_split_adjustment_is_backward_only_and_keeps_action_lineage(
    client: TestClient, test_engine
) -> None:
    user_id, instrument_id = seed_adjustment_instrument(client, test_engine)
    with Session(test_engine) as db:
        action = CorporateAction(
            user_id=UUID(user_id), instrument_id=UUID(instrument_id),
            action_type="split", ex_date=date(2026, 1, 10), ratio=Decimal("2"),
            amount=None, source="manual", source_reference="owner reference",
            created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )
        db.add(action)
        db.flush()
        raw = list(db.scalars(select(MarketBar).where(
            MarketBar.instrument_id == UUID(instrument_id)
        ).order_by(MarketBar.timestamp)))
        adjusted = adjusted_bars_for_backtest(
            db, UUID(user_id), db.get(Instrument, UUID(instrument_id)), raw,
            raw_price_basis_verified=True,
        )
        assert adjusted[0].close == Decimal("50")
        assert adjusted[0].volume == Decimal("20")
        assert adjusted[0].applied_action_ids == (action.id,)
        assert adjusted[1].close == Decimal("50")
        assert raw[0].close == Decimal("100")


def test_bonus_adjustment_uses_bonus_share_ratio(client: TestClient, test_engine) -> None:
    user_id, instrument_id = seed_adjustment_instrument(client, test_engine)
    with Session(test_engine) as db:
        action = CorporateAction(
            user_id=UUID(user_id), instrument_id=UUID(instrument_id),
            action_type="bonus", ex_date=date(2026, 1, 10), ratio=Decimal("1"),
            amount=None, source="manual", created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        )
        db.add(action)
        db.flush()
        raw = list(db.scalars(select(MarketBar).where(
            MarketBar.instrument_id == UUID(instrument_id)
        ).order_by(MarketBar.timestamp)))
        adjusted = adjusted_bars_for_backtest(
            db, UUID(user_id), db.get(Instrument, UUID(instrument_id)), raw,
            raw_price_basis_verified=True,
        )
        assert adjusted[0].close == Decimal("50")
        assert adjusted[0].volume == Decimal("20")
        assert adjusted[0].applied_action_ids == (action.id,)


def test_yfinance_conflict_keeps_both_rows_and_flags_them(
    client: TestClient, test_engine
) -> None:
    user_id, instrument_id = seed_adjustment_instrument(client, test_engine)
    with Session(test_engine) as db:
        db.add(CorporateAction(
            user_id=UUID(user_id), instrument_id=UUID(instrument_id),
            action_type="split", ex_date=date(2026, 1, 10), ratio=Decimal("3"),
            amount=None, source="manual", created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        ))
        db.commit()

        class Provider:
            def get_corporate_actions(self, ticker):
                assert ticker == "ALPHA.NS"
                return [{
                    "ex_date": date(2026, 1, 10), "action_type": "split",
                    "ratio": Decimal("2"), "amount": None,
                }]

        report = sync_yfinance_corporate_actions(db, UUID(user_id), provider=Provider())
        rows = db.scalars(select(CorporateAction).where(
            CorporateAction.instrument_id == UUID(instrument_id),
            CorporateAction.ex_date == date(2026, 1, 10),
        )).all()
        assert report["actions_inserted"] == 1
        assert len(rows) == 2
        assert {row.source for row in rows} == {"manual", "yfinance"}
        assert all(row.is_discrepant for row in rows)
        assert next(row for row in rows if row.source == "manual").verified_at is None
        assert next(row for row in rows if row.source == "yfinance").verified_at is not None

        repeated = sync_yfinance_corporate_actions(db, UUID(user_id), provider=Provider())
        assert repeated["actions_inserted"] == 0
        assert db.scalar(select(func.count(CorporateAction.id)).where(
            CorporateAction.instrument_id == UUID(instrument_id),
            CorporateAction.ex_date == date(2026, 1, 10),
        )) == 2


def test_unavailable_action_source_marks_instrument_unverified(
    client: TestClient, test_engine
) -> None:
    user_id, instrument_id = seed_adjustment_instrument(client, test_engine)
    with Session(test_engine) as db:
        class Provider:
            def get_corporate_actions(self, ticker):
                raise RuntimeError("provider unavailable")

        report = sync_yfinance_corporate_actions(db, UUID(user_id), provider=Provider())
        coverage = db.scalar(select(CorporateActionCoverage).where(
            CorporateActionCoverage.instrument_id == UUID(instrument_id)
        ))
        assert report["instruments_unverified"] == 1
        assert coverage is not None and coverage.status == "unverified"
        raw = list(db.scalars(select(MarketBar).where(
            MarketBar.instrument_id == UUID(instrument_id)
        ).order_by(MarketBar.timestamp)))
        instrument = db.get(Instrument, UUID(instrument_id))
        with pytest.raises(CorporateActionCoverageError, match="unverified"):
            adjusted_bars_for_backtest(db, UUID(user_id), instrument, raw)
