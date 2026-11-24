"""Paper engine: the frozen R3 rules on LIVE public Delta data, BTCUSD and ETHUSD, through the same signal code, simulator and risk gate as the backtest.
R1 runs as a SHADOW (logged to shadow_r1.csv, no decisions taken from it).

It can never send an order: there is no order code in this module and `--mode live` is refused. (Live orders exist only in app.delta_execution, behind its
three layers.) Data lives under DELTA_LAB_DATA_DIR (a Fly volume in production). Stop a CLI run by creating <dir>/paper_live/STOP.
Decision support, not investment advice."""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import data, families, round2
from .engine import simulate
from .paper_config import FROZEN, PARAM_HASH, SYMBOLS
from .run import BASE, CFG

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get("DELTA_LAB_DATA_DIR", str(REPO / "data" / "lab")))
OUT = ROOT / "paper_live"
KEEP_DAYS = 75        # R3 ranks the 24-hour range against the last 60 days of hourly bars
if os.environ.get("DELTA_LAB_DATA_DIR"):
    data.DATA_DIR = ROOT / "series"
else:
    data.DATA_DIR = REPO / "diagnostics" / "delta_lab_data"


def ensure_spec(symbol: str) -> None:
    f = data.DATA_DIR / f"{symbol}_spec.json"
    if not f.exists():
        data.DATA_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data.product_spec(symbol), default=str))


def refresh(symbol: str, now: int) -> None:
    """Append the newest completed candles to the rolling live series."""
    ensure_spec(symbol)
    for series, prefix, res in (("last_1m", "", "1m"), ("mark_1m", "MARK:", "1m"), ("funding_1h", "FUNDING:", "1h")):
        name = f"{symbol}_live_{series}"
        try:
            old = data.load(name)
            start = int(old["time"].max()) - 3600
        except FileNotFoundError:
            old, start = None, now - KEEP_DAYS * 86400
        new = data.fetch_candles(prefix + symbol, res, start, now)
        df = new if old is None else pd.concat([old, new]).drop_duplicates("time", keep="last")
        df = df[df["time"] >= now - KEEP_DAYS * 86400].sort_values("time").reset_index(drop=True)
        data.save(df, name)


def _funding_hist(m) -> pd.DataFrame:
    """Delta's hourly FUNDING series (percent) as the 8-hourly settlement rows R1 reads (fraction)."""
    f = m.funding_df
    rows = [(int(t) + 3600, float(c) / 100.0) for t, c in zip(f["time"], f["close"]) if (int(t) + 3600) % 28800 == 0]
    return pd.DataFrame(rows, columns=["time", "rate"])


def run_symbol(symbol: str, start_ts: int) -> dict:
    now = int(time.time()) // 60 * 60
    refresh(symbol, now)
    m = families.build_market(symbol, "live_")
    m.atr_4h = round2._atr_grid(m, "4h")
    start_idx = int(m.t.searchsorted(start_ts))
    last_t = int(m.t[-1])
    out = {"symbol": symbol, "last_bar": last_t}
    for role, sigs in (("decision", round2.r3(m, None)), ("shadow", None)):
        if role == "shadow":
            m.funding_hist = _funding_hist(m)
            sigs = round2.r1(m, 10, 2, 72, 2)
        trades, rows, counts = simulate(m, sigs, BASE, CFG, window=(start_idx, len(m.t)))
        closed = [t for t in trades if t["exit_time"] <= last_t - 120]
        out[role] = {"trades": closed, "open": len(trades) - len(closed), "rejections": counts, "rejection_rows": rows}
    return out


def _write_rows(path: Path, header: list[str], rows: list[list]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_results(results: list[dict]) -> None:
    head = ["timestamp_utc", "symbol", "role", "variant", "param_hash", "side", "intended_entry_ref", "entry_fill_assumed", "size_contracts", "exit_time_utc", "exit_reason", "net_usd", "net_bps", "gross_bps",
            "fees", "gst", "funding", "slippage_assumed", "required_margin"]
    for role, fname in (("decision", "paper_trades.csv"), ("shadow", "shadow_r1.csv")):
        rows = []
        for r in results:
            for t in r[role]["trades"]:
                rows.append([datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).isoformat(), r["symbol"], role, t["variant"], PARAM_HASH, t["side"], round(t["entry_ref"], 2), round(t["entry_px"], 2), t["qty"],
                             datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).isoformat(), t["exit_reason"], round(t["net"], 4), round(t["bps_net"], 2), round(t["gross"] / t["notional"] * 1e4, 2) if t["notional"] else "",
                             round(t["fees"], 4), round(t["gst"], 4), round(t["funding"], 4), round(t["slippage"], 4), round(t["required_margin"], 2)])
        _write_rows(OUT / fname, head, rows)
    (OUT / "state.json").write_text(json.dumps({"as_of": time.time(), "param_hash": PARAM_HASH, "frozen": FROZEN,
                                                "symbols": {r["symbol"]: {"last_bar": r["last_bar"], "open": r["decision"]["open"], "closed": len(r["decision"]["trades"]), "rejections": r["decision"]["rejections"],
                                                                          "shadow_closed": len(r["shadow"]["trades"])} for r in results}}, indent=1))


def run_cycle(start_ts: int) -> list[dict]:
    results = [run_symbol(s, start_ts) for s in SYMBOLS]
    write_results(results)
    (OUT / "heartbeat").write_text(str(int(time.time())))
    return results


def start_timestamp() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "start.txt"                                 # the paper run keeps its start across restarts and redeploys
    if f.exists():
        return int(f.read_text())
    ts = int(time.time())
    f.write_text(str(ts))
    return ts


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-trade the frozen R3 rules on live public Delta data. Never sends an order.")
    ap.add_argument("--mode", default="paper"); ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.mode != "paper":
        raise SystemExit("Refused: this engine is paper-only. Live orders go through the existing Delta execution (three layers), not through the lab.")
    start = start_timestamp()
    (OUT / "STOP").unlink(missing_ok=True)
    while not (OUT / "STOP").exists():
        run_cycle(start)
        if a.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
