from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.market_stream import get_event_publisher, get_latest_store
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.market_stream.events import (
    MarketBarEvent,
    event_from_json_bytes,
    event_identity,
)
from app.market_stream.persistence import apply_event, persist_event
from app.market_stream.poller import DhanBarPoller, parse_dhan_identifier
from app.market_stream.state import InMemoryLatestStateStore
from app.market_stream.transport import (
    InMemoryEventPublisher,
    MarketStreamError,
    consume_batch,
)
from app.models import Instrument, MarketBar, User

NOW = datetime.now(timezone.utc).replace(microsecond=0)
PROVIDER_ID = "1333|NSE_EQ"


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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "market_stream_enabled", True)


def make_event(
    *,
    instrument_id: UUID | None = None,
    bar_timestamp: datetime = NOW - timedelta(minutes=10),
    close: str = "101.5",
    interval: str = "5m",
    ingested_at: datetime = NOW,
) -> MarketBarEvent:
    instrument_id = instrument_id or uuid4()
    return MarketBarEvent(
        event_id=event_identity("dhan", instrument_id, interval, bar_timestamp),
        provider="dhan",
        instrument_id=instrument_id,
        provider_instrument_id=PROVIDER_ID,
        interval=interval,
        bar_timestamp=bar_timestamp,
        open=Decimal("100.25"),
        high=Decimal("102"),
        low=Decimal("99.5"),
        close=Decimal(close),
        volume=Decimal("1200.5"),
        ingested_at=ingested_at,
    )


def raw_bar(timestamp: datetime, *, high: str = "102", low: str = "99.5") -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        open=Decimal("100.25"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("101.5"),
        volume=Decimal("1200.5"),
    )


class FakeAdapter:
    calls: list[tuple] = []

    def __init__(self, tokens: dict[str, str], bars: list[SimpleNamespace] | None = None):
        self.tokens = tokens
        self.bars = bars or []

    def get_historical_bars(self, segment, security_id, interval, start, end):
        FakeAdapter.calls.append((segment, security_id, interval))
        return self.bars


def add_instrument(test_engine, symbol: str) -> UUID:
    with Session(test_engine) as db:
        instrument = Instrument(
            symbol=symbol, display_name=f"{symbol} Corp", asset_class="equity",
            venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", created_at=NOW,
        )
        db.add(instrument)
        db.commit()
        db.refresh(instrument)
        return instrument.id


def register(client: TestClient, test_engine, email: str, verified: bool) -> None:
    password = "correct-horse-battery"
    assert client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    ).status_code == 202
    assert client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    ).status_code == 200
    with Session(test_engine) as db:
        user = db.scalar(select(User).where(User.email == email))
        user.email_verified = verified
        db.commit()


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def test_event_rejects_invalid_ohlc_synthetic_and_unsupported_interval():
    base = make_event()
    with pytest.raises(ValueError, match="invalid_ohlc_relationship"):
        MarketBarEvent(**{**base.__dict__, "high": Decimal("99")})
    with pytest.raises(ValueError, match="synthetic_bars_not_allowed"):
        MarketBarEvent(**{**base.__dict__, "is_synthetic": True})
    with pytest.raises(ValueError, match="unsupported_interval"):
        MarketBarEvent(**{**base.__dict__, "interval": "3m"})
    with pytest.raises(ValueError, match="timestamps_must_be_timezone_aware"):
        MarketBarEvent(**{**base.__dict__, "bar_timestamp": datetime(2024, 1, 1)})


def test_event_json_round_trip_preserves_decimal_values_and_identity():
    event = make_event(close="101.12345678")
    restored = event_from_json_bytes(event.to_json_bytes())
    assert restored == event
    assert restored.close == Decimal("101.12345678")
    assert event.event_id == event_identity("dhan", event.instrument_id, "5m", event.bar_timestamp)


def test_dhan_identifier_parsing_accepts_only_verified_segments():
    assert parse_dhan_identifier("1333|NSE_EQ") == ("1333", "NSE_EQ")
    for bad in ["1333", "1333|NSE_FNO", "ABC|NSE_EQ", "|NSE_EQ"]:
        with pytest.raises(MarketStreamError, match="invalid_provider_instrument_id"):
            parse_dhan_identifier(bad)


