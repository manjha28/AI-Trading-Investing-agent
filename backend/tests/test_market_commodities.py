"""MCX commodity section of the Market watch: REST contract list + live stream. Rows/ticks are TEST DATA."""
import asyncio
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api import market_data
from app.broker_adapters import HistoricalBar
from app.db import Base, get_db
from app.main import app
from app.models import User, UserSession
from app.models.derivatives import DerivativeContract

NOW = datetime.now(timezone.utc)
TODAY = NOW.astimezone(market_data.ZoneInfo("Asia/Kolkata")).date()


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def client(engine) -> Iterator[TestClient]:
    def override_get_db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client: TestClient, engine, *, two_factor: bool = False) -> None:
    creds = {"email": "mcx@example.com", "password": "correct-horse-battery"}
    assert client.post("/api/v1/auth/register", json=creds).status_code == 202
    assert client.post("/api/v1/auth/login", json=creds).status_code == 200
    with Session(engine) as db:
        user = db.scalar(select(User))
        user.email_verified = True
        if two_factor:  # the live feed needs a 2FA-verified session
            user.two_factor_enabled = True
            db.scalar(select(UserSession)).two_factor_verified = True
        db.commit()


def contract(db: Session, underlying: str, security_id: str, expiry: date, *, exchange="MCX", kind="FUTCOM", status="active") -> None:
    db.add(DerivativeContract(
        exchange=exchange, segment="MCX_COMM" if exchange == "MCX" else "NSE_FNO", master_segment="M", instrument_type=kind,
        security_id=security_id, underlying_symbol=underlying, trading_symbol=underlying, display_name=f"{underlying} future",
        expiry=expiry, lot_size=Decimal("1"), tick_size=Decimal("1"), status=status, valid_from=NOW, first_seen_at=NOW,
        last_seen_at=NOW, spec_hash="x" * 40, master_version="test", ingested_at=NOW,
    ))


def test_commodities_list_the_nearest_unexpired_contract_per_underlying(client, engine):
    login(client, engine)
    with Session(engine) as db:
        contract(db, "CRUDEOIL", "1", TODAY - timedelta(days=1))          # expired but still marked active in the master
        contract(db, "CRUDEOIL", "2", TODAY + timedelta(days=28))         # the real front month
        contract(db, "CRUDEOIL", "3", TODAY + timedelta(days=58))
        contract(db, "GOLD", "4", TODAY + timedelta(days=10))
        contract(db, "SILVER", "5", TODAY + timedelta(days=9), status="removed")  # not tradable
        contract(db, "RELIANCE", "6", TODAY + timedelta(days=9), exchange="NSE", kind="FUTSTK")  # not a commodity
        db.commit()
    body = client.get("/api/v1/market-data/commodities").json()
    assert [(r["symbol"], r["security_id"]) for r in body["instruments"]] == [("CRUDEOIL", "2"), ("GOLD", "4")]
    crude = body["instruments"][0]
    assert crude["key"] == "mcx:2" and crude["days_to_expiry"] == 28 and crude["expiry"] == (TODAY + timedelta(days=28)).isoformat()
    assert body["count"] == 2 and body["exchange"] == "MCX" and body["hours_ist"] == "09:00–23:30 Mon–Fri"
    assert isinstance(body["session_open"], bool)
    assert client.get("/api/v1/market-data/commodities").status_code == 200


def test_commodities_require_a_verified_session(client):
    assert client.get("/api/v1/market-data/commodities").status_code == 401


def test_mcx_session_hint_follows_the_exchange_hours():
    ist = market_data.ZoneInfo("Asia/Kolkata")
    at = lambda d, h, m: datetime(2026, 11, d, h, m, tzinfo=ist)  # noqa: E731
    assert market_data._mcx_session_open(at(20, 21, 0))          # Fri evening: MCX is open
    assert market_data._mcx_session_open(at(20, 23, 29))
    assert not market_data._mcx_session_open(at(20, 23, 30))     # closed from 23:30
    assert not market_data._mcx_session_open(at(20, 8, 59))      # opens 09:00
    assert not market_data._mcx_session_open(at(21, 12, 0))      # Saturday


