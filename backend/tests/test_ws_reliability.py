"""Unexpected disconnects, heartbeat failures, reconnects and subscription restoration for the
Dhan live feed and the Delta public collector. No network: sockets are fakes."""
import asyncio
import json
import logging
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from app import broker_adapters
from app.broker_adapters import DhanAdapter
from app.delta_research import collector as collector_module
from app.delta_research.collector import DeltaPublicCollector
from app.ws_diagnostics import describe_ws_failure


# ───────────── failure descriptions ─────────────
def test_1006_without_close_frame_is_explained_as_a_network_drop_not_a_shutdown():
    text = describe_ws_failure(ConnectionClosedError(None, None))
    assert "abnormal closure (1006)" in text and "without a close frame" in text and "uvicorn" not in text.lower()
    assert "abnormal closure" in describe_ws_failure(RuntimeError("no close frame received or sent"))


def test_keepalive_timeout_server_restart_and_provider_codes_are_distinguished():
    assert "keepalive timeout" in describe_ws_failure(ConnectionClosedError(Close(1011, "keepalive ping timeout"), None))
    assert "restarting" in describe_ws_failure(ConnectionClosedError(Close(1012, "restart"), None))
    assert "provider rejected" in describe_ws_failure(ConnectionClosedError(Close(4403, "x"), None))
    assert describe_ws_failure(OSError("boom")).startswith("OSError: boom")


# ───────────── Dhan live feed ─────────────
def run_dhan_feed(monkeypatch, scripts, *, stop_after_connections):
    """scripts[i] is what connection i's recv() does: an exception to raise, or None to block."""
    sent: list[tuple[int, dict]] = []
    statuses: list[tuple] = []
    active = 0
    max_active = 0
    created = 0

    class Socket:
        close_code = None
        close_reason = ""
        response = type("R", (), {"status_code": 101, "reason_phrase": "ok"})()

        def __init__(self, index):
            self.index = index

        async def send(self, payload):
            sent.append((self.index, json.loads(payload)))

        async def recv(self):
            action = scripts[self.index] if self.index < len(scripts) else None
            if action is not None:
                await asyncio.sleep(0)
                raise action
            await asyncio.sleep(3600)

    class Ctx:
        def __init__(self, index):
            self.index = index

        async def __aenter__(self):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            return Socket(self.index)

        async def __aexit__(self, *_a):
            nonlocal active
            active -= 1

    def fake_connection(*_a, **_k):
        nonlocal created
        created += 1
        return Ctx(created - 1)

    monkeypatch.setattr(broker_adapters, "_single_dhan_live_connection", fake_connection)
    monkeypatch.setattr(broker_adapters, "_dhan_backoff_seconds", lambda *_a, **_k: 0.0)

    async def scenario():
        async def status(*args):
            statuses.append(args)

        agen = DhanAdapter().stream_watchlist_market_quotes(
            "token-value", {"client_id": "client-value"},
            {"a": ("NSE_EQ", "1333"), "b": ("NSE_EQ", "2885")}, asyncio.Queue(maxsize=1), status,
        )
        task = asyncio.create_task(agen.__anext__())
        for _ in range(200):
            if created >= stop_after_connections:
                await asyncio.sleep(0.05)
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agen.aclose()

    asyncio.run(scenario())
    return sent, statuses, max_active, created


def test_dhan_live_abnormal_close_reconnects_and_restores_every_subscription(monkeypatch):
    sent, statuses, max_active, created = run_dhan_feed(
        monkeypatch, [ConnectionClosedError(None, None), None], stop_after_connections=2)
    assert created == 2 and max_active == 1  # reconnected, never two sockets at once
    first = {e["SecurityId"] for i, b in sent if i == 0 and b["RequestCode"] == 17 for e in b["InstrumentList"]}
    second = {e["SecurityId"] for i, b in sent if i == 1 and b["RequestCode"] == 17 for e in b["InstrumentList"]}
    assert first == second == {"1333", "2885"}
    reconnecting = [s for s in statuses if s and s[0] == "reconnecting" and len(s) > 1 and s[1]]
    assert reconnecting and "abnormal closure (1006)" in reconnecting[0][1]


def test_dhan_live_keepalive_timeout_is_reported_as_such_and_reconnects(monkeypatch):
    _sent, statuses, _max, created = run_dhan_feed(
        monkeypatch, [ConnectionClosedError(Close(1011, "keepalive ping timeout"), None), None], stop_after_connections=2)
    reasons = [s[1] for s in statuses if s and s[0] == "connection_failure" and len(s) > 1]
    assert created == 2 and any("keepalive timeout" in r for r in reasons)


def test_dhan_live_keeps_retrying_through_repeated_drops_without_leaking_connections(monkeypatch):
    drops = [ConnectionClosedError(None, None)] * 4 + [None]
    _sent, _statuses, max_active, created = run_dhan_feed(monkeypatch, drops, stop_after_connections=5)
    assert created == 5 and max_active == 1


def test_dhan_dead_peer_detection_budget_is_tight():
    assert broker_adapters._DHAN_PING_TIMEOUT_SECONDS <= 15