def test_poller_publishes_valid_bars_in_order_and_rejects_bad_ohlc():
    instrument_id = uuid4()
    later = NOW - timedelta(minutes=5)
    earlier = NOW - timedelta(minutes=10)
    FakeAdapter.calls = []
    publisher = InMemoryEventPublisher()
    poller = DhanBarPoller(
        publisher,
        adapter_factory=lambda tokens: FakeAdapter(
            tokens, [raw_bar(later), raw_bar(earlier), raw_bar(earlier, high="90")]
        ),
        clock=lambda: NOW,
    )
    result = poller.poll(
        instrument_id=instrument_id, provider_instrument_id=PROVIDER_ID,
        tokens={"access_token": "x"}, interval="5m",
        start=NOW - timedelta(days=1), end=NOW,
    )
    assert (result.fetched, result.published, result.rejected) == (3, 2, 1)
    assert FakeAdapter.calls == [("NSE_EQ", "1333", "5m")]
    keys = {key for key, _ in publisher.messages}
    assert keys == {str(instrument_id)}
    decoded = [event_from_json_bytes(value) for _, value in publisher.messages]
    assert [item.bar_timestamp for item in decoded] == [earlier, later]
    assert all(item.is_synthetic is False and item.provider == "dhan" for item in decoded)


def test_poller_does_not_call_provider_for_malformed_identifier():
    FakeAdapter.calls = []
    poller = DhanBarPoller(InMemoryEventPublisher(), adapter_factory=lambda t: FakeAdapter(t))
    with pytest.raises(MarketStreamError, match="invalid_provider_instrument_id"):
        poller.poll(
            instrument_id=uuid4(), provider_instrument_id="bogus", tokens={},
            interval="5m", start=NOW - timedelta(days=1), end=NOW,
        )
    assert FakeAdapter.calls == []


def test_latest_state_only_moves_forward():
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    older = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=10))
    newer = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5), close="101.9")
    assert store.upsert_if_newer(newer) is True
    assert store.upsert_if_newer(older) is False
    assert store.upsert_if_newer(newer) is False
    assert store.get_many([instrument_id])[str(instrument_id)]["close"] == "101.9"


def test_older_event_arriving_after_newer_is_rejected_and_state_unchanged():
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    newer = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5), close="101.9")
    older = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=10), close="99.6")
    assert store.upsert_if_newer(newer) is True
    assert store.upsert_if_newer(older) is False
    stored = store.get_many([instrument_id])[str(instrument_id)]
    assert stored["event_id"] == str(newer.event_id)
    assert stored["close"] == "101.9"


def test_newer_event_replaces_older_event_entirely():
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    older = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=10), close="99.6")
    newer = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5), close="101.9")
    assert store.upsert_if_newer(older) is True
    assert store.upsert_if_newer(newer) is True
    stored = store.get_many([instrument_id])[str(instrument_id)]
    assert stored["event_id"] == str(newer.event_id)
    assert stored["bar_timestamp"] == newer.to_dict()["bar_timestamp"]
    assert stored["close"] == "101.9"


def test_equal_timestamp_keeps_first_writer_even_with_different_payload():
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    first = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5), close="101.0")
    second = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5), close="101.8")
    assert store.upsert_if_newer(first) is True
    assert store.upsert_if_newer(second) is False
    assert store.get_many([instrument_id])[str(instrument_id)]["close"] == "101.0"


def test_equal_instant_in_different_offsets_is_treated_as_equal():
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    utc_stamp = datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc)
    ist_stamp = datetime(2026, 11, 2, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert store.upsert_if_newer(make_event(instrument_id=instrument_id, bar_timestamp=utc_stamp, close="101.0")) is True
    assert store.upsert_if_newer(make_event(instrument_id=instrument_id, bar_timestamp=ist_stamp, close="101.8")) is False
    assert store.get_many([instrument_id])[str(instrument_id)]["close"] == "101.0"


def test_sub_second_difference_collapses_to_equal_at_second_precision():
    # Stored timestamps use second precision (TIMESTAMP_FORMAT). Bar timestamps are
    # candle boundaries, so this is documented behaviour: the first writer wins.
    store = InMemoryLatestStateStore()
    instrument_id = uuid4()
    base = NOW - timedelta(minutes=5)
    first = make_event(instrument_id=instrument_id, bar_timestamp=base.replace(microsecond=100000), close="101.0")
    second = make_event(instrument_id=instrument_id, bar_timestamp=base.replace(microsecond=900000), close="102.0")
    assert store.upsert_if_newer(first) is True
    assert store.upsert_if_newer(second) is False


def test_missing_or_invalid_timestamps_never_reach_the_store():
    instrument_id = uuid4()
    naive = datetime(2026, 11, 2, 4, 30)
    with pytest.raises(ValueError, match="timestamps_must_be_timezone_aware"):
        make_event(instrument_id=instrument_id, bar_timestamp=naive)
    with pytest.raises(ValueError, match="timestamps_must_be_timezone_aware"):
        make_event(instrument_id=instrument_id, ingested_at=naive)
    with pytest.raises(TypeError):
        MarketBarEvent(
            event_id=uuid4(),
            provider="dhan",
            instrument_id=instrument_id,
            provider_instrument_id=PROVIDER_ID,
            interval="5m",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
            ingested_at=NOW,
        )


class _RecordingScript:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, keys, args):
        self.calls.append({"keys": keys, "args": args})
        return self.result