def _bar(close: str, volume: str = "100") -> HistoricalBar:
    return HistoricalBar(timestamp=NOW, open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close), volume=Decimal(volume))


def _scripted_feed(items, record):
    """TEST DATA: stands in for DhanAdapter.stream_watchlist_market_quotes."""
    async def stream(self, access_token, tokens, subscriptions, updates, status_callback):
        record["subscriptions"] = dict(subscriptions)
        await status_callback("packet_received")
        for item in items:
            yield item
        await asyncio.sleep(0.05)
    return stream


def test_commodity_stream_seeds_from_a_snapshot_then_streams_ticks_with_python_computed_change(client, engine, monkeypatch):
    from app.broker_adapters import DhanAdapter

    login(client, engine, two_factor=True)
    with Session(engine) as db:
        contract(db, "GOLD", "111", TODAY + timedelta(days=10))
        contract(db, "CRUDEOIL", "222", TODAY + timedelta(days=20))
        db.commit()
    seen: dict = {}
    snapshot_gold = HistoricalBar(timestamp=NOW, open=Decimal("100500"), high=Decimal("101000"), low=Decimal("99000"),
                                  close=Decimal("100800"), volume=Decimal("5"), session_close=Decimal("100000"))
    snapshot_crude = HistoricalBar(timestamp=NOW, open=Decimal("6500"), high=Decimal("6600"), low=Decimal("6400"),
                                   close=Decimal("6420"), volume=Decimal("7"))  # the reference close is unavailable
    monkeypatch.setattr(DhanAdapter, "get_market_quotes_batch", lambda self, tok, toks, instruments: {"MCX_COMM": {"111": snapshot_gold, "222": snapshot_crude}})
    items = [
        ("mcx:111", _bar("101250.50")),        # a live tick after the snapshot
        ("mcx:999", _bar("1")),               # not a subscribed contract: ignored
    ]
    monkeypatch.setattr(DhanAdapter, "stream_watchlist_market_quotes", _scripted_feed(items, seen))
    monkeypatch.setattr(market_data, "_default_market_data_source", lambda *_a: "dhan")
    monkeypatch.setattr(market_data, "_fresh_dhan_tokens_worker", lambda *_a: {"access_token": "t", "client_id": "c"})
    with client.websocket_connect("/api/v1/market-data/commodities/stream", headers={"origin": "http://localhost:5173"}) as socket:
        messages = [socket.receive_json() for _ in range(5)]
    assert seen["subscriptions"] == {"mcx:111": ("MCX_COMM", "111"), "mcx:222": ("MCX_COMM", "222")}
    ticks = [m for m in messages if m["type"] == "tick"]
    assert [t["symbol"] for t in ticks] == ["GOLD", "CRUDEOIL", "GOLD"]  # snapshot rows first, then the live tick
    snap_gold, snap_crude, live_gold = ticks
    assert snap_gold["ltp"] == "100800" and snap_gold["previous_close"] == "100000" and snap_gold["change"] == "800" and snap_gold["change_pct"] == "0.80"
    assert snap_crude["change"] is None and snap_crude["change_pct"] is None and snap_crude["previous_close"] is None  # unknown, not zero
    assert live_gold["ltp"] == "101250.50" and live_gold["change"] == "1250.50" and live_gold["change_pct"] == "1.25"
    assert "received_at" in live_gold and "market_timestamp" not in live_gold
    assert any(m["type"] == "status" for m in messages)


def test_commodity_stream_refuses_unverified_sessions_and_other_data_sources(client, engine, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    login(client, engine)  # email verified but no 2FA-verified session
    with client.websocket_connect("/api/v1/market-data/commodities/stream", headers={"origin": "http://localhost:5173"}) as socket:
        refused = socket.receive_json()
        assert refused["status"] == "unavailable" and "Verify this session" in refused["message"]
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4403