# ───────────── Delta public collector ─────────────
class DeltaHarness:
    def __init__(self, monkeypatch, tmp_path, scripts):
        self.sent: list[tuple[int, dict]] = []
        self.connects = 0
        self.collector = DeltaPublicCollector(tmp_path / "delta.sqlite3")
        scripts_ = scripts
        harness = self

        class Socket:
            def __init__(self, index):
                self.index = index

            async def send(self, payload):
                harness.sent.append((self.index, json.loads(payload)))

            async def recv(self):
                action = scripts_[self.index] if self.index < len(scripts_) else None
                if action is not None:
                    await asyncio.sleep(0)
                    raise action
                await asyncio.sleep(3600)

        class Ctx:
            def __init__(self, index):
                self.index = index

            async def __aenter__(self):
                return Socket(self.index)

            async def __aexit__(self, *_a):
                return None

        def connect(*_a, **_k):
            harness.connects += 1
            if harness.connects > len(scripts_):
                harness.collector._stop.set_after_connect = True
            return Ctx(harness.connects - 1)

        monkeypatch.setattr(collector_module, "connect", connect)
        monkeypatch.setattr(collector_module, "reconnect_delay", lambda _n: 0.0)
        monkeypatch.setattr(self.collector, "_save_health", lambda force=False: None)

    async def run(self, until_connects):
        task = asyncio.create_task(self.collector._websocket_loop())
        for _ in range(300):
            if self.connects >= until_connects:
                await asyncio.sleep(0.05)
                break
            await asyncio.sleep(0.01)
        self.collector._stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_delta_unexpected_close_reconnects_restores_subscriptions_and_records_why(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    harness = DeltaHarness(monkeypatch, tmp_path, [ConnectionClosedError(None, None), None])
    asyncio.run(harness.run(until_connects=2))
    by_connection = {i: [m["type"] for idx, m in harness.sent if idx == i] for i in (0, 1)}
    assert by_connection[0] == by_connection[1] == ["enable_heartbeat", "subscribe"]  # restored on reconnect
    channels = [m for idx, m in harness.sent if idx == 1 and m["type"] == "subscribe"][0]["payload"]["channels"]
    assert {c["name"] for c in channels} == {"v2/ticker", "candlestick_1m"}
    status = harness.collector.status()
    assert "abnormal closure (1006)" in status["last_failure_reason"] and status["last_failure_at"] is not None
    assert harness.collector._reconnects >= 1
    assert any("abnormal closure" in r.message for r in caplog.records)  # the log says why, not just the class name


def test_delta_heartbeat_loss_forces_reconnect_and_is_labelled_idle(monkeypatch, tmp_path):
    class Silent(Exception):
        pass

    harness = DeltaHarness(monkeypatch, tmp_path, [None, None])
    monkeypatch.setattr(collector_module, "WS_IDLE_TIMEOUT_SECONDS", 0.01)
    asyncio.run(harness.run(until_connects=2))
    assert harness.connects >= 2
    assert "idle" in harness.collector.status()["last_failure_reason"].lower() or "idle" in (harness.collector.status()["last_error"] or "").lower()


def test_delta_shutdown_close_is_not_recorded_as_an_upstream_failure(monkeypatch, tmp_path):
    harness = DeltaHarness(monkeypatch, tmp_path, [None])

    async def scenario():
        task = asyncio.create_task(harness.collector._websocket_loop())
        await asyncio.sleep(0.05)
        harness.collector._stop.set()  # what stop() does first (uvicorn --reload / shutdown)
        # the socket dies as part of shutdown
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    status = harness.collector.status()
    assert status["consecutive_failures"] == 0 and status["last_failure_reason"] is None


def test_delta_shutdown_race_where_the_socket_errors_after_stop_is_not_a_failure(monkeypatch, tmp_path):
    collector = DeltaPublicCollector(tmp_path / "d.sqlite3")
    monkeypatch.setattr(collector, "_save_health", lambda force=False: None)

    class Ctx:
        async def __aenter__(self):
            collector._stop.set()  # shutdown begins while connecting
            raise ConnectionClosedError(None, None)

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(collector_module, "connect", lambda *_a, **_k: Ctx())
    asyncio.run(collector._websocket_loop())
    assert collector.status()["consecutive_failures"] == 0 and collector._reconnects == 0


def test_delta_start_twice_creates_one_set_of_tasks(monkeypatch, tmp_path):
    created = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(collector_module.httpx, "AsyncClient", lambda **_k: FakeClient())
    collector = DeltaPublicCollector(tmp_path / "once.sqlite3")

    async def loop(name):
        created.append(name)
        while not collector._stop.is_set() or name != "writer":  # the real writer drains and exits on stop
            await asyncio.sleep(0.01)

    monkeypatch.setattr(collector, "_rest_loop", lambda _c: loop("rest"))
    monkeypatch.setattr(collector, "_websocket_loop", lambda: loop("ws"))
    monkeypatch.setattr(collector, "_writer_loop", lambda: loop("writer"))
    monkeypatch.setattr(collector, "_save_health", lambda force=False: None)

    async def scenario():
        first = asyncio.create_task(collector.start())
        await asyncio.sleep(0.05)
        await collector.start()  # second start must return immediately without new tasks
        await asyncio.sleep(0.05)
        count = len(created)
        await collector.stop()
        await asyncio.gather(first, return_exceptions=True)
        return count

    assert asyncio.run(scenario()) == 3