class _RecordingValkeyClient:
    def __init__(self, result: int) -> None:
        self.script = _RecordingScript(result)

    def register_script(self, _source: str) -> _RecordingScript:
        return self.script


def test_valkey_store_sends_formatted_bar_timestamp_and_maps_script_result():
    # Wiring test only: the Lua compare-and-set itself needs a live Valkey server
    # (see test_valkey_latest_state_is_newer_only_under_lua_compare_and_set).
    from app.market_stream.state import LATEST_KEY_PREFIX, ValkeyLatestStateStore

    instrument_id = uuid4()
    event = make_event(instrument_id=instrument_id, bar_timestamp=NOW - timedelta(minutes=5))
    rejecting = _RecordingValkeyClient(result=0)
    assert ValkeyLatestStateStore(rejecting).upsert_if_newer(event) is False
    call = rejecting.script.calls[0]
    assert call["keys"] == [f"{LATEST_KEY_PREFIX}{instrument_id}"]
    assert call["args"][1] == event.to_dict()["bar_timestamp"]

    accepting = _RecordingValkeyClient(result=1)
    assert ValkeyLatestStateStore(accepting).upsert_if_newer(event) is True


def test_persistence_is_idempotent_and_apply_event_updates_state(test_engine):
    instrument_id = add_instrument(test_engine, "IDEMP")
    event = make_event(instrument_id=instrument_id)
    store = InMemoryLatestStateStore()
    with Session(test_engine) as db:
        assert apply_event(db, store, event) == {"inserted": True, "latest_updated": True}
        assert persist_event(db, event) is False
        assert apply_event(db, store, event) == {"inserted": False, "latest_updated": False}
        stored = db.scalar(select(MarketBar).where(MarketBar.instrument_id == instrument_id))
        assert stored.source == "dhan"
        assert stored.is_synthetic is False
        assert stored.close == Decimal("101.5")
        assert db.scalar(select(func.count()).select_from(MarketBar)) == 1


def test_consume_batch_commits_only_after_successful_handle():
    class Message:
        def __init__(self, value: bytes):
            self._value = value
            self.committed = False

        def error(self):
            return None

        def value(self):
            return self._value

    class Consumer:
        def __init__(self, messages):
            self.messages = messages
            self.commits = []

        def consume(self, num_messages, timeout):
            return self.messages

        def commit(self, message, asynchronous):
            self.commits.append(message)

    good = Message(make_event().to_json_bytes())
    consumer = Consumer([good])
    handled = []
    assert consume_batch(consumer, handled.append) == 1
    assert consumer.commits == [good] and len(handled) == 1

    bad = Message(make_event().to_json_bytes())
    failing = Consumer([bad])

    def boom(_event):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        consume_batch(failing, boom)
    assert failing.commits == []


def test_snapshot_is_disabled_by_default(client, test_engine):
    register(client, test_engine, "off@example.com", verified=True)
    response = client.get(f"/api/v1/market-stream/latest?instrument_ids={uuid4()}")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "market_stream_disabled"


def test_snapshot_requires_authentication(client, enabled):
    response = client.get(f"/api/v1/market-stream/latest?instrument_ids={uuid4()}")
    assert response.status_code == 401


def test_snapshot_requires_verified_email(client, test_engine, enabled):
    register(client, test_engine, "unverified@example.com", verified=False)
    response = client.get(f"/api/v1/market-stream/latest?instrument_ids={uuid4()}")
    assert response.status_code == 403


def test_snapshot_reports_freshness_and_missing_instruments(client, test_engine, enabled):
    register(client, test_engine, "snap@example.com", verified=True)
    fresh_id = uuid4()
    stale_id = uuid4()
    missing_id = uuid4()
    store = InMemoryLatestStateStore()
    store.upsert_if_newer(make_event(instrument_id=fresh_id, bar_timestamp=NOW - timedelta(minutes=10)))
    store.upsert_if_newer(
        make_event(instrument_id=stale_id, bar_timestamp=NOW - timedelta(days=3), interval="1h")
    )
    app.dependency_overrides[get_latest_store] = lambda: store
    try:
        response = client.get(
            "/api/v1/market-stream/latest",
            params={"instrument_ids": f"{fresh_id},{stale_id},{missing_id}"},
        )
    finally:
        app.dependency_overrides.pop(get_latest_store, None)
    assert response.status_code == 200
    body = response.json()
    by_id = {item["instrument_id"]: item for item in body["bars"]}
    assert by_id[str(fresh_id)]["freshness"] == "fresh"
    assert by_id[str(fresh_id)]["is_synthetic"] is False
    assert by_id[str(fresh_id)]["close"] == "101.5"
    assert by_id[str(stale_id)]["freshness"] == "stale"
    assert body["missing_instrument_ids"] == [str(missing_id)]
    assert body["disclaimer"] == "Decision support, not investment advice."


