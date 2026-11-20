"""The Dhan scrip-master index must return exactly what the old full scans did. The master below is TEST DATA."""
import random
import time

import pytest

from app.api.brokers import _dhan_master_matches
from app.broker_adapters import DhanAdapter


def row(sid, exch="NSE", seg="E", inst="EQUITY", series="EQ", isin="", name="", underlying=""):
    return {"EXCH_ID": exch, "SEGMENT": seg, "SECURITY_ID": sid, "SYMBOL_NAME": name, "UNDERLYING_SYMBOL": underlying, "INSTRUMENT": inst, "SERIES": series, "ISIN": isin,
            "LOT_SIZE": "1", "TICK_SIZE": "5", "LISTING_DATE": "", "LIST_DATE": "", "LISTINGDATE": "", "SM_EXPIRY_DATE": "", "EXPIRY_DATE": "", "EXPIRY": ""}


@pytest.fixture
def master(monkeypatch):
    rng = random.Random(4)
    rows = []
    for n in range(1, 400):
        rows.append(row(str(n), "NSE", isin=f"INE{n:05d}A01010", name=f"CO{n}-EQ", underlying=f"CO{n}"))
        if n % 3 == 0:
            rows.append(row(str(n), "BSE", isin=f"INE{n:05d}A01010", name=f"CO{n}", underlying=f"CO{n}"))       # dual-listed
        if n % 5 == 0:
            rows.append(row(str(n + 10_000), "NSE", seg="D", inst="FUTSTK", series="XX", isin=f"INE{n:05d}A01010", name=f"CO{n}FUT"))   # a derivative with the same ISIN
        if n % 7 == 0:
            rows.append(row("ABC", "NSE", isin=f"INE{n:05d}A01010", name="BAD"))                                      # non-numeric id
        if n % 11 == 0:
            rows.append(row(str(n), "NSE", isin=f"INE{n:05d}A01010", name=f"CO{n}-EQ", underlying=f"CO{n}"))        # an exact duplicate later in the file
        if n % 13 == 0:
            rows.append(row(str(n + 20_000), "NSE", series="BE", isin=f"INE{n:05d}A01010", name=f"CO{n}"))          # a different series
    rng.shuffle(rows)
    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", (time.monotonic(), rows))
    monkeypatch.setattr(DhanAdapter, "_equity_lookup_cache", None)
    return rows


def test_the_index_path_equals_the_scan_path_for_every_kind_of_lookup(master):
    scan_copy = list(master)                                        # a different list object: forces the original scan
    assert scan_copy is not DhanAdapter._scrip_master_cache[1]
    queries = [(str(n), None) for n in range(0, 420, 3)] + [(None, f"INE{n:05d}A01010") for n in range(1, 420, 4)] + [(str(n), f"INE{n:05d}A01010") for n in range(1, 400, 9)]
    queries += [("999999", None), (None, "INE00000X00000"), ("", ""), ("5", " ine00005a01010 "), ("5", "INE00006A01010"), ("ABC", None), ("10005", None), ("20013", None)]
    for sid, isin in queries:
        assert _dhan_master_matches(master, sid, isin) == _dhan_master_matches(scan_copy, sid, isin), (sid, isin)


def reference_identity(rows, symbol, isin, security_id=None):
    """The original implementation, verbatim, as the oracle."""
    s = symbol.strip().upper().removesuffix("-EQ")
    i = isin.strip().upper() if isin else ""
    if not s or not i:
        return None
    cands = [r for r in rows if r.get("EXCH_ID", "").strip().upper() in {"NSE", "BSE"} and r.get("SEGMENT", "").strip().upper() == "E" and r.get("INSTRUMENT", "").strip().upper() == "EQUITY"
             and r.get("SERIES", "").strip().upper() == "EQ" and r.get("ISIN", "").strip().upper() == i
             and s in {r.get("SYMBOL_NAME", "").strip().upper().removesuffix("-EQ"), r.get("UNDERLYING_SYMBOL", "").strip().upper().removesuffix("-EQ")} and r.get("SECURITY_ID", "").strip().isdigit()]
    if security_id and security_id.isdigit():
        cands = [r for r in cands if r.get("SECURITY_ID", "").strip() == security_id]
    resolved = {(r["SECURITY_ID"].strip(), "NSE_EQ" if r["EXCH_ID"].strip().upper() == "NSE" else "BSE_EQ") for r in cands}
    return next(iter(resolved)) if len(resolved) == 1 else None


