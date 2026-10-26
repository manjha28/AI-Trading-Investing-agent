from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.api.market_data import OptionChainResponse, require_verified_market_session
from app.db import Base, get_db
from app.main import app
from app.models import Instrument


def _instrument(session: Session) -> Instrument:
    instrument = Instrument(
        symbol="TESTIDX",
        display_name="Test Index",
        asset_class="index",
        venue="NSE",
        currency="INR",
        country="IN",
        timezone="Asia/Kolkata",
        is_synthetic=False,
        relink_required=False,
        created_at=datetime.now(timezone.utc),
    )
    session.add(instrument)
    session.commit()
    session.refresh(instrument)
    return instrument


def test_signal_endpoint_returns_no_trade_when_real_intraday_bars_are_missing(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        instrument = _instrument(session)
        expiry = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=5)
        fetched_at = datetime.now(timezone.utc)
        response = OptionChainResponse(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            expiry=expiry,
            underlying_price=Decimal("22000"),
            strikes=[],
            source="Dhan",
            fetched_at=fetched_at,
        )
        monkeypatch.setattr(market_data, "get_instrument_option_chain", lambda **_: response)

        def override_db():
            yield session

        monkeypatch.setitem(app.dependency_overrides, get_db, override_db)
        monkeypatch.setitem(
            app.dependency_overrides,
            require_verified_market_session,
            lambda: SimpleNamespace(user_id=uuid4()),
        )
        monkeypatch.setitem(
            app.dependency_overrides,
            market_data.get_market_data_provider,
            lambda: SimpleNamespace(providers=[], unavailable_providers=[]),
        )
        monkeypatch.setitem(
            app.dependency_overrides,
            market_data.get_current_user,
            lambda: SimpleNamespace(id=uuid4(), email_verified=True),
        )
        with TestClient(app) as client:
            result = client.get(
                f"/api/v1/market-data/instruments/{instrument.id}/option-chain/signal",
                params={"expiry": expiry.isoformat()},
            )
    engine.dispose()

    assert result.status_code == 200
    payload = result.json()
    assert payload["action"] == "NO_TRADE"
    assert "insufficient_5m_history" in payload["blockers"]
    assert payload["bar_source"] == "dhan"
    assert payload["chain_source"] == "Dhan"
    assert payload["last_bar_timestamp"] is None
    assert "No order was placed" in payload["disclaimer"]


def test_signal_endpoint_requires_all_open_position_fields(monkeypatch) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        instrument = _instrument(session)

        def override_db():
            yield session

        monkeypatch.setitem(app.dependency_overrides, get_db, override_db)
        monkeypatch.setitem(
            app.dependency_overrides,
            require_verified_market_session,
            lambda: SimpleNamespace(user_id=uuid4()),
        )
        monkeypatch.setitem(
            app.dependency_overrides,
            market_data.get_market_data_provider,
            lambda: SimpleNamespace(providers=[], unavailable_providers=[]),
        )
        monkeypatch.setitem(
            app.dependency_overrides,
            market_data.get_current_user,
            lambda: SimpleNamespace(id=uuid4(), email_verified=True),
        )
        try:
            with TestClient(app) as client:
                result = client.get(
                    f"/api/v1/market-data/instruments/{instrument.id}/option-chain/signal",
                    params={
                        "expiry": (date.today() + timedelta(days=5)).isoformat(),
                        "position_option_type": "call",
                    },
                )
        finally:
            app.dependency_overrides.clear()
    engine.dispose()

    assert result.status_code == 422
    assert "requires option type, strike, and entry premium together" in result.json()["detail"]