def test_snapshot_rejects_malformed_or_oversized_requests(client, test_engine, enabled):
    register(client, test_engine, "bad@example.com", verified=True)
    assert client.get("/api/v1/market-stream/latest?instrument_ids=not-a-uuid").status_code == 422
    too_many = ",".join(str(uuid4()) for _ in range(51))
    assert client.get("/api/v1/market-stream/latest", params={"instrument_ids": too_many}).status_code == 422


def test_poll_requires_csrf_and_verified_dhan_identifier(client, test_engine, enabled):
    register(client, test_engine, "poll@example.com", verified=True)
    instrument_id = add_instrument(test_engine, "POLL")
    body = {"instrument_id": str(instrument_id), "interval": "5m"}
    app.dependency_overrides[get_event_publisher] = lambda: InMemoryEventPublisher()
    try:
        assert client.post("/api/v1/market-stream/poll", json=body).status_code == 403
        response = client.post("/api/v1/market-stream/poll", json=body, headers=csrf(client))
    finally:
        app.dependency_overrides.pop(get_event_publisher, None)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "dhan_identifier_unverified"


def test_poll_rejects_unknown_instrument_and_bad_interval(client, test_engine, enabled):
    register(client, test_engine, "unknown@example.com", verified=True)
    app.dependency_overrides[get_event_publisher] = lambda: InMemoryEventPublisher()
    try:
        unknown = client.post(
            "/api/v1/market-stream/poll",
            json={"instrument_id": str(uuid4()), "interval": "5m"},
            headers=csrf(client),
        )
        invalid = client.post(
            "/api/v1/market-stream/poll",
            json={"instrument_id": str(uuid4()), "interval": "3m"},
            headers=csrf(client),
        )
    finally:
        app.dependency_overrides.pop(get_event_publisher, None)
    assert unknown.status_code == 404
    assert invalid.status_code == 422


class _UnreachableStore:
    def get_many(self, _ids):
        import redis
        raise redis.ConnectionError("valkey down")

    def upsert_if_newer(self, _event):
        raise AssertionError("not used")


def _get_latest(client, store, ids):
    app.dependency_overrides[get_latest_store] = lambda: store
    try:
        return client.get("/api/v1/market-stream/latest", params={"instrument_ids": ",".join(map(str, ids))})
    finally:
        app.dependency_overrides.pop(get_latest_store, None)


def test_snapshot_store_outage_is_a_structured_503_without_any_data(client, test_engine, enabled):
    register(client, test_engine, "outage@example.com", verified=True)
    response = _get_latest(client, _UnreachableStore(), [uuid4()])
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "latest_state_unavailable"
    assert detail["status"] == "unavailable" and detail["bars"] == []
    assert response.headers["retry-after"] == "5"


def test_disabled_stream_503_is_structured_and_carries_no_bars(client, test_engine):
    register(client, test_engine, "off2@example.com", verified=True)
    detail = client.get(f"/api/v1/market-stream/latest?instrument_ids={uuid4()}").json()["detail"]
    assert detail["code"] == "market_stream_disabled" and detail["status"] == "unavailable" and detail["bars"] == []


def test_snapshot_without_any_tick_reports_no_data_and_never_fabricates(client, test_engine, enabled):
    register(client, test_engine, "empty@example.com", verified=True)
    missing = uuid4()
    body = _get_latest(client, InMemoryLatestStateStore(), [missing]).json()
    assert body["status"] == "no_data" and body["bars"] == [] and body["missing_instrument_ids"] == [str(missing)]


def test_snapshot_status_is_stale_when_every_stored_bar_is_stale(client, test_engine, enabled):
    register(client, test_engine, "stale@example.com", verified=True)
    old_id = uuid4()
    store = InMemoryLatestStateStore()
    store.upsert_if_newer(make_event(instrument_id=old_id, bar_timestamp=NOW - timedelta(days=3), interval="1h"))
    body = _get_latest(client, store, [old_id]).json()
    assert body["status"] == "stale" and body["bars"][0]["freshness"] == "stale"


def test_corrupt_stored_tick_is_reported_unreadable_not_a_500(client, test_engine, enabled):
    register(client, test_engine, "corrupt@example.com", verified=True)
    bad = uuid4()

    class CorruptStore(InMemoryLatestStateStore):
        def get_many(self, ids):
            return {str(i): {"interval": "1m", "bar_timestamp": "not-a-timestamp"} for i in ids}

    body = _get_latest(client, CorruptStore(), [bad]).json()
    assert body["unreadable_instrument_ids"] == [str(bad)] and body["bars"] == [] and body["status"] == "no_data"
