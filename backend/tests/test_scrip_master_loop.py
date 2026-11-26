"""The Dhan instrument master must never be re-downloaded on the event loop while a copy exists. Test data only; no network."""
import asyncio
import time

from app.broker_adapters import DhanAdapter


def test_an_expired_copy_is_served_on_the_event_loop_and_refreshed_only_off_it(monkeypatch):
    old = [{"EXCH_ID": "NSE", "SECURITY_ID": "1"}]
    calls = []
    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", (time.monotonic() - 90000, old))
    monkeypatch.setattr(DhanAdapter, "_download_scrip_master", classmethod(lambda cls, now: calls.append(now) or [{"new": "1"}]))

    async def on_loop():
        return DhanAdapter._scrip_master()

    assert asyncio.run(on_loop()) is old and calls == []          # on the loop: the old copy, no download
    assert DhanAdapter._scrip_master() == [{"new": "1"}] and len(calls) == 1   # in a thread (no running loop): refreshed