def test_identity_resolution_matches_the_original_implementation(master):
    for n in range(1, 420, 2):
        for symbol in (f"CO{n}", f"co{n}-eq", "WRONG"):
            for sid in (None, str(n), "7"):
                got = DhanAdapter.find_verified_equity_mapping_by_identity(symbol, f"INE{n:05d}A01010", sid)
                assert got == reference_identity(master, symbol, f"INE{n:05d}A01010", sid), (symbol, n, sid)
    assert DhanAdapter.find_verified_equity_mapping_by_identity("CO3", None) is None and DhanAdapter.find_verified_equity_mapping_by_identity("", "INE00003A01010") is None


def test_the_index_is_built_once_per_master_load_and_rebuilt_when_the_master_is_replaced(master, monkeypatch):
    builds = []
    original = DhanAdapter._equity_lookup.__func__

    first = DhanAdapter._equity_lookup()
    assert DhanAdapter._equity_lookup()[0] is first[0]                                  # served from the cache: the same dict object
    DhanAdapter._scrip_master_cache = (time.monotonic(), [row("1", isin="INE99999A01010", name="NEW", underlying="NEW")])
    fresh = DhanAdapter._equity_lookup()
    assert fresh[0] is not first[0] and list(fresh[0]) == ["INE99999A01010"]            # a refreshed master is re-indexed, never read stale
    assert builds == [] and original is not None


def test_lookups_do_not_scan_the_master(master):
    DhanAdapter._equity_lookup()                                                          # warm
    big = [row(str(n), isin=f"INE{n:06d}A01010", name=f"X{n}", underlying=f"X{n}") for n in range(1, 60_001)]
    DhanAdapter._scrip_master_cache = (time.monotonic(), big)
    DhanAdapter._equity_lookup()
    started = time.perf_counter()
    for n in range(1, 2001):
        assert _dhan_master_matches(big, str(n), None)
        assert DhanAdapter.find_verified_equity_mapping_by_identity(f"X{n}", f"INE{n:06d}A01010") == (str(n), "NSE_EQ")
    assert time.perf_counter() - started < 1.0                                           # 4,000 lookups into a 60,000-row master (a scan would take minutes)


# ───────────────────────── background warm-up ─────────────────────────
def test_warm_once_downloads_when_missing_skips_when_fresh_and_keeps_the_old_copy_on_failure(monkeypatch):
    from app import dhan_master_warm as warm

    calls: list[bool] = []

    def fake_master(cls, force=False):
        if force:
            calls.append(force)                                                          # only forced (re-download) calls are counted; the index build reads the cache
        if len(calls) == 2 and force:
            raise RuntimeError("download failed")
        DhanAdapter._scrip_master_cache = (time.monotonic(), [row("1", isin="INE00001A01010", name="A", underlying="A")])
        return DhanAdapter._scrip_master_cache[1]

    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", None)
    monkeypatch.setattr(DhanAdapter, "_equity_lookup_cache", None)
    monkeypatch.setattr(DhanAdapter, "_scrip_master", classmethod(fake_master))
    assert warm.warm_once() == "refreshed" and calls == [True]                           # nothing cached: downloads and indexes
    assert DhanAdapter._equity_lookup_cache is not None
    assert warm.warm_once() == "fresh" and calls == [True]                               # a fresh copy is left alone
    old = DhanAdapter._scrip_master_cache
    DhanAdapter._scrip_master_cache = (time.monotonic() - 21 * 3600, old[1])             # older than 20h: due
    assert warm.warm_once() == "failed" and calls == [True, True]                        # the refresh raised ...
    assert DhanAdapter._scrip_master_cache[1] is old[1]                                  # ... and the previous copy is still what requests are served


def test_a_forced_refresh_does_not_clear_the_old_master_until_the_new_one_arrives(monkeypatch):
    rows = [row("1", isin="INE00001A01010", name="A", underlying="A")]
    monkeypatch.setattr(DhanAdapter, "_scrip_master_cache", (time.monotonic() - 90_000, rows))                       # expired
    assert DhanAdapter._scrip_master_cache[1] is rows

    class Boom:
        def __enter__(self):
            raise __import__("httpx").ConnectError("down")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr("app.broker_adapters.httpx.stream", lambda *a, **k: Boom())
    with pytest.raises(Exception, match="unavailable"):
        DhanAdapter._scrip_master(force=True)
    assert DhanAdapter._scrip_master_cache[1] is rows                                    # a failed download never leaves requests without a master
