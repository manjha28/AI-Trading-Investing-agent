"""Recorder: snapshots and trades are written, a quiet feed is logged as a gap, and a dropped connection reconnects with backoff. TEST fakes only."""
import asyncio
import gzip
import json

from app.delta_lab.recorder import Recorder


class Clock:
    def __init__(self, t=1_795_500_000.0):
        self.t = t

    def __call__(self):
        return self.t


def book_msg(sym="BTCUSD", bid="60000.0", ask="60000.5"):
    return {"type": "l2_orderbook", "symbol": sym, "timestamp": 1, "buy": [{"limit_price": bid, "size": 5, "depth": "5"}] * 12, "sell": [{"limit_price": ask, "size": 4, "depth": "4"}] * 12}


def read(path):
    with gzip.open(path, "rb") as fh:
        return [json.loads(line) for line in fh.read().decode().splitlines()]


def test_book_snapshots_keep_the_top_ten_levels_and_trades_are_stored(tmp_path):
    clock = Clock()
    rec = Recorder(["BTCUSD", "ETHUSD"], tmp_path, clock)
    rec.handle_message(book_msg()); rec.handle_message({"type": "trades", "sy": "BTCUSD", "p": "60000.5", "s": 3.0, "r": "m", "t": 1, "ts": 2})
    rec.sample_books(); rec.flush_trades()
    book = read(rec.files.path("BTCUSD", "book", clock()))
    assert len(book) == 1 and len(book[0]["bids"]) == 10 and book[0]["asks"][0] == ["60000.5", 4]
    assert read(rec.files.path("BTCUSD", "trades", clock()))[0]["p"] == "60000.5"
    assert rec.counts["book_rows"] == 1 and rec.counts["trades"] == 1


def test_a_quiet_feed_is_logged_as_a_gap_and_recovery_is_logged_too(tmp_path):
    clock = Clock()
    rec = Recorder(["BTCUSD"], tmp_path, clock)
    rec.connected["book"] = True
    rec.handle_message(book_msg()); rec.sample_books()
    clock.t += 9                                                   # nothing for 9 s: over the 5 s gap threshold
    rec.sample_books()
    assert rec.counts["gaps"] == 1 and "book gap start BTCUSD" in (tmp_path / "gaps.log").read_text()
    assert rec.counts["book_rows"] == 1                            # no stale snapshot was written during the gap
    rec.handle_message(book_msg()); rec.sample_books()
    assert "book gap end BTCUSD" in (tmp_path / "gaps.log").read_text() and rec.counts["book_rows"] == 2


def test_status_reports_health_and_staleness(tmp_path):
    clock = Clock()
    rec = Recorder(["BTCUSD"], tmp_path, clock)
    assert rec.status()["healthy"] is False                        # nothing received yet
    rec.connected.update({"book": True, "trades": True}); rec.handle_message(book_msg())
    assert rec.status()["healthy"] is True
    clock.t += 31
    assert rec.status()["healthy"] is False and rec.status()["seconds_since_last_book"]["BTCUSD"] == 31.0
    rec.write_status()
    assert json.loads((tmp_path / "status.json").read_text())["counts"]["reconnects"] == 0


def test_a_dropped_connection_reconnects_with_growing_backoff(tmp_path, monkeypatch):
    rec = Recorder(["BTCUSD"], tmp_path)
    sleeps, attempts = [], []

    class Boom:
        async def __aenter__(self):
            raise ConnectionError("down")
        async def __aexit__(self, *a):
            return False

    class Good:
        def __init__(self): self.sent = []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send(self, m): self.sent.append(m)
        def __aiter__(self): return self._gen()
        async def _gen(self):
            yield json.dumps(book_msg())
            raise asyncio.CancelledError                           # ends the test after one good message

    def connect(url, **kw):
        attempts.append(1)
        return Boom() if len(attempts) < 4 else Good()

    async def fake_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr("app.delta_lab.recorder.asyncio.sleep", fake_sleep)
    try:
        asyncio.run(rec._listen("book", "wss://x", "l2_orderbook", connect))
    except asyncio.CancelledError:
        pass
    assert sleeps[:3] == [1.0, 2.0, 4.0] and rec.counts["reconnects"] == 3          # 1 s, 2 s, 4 s: it backs off, then connects
    assert "BTCUSD" in rec.latest_book
    assert (tmp_path / "gaps.log").read_text().count("connection lost") == 3
