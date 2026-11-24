"""Round 3: checkpoints, the go/no-go rule, the bootstrap interval and the recorded-book execution check. TEST fixtures only."""
import gzip
import json
from datetime import datetime, timezone

import numpy as np

from app.delta_lab import execution_check, round3_status
from app.delta_lab.paper_config import PARAM_HASH


def trades(n, bps):
    return [{"symbol": "BTCUSD", "net_bps": str(bps), "net_usd": str(bps / 10)} for _ in range(n)]


def test_the_parameter_hash_is_stable_and_twelve_hex_characters():
    assert len(PARAM_HASH) == 12 and int(PARAM_HASH, 16) >= 0


def test_no_decision_before_fifty_trades_and_the_four_conditions_after():
    assert round3_status.go_no_go(trades(49, 40), 1.0, 0, 0) == {"decided": False, "reason": "49 of 50 combined trades so far"}
    ok = round3_status.go_no_go(trades(50, 40), 1.0, 0, 0)
    assert ok["decided"] and ok["passed"] is True
    assert round3_status.go_no_go(trades(50, 10), 1.0, 0, 0)["passed"] is False                    # under half of the holdout net/trade (about 45 bps)
    assert round3_status.go_no_go(trades(50, 40), 3.5, 0, 0)["passed"] is False                    # measured slippage too high
    assert round3_status.go_no_go(trades(50, 40), None, 0, 0)["passed"] is False                   # unmeasured counts as not passed
    assert round3_status.go_no_go(trades(50, 40), 1.0, 1, 0)["passed"] is False and round3_status.go_no_go(trades(50, 40), 1.0, 0, 1)["passed"] is False


def test_the_thirty_trade_checkpoint_reports_a_bootstrap_interval():
    cp = round3_status.checkpoints([{"net_bps": str(b), "net_usd": str(b / 10)} for b in list(np.linspace(-20, 80, 30))], {"start": 0}, now=10)
    t = cp["trade30"]
    assert cp["combined_trades"] == 30 and t["bootstrap90_net_bps"][0] < t["net_bps_per_trade"] < t["bootstrap90_net_bps"][1]
    assert "trade30" not in round3_status.checkpoints(trades(29, 10), {"start": 0}, now=10)


def book_file(root, ts, asks, bids):
    day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    d = root / "BTCUSD"
    d.mkdir(parents=True)
    with gzip.open(d / f"book-{day}.jsonl.gz", "wb") as fh:
        fh.write((json.dumps({"t": ts, "asks": asks, "bids": bids}) + "\n").encode())


def test_taker_slippage_walks_the_recorded_book_at_my_size_and_refuses_a_stale_snapshot(tmp_path):
    ts = 1_795_500_000.0
    book_file(tmp_path, ts, asks=[["60000.5", 5], ["60001.0", 10]], bids=[["60000.0", 5]])
    t = {"symbol": "BTCUSD", "entry_time": ts + 2, "side": "long", "qty": 8, "entry_ref": 60000.0}
    # 5 contracts at 60000.5 and 3 at 60001.0 -> average 60000.6875 against the intended 60000.0
    assert abs(execution_check.taker_slippage_bps(tmp_path, t) - (60000.6875 - 60000.0) / 60000.0 * 1e4) < 1e-6
    assert execution_check.taker_slippage_bps(tmp_path, {**t, "entry_time": ts + 30}) is None        # snapshot older than 10 s: no measurement, no guess
    s = execution_check.summarize(tmp_path, [t, {**t, "entry_time": ts + 30}])
    assert s["signals"] == 2 and s["measured"] == 1 and s["median_bps"] is not None


def test_the_daily_health_line_replaces_the_same_day_instead_of_duplicating(tmp_path, monkeypatch):
    monkeypatch.setattr(round3_status, "REPORT_DIR", tmp_path); monkeypatch.setattr(round3_status, "ROOT", tmp_path); monkeypatch.setattr(round3_status, "OUT", tmp_path / "paper_live")
    monkeypatch.setattr(round3_status, "BOOK", tmp_path / "book")
    round3_status.write_all(1_795_500_000.0); round3_status.write_all(1_795_500_100.0)
    lines = [ln for ln in (tmp_path / "paper_weekly.md").read_text().splitlines() if ln.startswith("| 2026-")]
    assert len(lines) == 1 and "DOWN" in lines[0]
    assert "Round 3 status" in (tmp_path / "round3_status.md").read_text()
