"""Paper engine: one chosen variant on LIVE public Delta data, using the same signal code, the same simulate() and the same risk_gate() as the backtest.

It can never send an order: there is no order code in this module, and `--mode live` is refused. (Live orders exist only in app.delta_execution,
behind its three layers; that path now also applies the shared liquidation-safety rule.) It also records the top of the book every minute, so the
measured spread can later replace the assumed slippage. Stop it by creating reports/paper_live/STOP. Decision support, not investment advice."""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from . import data, families
from .engine import simulate
from .run import BASE, CFG

REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"
OUT = REPORTS_DIR / "paper_live"
KEEP_DAYS = 75        # R3 ranks the 24-hour range against the last 60 days of hourly bars


def refresh(symbol: str, now: int) -> None:
    """Append the newest completed candles to the rolling live series (45 days: enough for the 30-day windows)."""
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


def record_quote(symbol: str) -> dict | None:
    try:
        q = httpx.get(f"{data.BASE}/v2/tickers/{symbol}", timeout=10).json()["result"].get("quotes") or {}
        return {"time": int(time.time()), "symbol": symbol, "best_bid": q.get("best_bid"), "best_ask": q.get("best_ask"), "bid_size": q.get("bid_size"), "ask_size": q.get("ask_size")}
    except Exception:  # noqa: BLE001 - a missed snapshot is just a gap
        return None


def run_cycle(symbol: str, variant: str, family: str, start_ts: int) -> dict:
    now = int(time.time()) // 60 * 60
    refresh(symbol, now)
    m = families.build_market(symbol, "live_")
    if family.startswith("R"):
        from . import round2
        sigs = {"R3": lambda: round2.r3(m, None)}[family]()      # round-2 families are added here as they pass
    else:
        sigs = dict(families.variants(m, family))[variant]
    start_idx = int(m.t.searchsorted(start_ts))
    trades, rows, counts = simulate(m, sigs, BASE, CFG, window=(start_idx, len(m.t)))   # CFG.mode is paper: nothing can be sent
    last_t = int(m.t[-1])
    done = [t for t in trades if t["exit_time"] <= last_t - 120 and t["exit_reason"] != "time" or t["exit_time"] < last_t - 3600]
    return {"trades": done, "open": len(trades) - len(done), "rejections": counts, "rejection_rows": rows, "last_bar": last_t}


def weekly(result: dict, symbol: str, family: str) -> None:
    """reports/paper_weekly.md: paper vs backtest, by ISO week. Slippage in paper is still modelled (the recorder's measured spread will replace it)."""
    import numpy as np
    res = REPORTS_DIR / "round2_results.json"
    ref = json.loads(res.read_text())["families"].get(family, {}).get(symbol, {}) if res.exists() else {}
    hold = (ref.get("holdout") or {}).get("maker_maker", {}).get("summary", {})
    by: dict[str, list[dict]] = {}
    for t in result["trades"]:
        by.setdefault(datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).strftime("%G-W%V"), []).append(t)
    L = [f"# Paper run: {family} {symbol}", "", "*Paper trades on live Delta data through the same signal code, simulator and risk gate. No order is ever sent. Decision support, not investment advice.*", "",
         f"Backtest holdout reference: {hold.get('net_bps_per_trade', float('nan')):.1f} net bps/trade over {hold.get('trades', '—')} trades.", "",
         "| Week | Trades | Net bps/trade | Net $ | Maker fills | Rejections so far |", "|---|---|---|---|---|---|"]
    for wk, ts in sorted(by.items()):
        L.append(f"| {wk} | {len(ts)} | {np.mean([t['bps_net'] for t in ts]):.1f} | {sum(t['net'] for t in ts):.2f} | {sum(1 for t in ts if t['entry_maker'])} | {sum(result['rejections'].values())} |")
    total = len(result["trades"])
    L += ["", f"Total closed paper trades: {total} (open: {result['open']}). The run needs at least 4 weeks or 50 trades, whichever is later, before any comparison is read.", ""]
    (REPORTS_DIR / "paper_weekly.md").write_text("\n".join(L))


def write(result: dict, symbol: str, family: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "paper_trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_utc", "symbol", "family", "variant", "side", "intended_entry", "size_contracts", "exit_time_utc", "exit_reason", "net_usd", "net_bps", "fees", "gst", "funding", "slippage_assumed"])
        for t in result["trades"]:
            w.writerow([datetime.fromtimestamp(t["entry_time"], tz=timezone.utc).isoformat(), symbol, family, t["variant"], t["side"], round(t["entry_px"], 2), t["qty"],
                        datetime.fromtimestamp(t["exit_time"], tz=timezone.utc).isoformat(), t["exit_reason"], round(t["net"], 4), round(t["bps_net"], 2), round(t["fees"], 4), round(t["gst"], 4), round(t["funding"], 4), round(t["slippage"], 4)])
    (OUT / "rejections.json").write_text(json.dumps({"counts": result["rejections"], "open_positions": result["open"], "last_bar": result["last_bar"]}, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description="Paper-trade one variant on live public Delta data. Never sends an order.")
    ap.add_argument("--symbol", required=True); ap.add_argument("--family", required=True); ap.add_argument("--variant", required=True)
    ap.add_argument("--mode", default="paper"); ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if a.mode != "paper":
        raise SystemExit("Refused: this engine is paper-only. Live orders go through the existing Delta execution (three layers), not through the lab.")
    OUT.mkdir(parents=True, exist_ok=True)
    start_file = OUT / "start.txt"                      # the paper run keeps its start across restarts
    start_ts = int(start_file.read_text()) if start_file.exists() else int(time.time())
    start_file.write_text(str(start_ts))
    (OUT / "STOP").unlink(missing_ok=True)
    while not (OUT / "STOP").exists():
        result = run_cycle(a.symbol, a.variant, a.family, start_ts)
        write(result, a.symbol, a.family)
        weekly(result, a.symbol, a.family)
        quote = record_quote(a.symbol)
        if quote:
            new = not (OUT / "quotes.csv").exists()
            with (OUT / "quotes.csv").open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(quote)); new and w.writeheader(); w.writerow(quote)
        if a.once:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
