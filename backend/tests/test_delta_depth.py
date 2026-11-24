"""Delta depth panel backend. All books, messages and clocks below are TEST DATA; nothing touches the network."""
import asyncio
import json
from decimal import Decimal as D

from app.delta_depth import hub as hubmod
from app.delta_depth.book import OrderBook, funding_countdown_seconds, group_levels
from app.delta_depth.hub import DepthFeed, DepthHub

SNAP = {"type": "l2_updates", "action": "snapshot", "symbol": "BTCUSD", "sequence_no": 10,
        "bids": [["100.0", "5"], ["99.5", "10"], ["99.0", "20"]], "asks": [["100.5", "4"], ["101.0", "8"], ["101.5", "16"]]}


def upd(seq, bids=(), asks=()):
    return {"type": "l2_updates", "action": "update", "symbol": "BTCUSD", "sequence_no": seq, "bids": [list(b) for b in bids], "asks": [list(a) for a in asks]}


def feed(clock=lambda: 0.0, rest=None):
    async def default_rest(_s):
        return [["100.0", "5"], ["99.5", "10"]], [["100.5", "4"], ["101.0", "8"]]
    return DepthFeed("BTCUSD", clock, lambda url: None, rest or default_rest)


def test_updates_apply_in_sequence_and_zero_size_removes_a_level():
    f = feed()
    assert f.handle(SNAP) is None
    assert f.handle(upd(11, bids=[("100.0", "0"), ("99.8", "7")], asks=[("100.5", "9")])) is None
    top = f.book.top(5)
    assert [b["price"] for b in top["bids"]] == ["99.8", "99.5", "99.0"] and top["asks"][0]["size"] == "9"
    assert f.book.sequence == 11
    assert f.handle(upd(11)) is None and f.book.sequence == 11            # an old or repeated message is ignored


def test_a_sequence_gap_discards_the_book_and_resyncs_from_the_rest_snapshot():
    f = feed()
    f.handle(SNAP)
    assert f.handle(upd(13, bids=[("99.9", "1")])) == "resync"             # 12 is missing
    assert f.book.needs_resync and f.book.gaps == 1
    assert "99.9" not in [b["price"] for b in f.book.top(5)["bids"]]       # the gapped update was NOT applied
    asyncio.run(f.resync())
    assert f.book.provisional and f.resyncs == 1 and f.book.metrics()["best_ask"] == "100.5"   # shown from REST
    assert f.handle(upd(14, bids=[("99.9", "1")])) == "resync"             # updates stay blocked until the socket sends a fresh snapshot
    assert f.handle({**SNAP, "sequence_no": 50}) is None
    assert not f.book.provisional and not f.book.needs_resync and f.book.sequence == 50
    assert f.handle(upd(51, asks=[("100.5", "1")])) is None


def test_a_crossed_book_is_never_shown_and_forces_a_resync():
    f = feed()
    f.handle(SNAP)
    assert f.handle(upd(11, bids=[("100.5", "3")])) == "resync"            # bid reaches the best ask
    assert f.book.crossed_rejections == 1
    assert max(D(b["price"]) for b in f.book.top(5)["bids"]) == D("100.0")  # the book was left as it was
    crossed_snapshot = {**SNAP, "bids": [["101.0", "1"]], "asks": [["100.0", "1"]]}
    g = feed()
    assert g.handle(crossed_snapshot) == "resync" and g.book.bids == {}


def test_stale_badge_after_three_seconds_without_an_update():
    t = {"now": 100.0}
    f = feed(lambda: t["now"])
    assert f.view()["stale"] is True                                       # nothing received yet is not "live"
    f.handle(SNAP)
    assert f.view()["stale"] is False
    t["now"] = 102.9
    assert f.view()["stale"] is False
    t["now"] = 103.1
    v = f.view()
    assert v["stale"] is True and v["age_seconds"] == 3.1


def test_metrics_imbalance_spread_and_grouping():
    f = feed()
    f.handle(SNAP)
    m = f.book.metrics()
    assert m["best_bid"] == "100.0" and m["best_ask"] == "100.5" and m["mid"] == "100.25" and m["spread"] == "0.5" and m["spread_bps"] == "49.88"
    assert m["imbalance_5"] == str(((D(35) - D(28)) / D(63)).quantize(D("0.0001")))
    grouped = group_levels(f.book.top(20)["bids"], D("1"), "bid")          # 100.0 | 99.5 + 99.0 -> 99
    assert [(g["price"], g["size"]) for g in grouped] == [("100", "5"), ("99", "30")] and grouped[-1]["cum"] == "35"
    assert 0 < funding_countdown_seconds(1_000_000.0) <= 8 * 3600


