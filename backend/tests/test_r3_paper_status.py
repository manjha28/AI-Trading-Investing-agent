"""R3 paper card: the lab's read-only status JSON and the API route that reads it. Files and replies below are TEST DATA."""
import json

import httpx

from app.delta_lab import round3_status
from tests.test_crypto_delta import client, engine, login  # noqa: F401


def test_status_json_reads_the_paper_files(tmp_path, monkeypatch):
    out, book = tmp_path / "paper_live", tmp_path / "book"
    out.mkdir(); book.mkdir()
    (out / "start.txt").write_text("1795392000")
    (out / "state.json").write_text(json.dumps({"symbols": {"BTCUSD": {"last_bar": 1795594620, "open": 0, "closed": 1, "rejections": {}}}}))
    (out / "paper_trades.csv").write_text("timestamp_utc,symbol,role,variant,param_hash,side,intended_entry_ref,entry_fill_assumed,size_contracts,exit_time_utc,exit_reason,net_usd,net_bps,gross_bps,fees,gst,funding,slippage_assumed,required_margin\n"
                                          "2026-11-24T10:00:00+00:00,BTCUSD,decision,R3|3R/48h,eaea623c741f,long,59000,59001,1,2026-11-24T12:00:00+00:00,target,1.5,40.0,50,0.1,0.02,0,0.01,6\n")
    monkeypatch.setattr(round3_status, "OUT", out)
    monkeypatch.setattr(round3_status, "BOOK", book)
    s = round3_status.status_json(now=1795594700)
    assert s["param_hash"] == "eaea623c741f" and s["closed_trades"] == 1 and s["net_usd_total"] == "1.5" and s["symbols"]["BTCUSD"]["closed"] == 1
    assert s["go_no_go"]["decided"] is False and "Paper only" in s["label"]


def test_route_returns_the_lab_status_and_marks_it_stale_when_the_lab_is_down(client, engine, monkeypatch):  # noqa: F811
    import app.api.crypto_delta as api
    login(client, engine, "r3@example.com")
    real = httpx.Client
    up = {"ok": True}

    def handler(request):
        if not up["ok"]:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"param_hash": "eaea623c741f", "closed_trades": 0})
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    api._lab_cache.update(at=None, data=None)
    got = client.get("/api/v1/crypto-delta/r3-paper").json()
    assert got["param_hash"] == "eaea623c741f" and got["stale"] is False
    up["ok"] = False
    got = client.get("/api/v1/crypto-delta/r3-paper").json()
    assert got["stale"] is True and got["param_hash"] == "eaea623c741f" and "could not be reached" in got["error"]
