"""Dhan live-feed hub, Dhan quote subscription restore, and Dhan ledger reconciliation."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from websockets.exceptions import ConnectionClosedError

from app import broker_adapters
from app.api import brokers as brokers_api
from app.api import market_data as md
from app.broker_adapters import BrokerAdapterError, DhanAdapter
from app.db import Base
from app.models import DhanLedgerDataQualityException, DhanLedgerMirror

NOW = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc)


# ───────────── stream health classification ─────────────
def test_stream_state_is_explicit_and_never_live_when_disconnected_or_stale():
    fresh, old = NOW - timedelta(seconds=3), NOW - timedelta(seconds=300)
    c = md.classify_stream_state
    assert c("active", fresh, 30, NOW) == "connected"
    assert c("active", old, 30, NOW) == "stale"
    assert c("active", None, 30, NOW) == "reconnecting"
    assert c("reconnecting", fresh, 30, NOW) == "reconnecting"
    assert c("connecting", None, 30, NOW) == "reconnecting"
    assert c("failed", fresh, 30, NOW) == "unavailable"
    assert c("idle", fresh, 30, NOW) == "unavailable"


def test_hub_health_exposes_stream_state_and_is_live_only_when_connected():
    hub = md.DhanLiveFeedHub()
    health = hub.health("depth")
    assert health["stream_state"] == "unavailable" and health["is_live"] is False
    hub._feed_health["depth"].update(connection_state="active", last_successful_packet_at=datetime.now(timezone.utc))
    assert hub.health("depth")["is_live"] is True
    hub._feed_health["depth"]["last_successful_packet_at"] = datetime.now(timezone.utc) - timedelta(hours=1)
    health = hub.health("depth")
    assert health["stream_state"] == "stale" and health["is_live"] is False


# ───────────── depth feed: disconnect, reconnect, restore, shutdown ─────────────
@pytest.fixture
def hub(monkeypatch):
    hub = md.DhanLiveFeedHub()
    monkeypatch.setattr(hub, "_open_feed_gaps", lambda *a, **k: None)
    monkeypatch.setattr(hub, "_close_feed_gaps", lambda *a, **k: None)
    monkeypatch.setattr(md, "_dhan_backoff_seconds", lambda _base, _retry=None: 0.0)
    return hub


SUBS = {"inst-1": ("NSE_EQ", "1333")}


def test_depth_feed_reconnects_after_transient_close_and_restores_subscriptions(hub):
    calls: list[dict] = []
    running = 0
    max_running = 0

    class Adapter:
        async def stream_watchlist_market_depth(self, _t, _tokens, initial, updates, status_callback):
            nonlocal running, max_running
            running += 1
            max_running = max(max_running, running)
            calls.append(dict(initial))
            try:
                await status_callback("active")
                if len(calls) == 1:
                    raise BrokerAdapterError("Dhan depth feed closed code=1006 reason=x", status_code=1006)
                yield "inst-1", {"timestamp": NOW, "bids": [1], "asks": [2]}
                await asyncio.sleep(3600)
            finally:
                running -= 1

    hub._adapter = Adapter()

    async def scenario():
        queue = await hub.join("depth", "g1", dict(SUBS), "token", {"client_id": "c"})
        message = await asyncio.wait_for(_next_depth(queue), 2)
        states = hub.health("depth")["stream_state"]
        await hub.stop()
        return message, states

    message, state = asyncio.run(scenario())
    assert message["type"] == "depth"
    assert len(calls) == 2 and calls[0] == calls[1] == SUBS  # subscriptions restored
    assert max_running == 1  # never two concurrent upstream connections
    assert state == "connected"
    assert hub._tasks["depth"] is None  # shutdown cleaned the task


async def _next_depth(queue):
    while True:
        message = await queue.get()
        if message.get("type") == "depth":
            return message


def test_depth_terminal_rejection_is_not_retried_and_reports_error(hub):
    attempts = 0

    class Adapter:
        async def stream_watchlist_market_depth(self, *_a):
            nonlocal attempts
            attempts += 1
            raise BrokerAdapterError("handshake rejected HTTP 403", status_code=403)
            yield  # pragma: no cover

    hub._adapter = Adapter()

    async def scenario():
        queue = await hub.join("depth", "g1", dict(SUBS), "token", {"client_id": "c"})
        errors = []
        while not errors:
            message = await asyncio.wait_for(queue.get(), 2)
            if message.get("type") == "error":
                errors.append(message)
        await asyncio.sleep(0.05)
        return errors[0]

    error = asyncio.run(scenario())
    assert attempts == 1 and error["status_code"] == 403


def test_duplicate_joins_share_one_upstream_task(hub):
    started = 0

    class Adapter:
        async def stream_watchlist_market_depth(self, *_a):
            nonlocal started
            started += 1
            await asyncio.sleep(3600)
            yield  # pragma: no cover

    hub._adapter = Adapter()

    async def scenario():
        await hub.join("depth", "g1", dict(SUBS), "token", {"client_id": "c"})
        await hub.join("depth", "g2", {"inst-2": ("NSE_EQ", "2885")}, "token", {"client_id": "c"})
        await asyncio.sleep(0.05)
        task = hub._tasks["depth"]
        await hub.stop()
        return task

    asyncio.run(scenario())
    assert started == 1


def test_depth_retryability():
    f = md._depth_failure_is_retryable
    assert f(BrokerAdapterError("x")) and f(BrokerAdapterError("x", status_code=1006))
    assert f(BrokerAdapterError("x", status_code=429)) and f(BrokerAdapterError("x", status_code=503))
    assert not f(BrokerAdapterError("x", status_code=401)) and not f(BrokerAdapterError("x", status_code=4403))


# ───────────── ticker: subscription update lost on disconnect is restored ─────────────
def test_ticker_restores_subscription_update_consumed_before_the_socket_dropped(monkeypatch):
    sent: list[list[dict]] = []
    connections = 0

    class Socket:
        close_code = None
        close_reason = ""
        response = type("R", (), {"status_code": 101, "reason_phrase": "ok"})()

        def __init__(self, index):
            self.index = index

        async def send(self, payload):
            import json
            body = json.loads(payload)
            if self.index == 0 and body["RequestCode"] == 17 and len(sent) >= 1:
                raise ConnectionClosedError(None, None)  # drop while applying the update
            sent.append([self.index, body["RequestCode"], body["InstrumentList"]])

        async def recv(self):
            await asyncio.sleep(3600)

    class Ctx:
        def __init__(self, index):
            self.socket = Socket(index)

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, *_a):
            return None

    def fake_connection(*_a, **_k):
        nonlocal connections
        connections += 1
        return Ctx(connections - 1)

    monkeypatch.setattr(broker_adapters, "_single_dhan_live_connection", fake_connection)
    monkeypatch.setattr(broker_adapters, "_dhan_backoff_seconds", lambda *_a, **_k: 0.0)
    adapter = DhanAdapter()

    async def scenario():
        updates: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def status(*_a):
            return None

        agen = adapter.stream_watchlist_market_quotes(
            "token", {"client_id": "client"}, {"a": ("NSE_EQ", "1333")}, updates, status
        )
        task = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0.05)
        await updates.put({"a": ("NSE_EQ", "1333"), "b": ("NSE_EQ", "2885")})
        for _ in range(100):
            if connections >= 2 and any(item[0] == 1 for item in sent):
                break
            await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agen.aclose()

    asyncio.run(scenario())
    restored = [item for item in sent if item[0] == 1 and item[1] == 17]
    assert connections >= 2
    ids = {entry["SecurityId"] for item in restored for entry in item[2]}
    assert ids == {"1333", "2885"}  # the update that was consumed before the drop is restored


# ───────────── ledger reconciliation ─────────────
def ledger_row(**over):
    row = {"vouchernumber": None, "voucherdate": "2026-10-01", "narration": "Funds added", "voucherdesc": "Receipt",
           "exchange": "NSE", "debit": "0", "credit": "5000.00", "runbal": "5000.00"}
    row.update(over)
    return row


UID = uuid4()


def test_keyed_ledger_identity_is_unchanged_and_provider_id_is_kept():
    [entry] = brokers_api._normalize_dhan_ledger([ledger_row(vouchernumber="V1")], UID, NOW)
    assert entry.provider_entry_id == "V1"
    again = brokers_api._normalize_dhan_ledger([ledger_row(vouchernumber="V1")], UID, NOW)[0]
    assert entry.entry_id == again.entry_id


def test_missing_id_uses_stable_content_key_and_never_invents_a_provider_id():
    a = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW)[0]
    b = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW)[0]
    assert a.provider_entry_id is None and a.entry_id == b.entry_id  # stable across syncs
    other = brokers_api._normalize_dhan_ledger([ledger_row(runbal="5100.00", credit="5100.00")], UID, NOW)[0]
    assert other.entry_id != a.entry_id
    assert brokers_api._normalize_dhan_ledger([ledger_row(vouchernumber="V1")], UID, NOW)[0].entry_id != a.entry_id


def test_identical_unkeyed_lines_get_distinct_keys_by_occurrence():
    first = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW, ordinal=0)[0]
    second = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW, ordinal=1)[0]
    assert first.entry_id != second.entry_id


def test_unkeyed_row_without_date_amount_or_balance_is_not_mirrored():
    for broken in ({"runbal": None}, {"voucherdate": None}, {"debit": None, "credit": None}):
        row = ledger_row(**broken)
        assert not brokers_api._ledger_content_identifiable(row)
        with pytest.raises(BrokerAdapterError):
            brokers_api._normalize_dhan_ledger([row], UID, NOW)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_resync_is_idempotent_and_adopts_content_row_when_provider_id_appears(db):
    row = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW)[0]
    assert brokers_api._upsert_dhan_ledger_entry(db, row) is True
    db.flush()
    again = brokers_api._normalize_dhan_ledger([ledger_row()], UID, NOW)[0]
    assert brokers_api._upsert_dhan_ledger_entry(db, again) is False  # no duplicate
    db.flush()
    keyed = brokers_api._normalize_dhan_ledger([ledger_row(vouchernumber="V9")], UID, NOW)[0]
    assert brokers_api._upsert_dhan_ledger_entry(db, keyed) is False  # adopted, not duplicated
    db.flush()
    rows = db.scalars(select(DhanLedgerMirror)).all()
    assert len(rows) == 1 and rows[0].provider_entry_id == "V9"


def test_quarantine_keeps_identical_rows_distinct_and_refreshes_counts(db):
    broken = ledger_row(runbal=None)
    recorded = brokers_api._record_dhan_ledger_exceptions(db, UID, [broken, dict(broken)], NOW)
    db.flush()
    assert len(recorded) == 2 and db.scalar(select(func.count()).select_from(DhanLedgerDataQualityException)) == 2
    later = NOW + timedelta(days=1)
    assert brokers_api._record_dhan_ledger_exceptions(db, UID, [broken, dict(broken)], later) == []
    db.flush()
    stored = db.scalars(select(DhanLedgerDataQualityException)).all()
    assert all(item.occurrence_count == 2 and item.last_seen_at.replace(tzinfo=None) == later.replace(tzinfo=None) for item in stored)


# ───────────── Dhan depth upstream: subscription payload, parsing, forwarding ─────────────
def _depth_packet(code: int, security_id: int, base: float, filled: int = 20) -> bytes:
    import struct
    body = b"".join(
        struct.pack("<dII", base + i if i < filled else 0.0, 100 * (i + 1) if i < filled else 0, i + 1 if i < filled else 0)
        for i in range(20)
    )
    return struct.pack("<HBBiI", 332, code, 1, security_id, 7) + body


def test_depth_upstream_subscribes_with_bare_id_parses_bundled_packets_and_marks_subscribed(monkeypatch):
    import json
    sent: list[dict] = []
    statuses: list[str] = []

    class Socket:
        close_code = None
        close_reason = ""
        response = type("R", (), {"status_code": 101, "reason_phrase": "ok"})()

        def __init__(self):
            self.frames = [
                "provider notice",  # a text frame must be logged and ignored, not crash the feed
                _depth_packet(41, 1333, 1500.0, filled=3) + _depth_packet(51, 1333, 1501.0, filled=3),
            ]

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            if self.frames:
                return self.frames.pop(0)
            await asyncio.sleep(3600)

    class Ctx:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(broker_adapters, "_single_dhan_live_connection", lambda *_a, **_k: Ctx())
    adapter = DhanAdapter()

    async def scenario():
        async def status(value, *_a):
            statuses.append(value)

        agen = adapter.stream_watchlist_market_depth(
            "token-value", {"client_id": "client-value"},
            {"inst-1": ("NSE_EQ", "1333|NSE_EQ")},  # canonical stored form must still subscribe the bare id
            asyncio.Queue(), status,
        )
        try:
            return await asyncio.wait_for(agen.__anext__(), 2)
        finally:
            await agen.aclose()

    instrument_id, depth = asyncio.run(scenario())
    assert sent[0] == {"RequestCode": 23, "InstrumentCount": 1,
                       "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "1333"}]}
    assert statuses[0] == "subscribed"  # subscribed is reported before any packet, and it is not "active"
    assert instrument_id == "inst-1"
    assert len(depth["bids"]) == 20 and len(depth["asks"]) == 20
    assert depth["bids"][0] == {"price": Decimal("1500.0"), "quantity": 100, "orders": 1}
    assert depth["asks"][2]["price"] == Decimal("1503.0") and depth["asks"][3]["quantity"] == 0
    assert depth["timestamp"].tzinfo is not None


def test_depth_packet_for_another_security_is_rejected_not_forwarded():
    with pytest.raises(BrokerAdapterError):
        DhanAdapter.decode_market_depth_packet(_depth_packet(41, 2885, 10.0), "NSE_EQ", "1333")


def test_clean_depth_levels_drops_empty_slots_and_keeps_decimal_prices():
    levels = [{"price": Decimal("10.5"), "quantity": 5, "orders": 1}, {"price": Decimal("0"), "quantity": 0, "orders": 0},
              {"price": Decimal("0"), "quantity": 7, "orders": 1}]
    assert md.clean_depth_levels(levels) == [{"price": "10.5", "quantity": 5, "orders": 1}]


def test_hub_publishes_subscribed_without_going_live(hub):
    async def scenario():
        hub._groups["depth"]["g"] = ({"inst-1": ("NSE_EQ", "1333")}, asyncio.Queue())

        async def cb_runner():
            pass

        class Adapter:
            async def stream_watchlist_market_depth(self, _t, _k, _i, _u, status_callback):
                await status_callback("subscribed")
                await asyncio.sleep(0.05)
                yield  # pragma: no cover
                await asyncio.sleep(3600)

        hub._adapter = Adapter()
        queue = hub._groups["depth"]["g"][1]
        task = asyncio.create_task(hub._run_feed("depth", "t", {"client_id": "c"}, dict(SUBS), asyncio.Queue(maxsize=1)))
        message = await asyncio.wait_for(queue.get(), 2)
        health = hub.health("depth")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return message, health

    message, health = asyncio.run(scenario())
    assert message["status"] == "subscribed"
    assert health["connection_state"] == "subscribed" and health["is_live"] is False


def test_depth_reconnects_are_bounded_then_the_real_reason_is_surfaced(hub, monkeypatch):
    monkeypatch.setattr(md, "DEPTH_MAX_RECONNECT_ATTEMPTS", 2)
    attempts = 0

    class Adapter:
        async def stream_watchlist_market_depth(self, *_a):
            nonlocal attempts
            attempts += 1
            raise BrokerAdapterError("Dhan depth feed closed code=1006 reason=ConnectionClosedError", status_code=1006)
            yield  # pragma: no cover

    hub._adapter = Adapter()

    async def scenario():
        queue = await hub.join("depth", "g1", dict(SUBS), "token", {"client_id": "c"})
        while True:
            message = await asyncio.wait_for(queue.get(), 3)
            if message.get("type") == "error":
                return message

    error = asyncio.run(scenario())
    assert attempts == 3 and "stayed unavailable after 2 reconnect attempts" in error["message"]
    assert "code=1006" in error["message"]