def test_trade_tape_marks_the_aggressor_and_keeps_the_last_50():
    f = feed()
    for i in range(60):
        f.handle({"type": "all_trades", "price": str(100 + i), "size": 1, "buyer_role": "taker" if i % 2 == 0 else "maker", "seller_role": "maker", "timestamp": i})
    assert len(f.tape) == 50 and f.tape[0]["price"] == "159" and f.tape[0]["aggressor"] == "sell" and f.tape[1]["aggressor"] == "buy"
    f.handle({"type": "v2/ticker", "funding_rate": "0.01", "oi_value_usd": "123", "spot_price": "100.1", "mark_price": "100.2"})
    assert f.mark == {"mark_price": "100.2", "index_price": "100.1", "funding_rate": "0.01", "open_interest_usd": "123"}


def test_renders_at_most_five_times_a_second_and_a_slow_viewer_gets_only_the_newest():
    assert hubmod.RENDER_HZ == 5
    f = feed()
    q = asyncio.Queue(maxsize=2)
    f.subscribers.add(q)
    f.handle(SNAP)
    for _ in range(5):
        f.publish()
    assert q.qsize() == 2                                                  # no backlog


def test_subscription_exists_only_while_a_panel_is_open_and_is_dropped_30s_after_the_last_viewer():
    t = {"now": 0.0}
    hub = DepthHub(clock=lambda: t["now"], start_tasks=False)
    a, b = hub.subscribe("btcusd"), hub.subscribe("BTCUSD")
    assert list(hub.feeds) == ["BTCUSD"] and len(hub.feeds["BTCUSD"].subscribers) == 2    # one feed shared by two viewers
    hub.unsubscribe("BTCUSD", a)
    t["now"] = 100.0
    assert hub.reap() == []                                                # a viewer is still there
    hub.unsubscribe("BTCUSD", b)
    t["now"] = 129.9
    assert hub.reap() == [] and "BTCUSD" in hub.feeds
    c = hub.subscribe("BTCUSD")                                            # reopened within 30 s: kept
    hub.unsubscribe("BTCUSD", c)
    t["now"] = 150.0
    assert hub.reap() == [] 
    t["now"] = 180.1
    assert hub.reap() == ["BTCUSD"] and hub.feeds == {}


def test_socket_loop_resubscribes_after_a_gap_and_sends_only_public_channels():
    sent, rest_calls = [], []

    class FakeWS:
        def __init__(self, msgs):
            self.msgs = msgs
        async def send(self, m):
            sent.append(json.loads(m))
        def __aiter__(self):
            async def gen():
                for m in self.msgs:
                    yield json.dumps(m)
                await asyncio.sleep(3600)
            return gen()

    class Ctx:
        async def __aenter__(self):
            return FakeWS([SNAP, upd(12)])                                  # 11 is missing
        async def __aexit__(self, *a):
            return False

    async def rest(_s):
        rest_calls.append(1)
        return [["100.0", "5"]], [["100.5", "4"]]

    async def go():
        f = DepthFeed("BTCUSD", lambda: 0.0, lambda url: Ctx(), rest)
        task = asyncio.create_task(f.run())
        await asyncio.sleep(0.2)
        task.cancel()
        return f
    f = asyncio.run(go())
    names = [(m["type"], [c["name"] for c in m["payload"]["channels"]]) for m in sent]
    assert names[0] == ("subscribe", ["l2_updates", "all_trades", "mark_price", "v2/ticker"])
    assert ("unsubscribe", ["l2_updates"]) in names and names.count(("subscribe", ["l2_updates"])) == 1   # fresh snapshot requested
    assert rest_calls == [1] and f.resyncs == 1
    assert all(m["type"] in ("subscribe", "unsubscribe") for m in sent)                                  # no order or private channel is ever sent


def test_stream_auth(client, engine):  # noqa: F811
    from starlette.websockets import WebSocketDisconnect

    from app.delta_depth.hub import hub
    login(client, engine)                                                            # verified email, no 2FA-verified session
    with client.websocket_connect("/api/v1/delta-depth/BTCUSD/stream", headers={"origin": "http://localhost:5173"}) as socket:
        refused = socket.receive_json()
        assert refused["status"] == "unavailable"
    with client.websocket_connect("/api/v1/delta-depth/BTC-INR/stream", headers={"origin": "http://localhost:5173"}) as socket:
        assert "USD-quoted" in socket.receive_json()["message"]
    assert hub.feeds == {}                                                           # a refused viewer never opens a Delta socket


from tests.test_market_commodities import client, engine, login  # noqa: E402,F401
