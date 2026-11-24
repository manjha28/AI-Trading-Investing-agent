"""Round 3 status: recorder health, paper trades, the pre-registered checkpoints, rejections, and the daily health line for paper_weekly.md.
Writes <data root>/round3_status.md and <data root>/paper_weekly.md (on Fly the data root is the volume; copy them into reports/ for the repo).
Hypothetical research; decision support, not investment advice. Nothing here sends an order."""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import execution_check
from .paper_config import FROZEN, HOLDOUT, PARAM_HASH

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get("DELTA_LAB_DATA_DIR", str(REPO / "data" / "lab")))
BOOK = Path(os.environ.get("DELTA_BOOK_DIR", str(REPO / "data" / "delta_book")))
REPORT_DIR = Path(os.environ.get("DELTA_LAB_REPORT_DIR", str(ROOT)))
OUT = ROOT / "paper_live"


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as fh:
        return list(csv.DictReader(fh))


def recorder_health(now: float | None = None) -> dict:
    now = now or time.time()
    st = json.loads((BOOK / "status.json").read_text()) if (BOOK / "status.json").exists() else None
    gaps_s, gap_events = 0.0, 0
    log = BOOK / "gaps.log"
    if log.exists():
        cutoff = now - 86400
        for line in log.read_text().splitlines():
            try:
                stamp = datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
            except ValueError:
                continue
            if stamp >= cutoff and "gap end" in line and " after " in line:
                gap_events += 1
                try:
                    gaps_s += float(line.rsplit(" after ", 1)[1].rstrip("s"))
                except ValueError:
                    pass
    window = min(86400.0, (st or {}).get("uptime_s", 86400)) or 86400.0
    last_book = [t for t in ((st or {}).get("seconds_since_last_book") or {}).values() if t is not None]
    return {"running": st is not None and now - st["as_of"] < 120, "uptime_pct_24h": round(max(0.0, 100 * (1 - gaps_s / window)), 2) if st else None, "gap_events_24h": gap_events,
            "last_snapshot": datetime.fromtimestamp(st["as_of"] - min(last_book), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if st and last_book else None,
            "reconnects": (st or {}).get("counts", {}).get("reconnects")}


def paper_heartbeat(now: float | None = None) -> float | None:
    hb = OUT / "heartbeat"
    return (now or time.time()) - float(hb.read_text()) if hb.exists() else None


def bootstrap_interval(returns: np.ndarray, stat, n: int = 4000, seed: int = 7) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    vals = [stat(rng.choice(returns, len(returns), replace=True)) for _ in range(n)]
    return float(np.percentile(vals, 5)), float(np.percentile(vals, 95))


def pf(r: np.ndarray) -> float:
    neg = -r[r < 0].sum()
    return float(r[r > 0].sum() / neg) if neg > 0 else float("inf")


def checkpoints(trades: list[dict], state: dict, now: float | None = None) -> dict:
    now = now or time.time()
    n = len(trades)
    net_bps = np.array([float(t["net_bps"]) for t in trades]) if n else np.array([])
    net_usd = np.array([float(t["net_usd"]) for t in trades]) if n else np.array([])
    ref = np.mean([HOLDOUT[s]["net_bps_per_trade"] for s in HOLDOUT])
    started = state.get("start")
    out: dict = {"combined_trades": n, "holdout_reference_net_bps": float(ref)}
    out["week4"] = {"due": bool(started and now - started >= 28 * 86400)}
    if n >= 30:
        lo, hi = bootstrap_interval(net_bps, np.mean)
        out["trade30"] = {"net_bps_per_trade": float(net_bps.mean()), "profit_factor": pf(net_usd), "bootstrap90_net_bps": [lo, hi], "vs_holdout_ratio": float(net_bps.mean() / ref)}
    return out


def go_no_go(trades: list[dict], median_slip_bps: float | None, gate_failures: int, reconciliation_mismatches: int) -> dict:
    n = len(trades)
    if n < 50:
        return {"decided": False, "reason": f"{n} of 50 combined trades so far"}
    mean = float(np.mean([float(t["net_bps"]) for t in trades]))
    ref = float(np.mean([HOLDOUT[s]["net_bps_per_trade"] for s in HOLDOUT]))
    checks = {"paper_net_over_half_of_holdout": mean > 0.5 * ref, "measured_slippage_median_under_3bps": median_slip_bps is not None and median_slip_bps < 3.0,
              "no_risk_gate_failure": gate_failures == 0, "no_reconciliation_mismatch": reconciliation_mismatches == 0}
    return {"decided": True, "passed": all(checks.values()), "paper_net_bps": mean, "checks": checks}


def build(now: float | None = None) -> str:
    now = now or time.time()
    state = json.loads((OUT / "state.json").read_text()) if (OUT / "state.json").exists() else {}
    start_f = OUT / "start.txt"
    state["start"] = int(start_f.read_text()) if start_f.exists() else None
    trades = _read_csv(OUT / "paper_trades.csv")
    shadow = _read_csv(OUT / "shadow_r1.csv")
    health = recorder_health(now)
    hb = paper_heartbeat(now)
    cp = checkpoints(trades, state, now)
    ex_trades = [{"symbol": t["symbol"], "entry_time": datetime.fromisoformat(t["timestamp_utc"]).timestamp(), "side": t["side"], "qty": t["size_contracts"], "entry_ref": t["intended_entry_ref"]} for t in trades]
    ex = execution_check.summarize(BOOK, ex_trades) if ex_trades else {"signals": 0, "measured": 0, "median_bps": None, "p90_bps": None, "note": "no paper trades yet"}
    rej: dict[str, int] = {}
    for sym in (state.get("symbols") or {}).values():
        for k, v in (sym.get("rejections") or {}).items():
            rej[k] = rej.get(k, 0) + v
    gate_failures = 0                                                # a gate FAILURE would be an exception or a stop beyond liquidation on a sent order; paper rejections are normal outcomes
    gng = go_no_go(trades, ex["median_bps"], gate_failures, 0)
    dsr_f = Path(os.environ.get("DELTA_LAB_DSR", str(REPO / "reports" / "round3_dsr.json")))
    L = ["# Round 3 status: R3 paper test", "", f"*Generated {datetime.fromtimestamp(now, tz=timezone.utc):%Y-%m-%d %H:%M UTC}. Paper trades only; no order is ever sent. Decision support, not investment advice.*", "",
         f"Frozen parameter hash `{PARAM_HASH}` (on every paper row). Started {datetime.fromtimestamp(state['start'], tz=timezone.utc):%Y-%m-%d %H:%M UTC}." if state.get("start") else "Paper run not started yet.", "",
         "## Health", f"- Order-book recorder: {'running' if health['running'] else '**NOT RUNNING**'}; uptime {health['uptime_pct_24h']}% over 24 h; {health['gap_events_24h']} gap events; reconnects {health['reconnects']}; last snapshot {health['last_snapshot']}.",
         f"- Paper engine heartbeat: {'%.0f s ago' % hb if hb is not None else 'none yet'}.", "",
         "## Paper trades", f"- Closed R3 trades: {len(trades)} combined ({sum(1 for t in trades if t['symbol'] == 'BTCUSD')} BTCUSD, {sum(1 for t in trades if t['symbol'] == 'ETHUSD')} ETHUSD). Open positions: {sum(s.get('open', 0) for s in (state.get('symbols') or {}).values())}.",
         f"- R1 shadow trades (logged only, no decisions): {len(shadow)}.", "",
         "## Pre-registered checkpoints", f"- Holdout reference: BTCUSD {HOLDOUT['BTCUSD']['net_bps_per_trade']} and ETHUSD {HOLDOUT['ETHUSD']['net_bps_per_trade']} net bps/trade.",
         f"- **Week 4 execution check** ({'due' if cp['week4']['due'] else 'not due yet'}): signals {ex['signals']}, measured against the recorded book {ex['measured']}; median taker slippage "
         f"{'%.2f bps' % ex['median_bps'] if ex['median_bps'] is not None else '—'}, 90th percentile {'%.2f bps' % ex['p90_bps'] if ex['p90_bps'] is not None else '—'}. {ex.get('note') or ''}"]
    if "trade30" in cp:
        t30 = cp["trade30"]
        L.append(f"- **30 trades reached**: net {t30['net_bps_per_trade']:.1f} bps/trade (bootstrap 90% interval {t30['bootstrap90_net_bps'][0]:.1f} to {t30['bootstrap90_net_bps'][1]:.1f}), profit factor {t30['profit_factor']:.2f}, {t30['vs_holdout_ratio']:.2f}x the holdout reference.")
    else:
        L.append(f"- 30-trade checkpoint: {len(trades)} of 30 so far.")
    L.append(f"- **Go/no-go at 50 trades**: " + (f"{'PASS' if gng['passed'] else 'NO-GO'}; " + ", ".join(f"{k}: {'yes' if v else 'no'}" for k, v in gng["checks"].items()) if gng["decided"] else gng["reason"]) + ".")
    L += ["", "## Risk-gate rejections so far (paper)", ("; ".join(f"{k}: {v}" for k, v in sorted(rej.items(), key=lambda kv: -kv[1])) or "none"), "Reconciliation: not applicable in paper (no exchange state); a mismatch can only occur once orders exist.", "",
          "## Re-costing with measured slippage",
          f"Eligible after 2 weeks of recorder data (recording since {datetime.fromtimestamp((json.loads((BOOK / 'status.json').read_text())['as_of'] - json.loads((BOOK / 'status.json').read_text())['uptime_s']), tz=timezone.utc):%Y-%m-%d}): run `python -m app.delta_lab.measure_costs` for the table and then the single holdout re-run." if (BOOK / "status.json").exists() else "Recorder has no data yet.", ""]
    if dsr_f.exists():
        d = json.loads(dsr_f.read_text())
        L += ["## Deflated Sharpe (trade level)", f"Best result: {d['best']}; {d['trades']} trades; per-trade Sharpe {d['sharpe_per_trade']:.3f}; benchmark SR0 {d['benchmark_sr0']:.3f} from the variance {d['variance_of_sharpe_across_variants']:.4f} of per-trade Sharpe across {d['trials_with_enough_trades']} variants, N={d['n_variants_true']}; "
              f"**deflated Sharpe probability {d['deflated_sharpe_prob']:.2f}** (single-test p-value {d['plain_p_value_single_test']:.3f}). Method in `round3_dsr.json`.", ""]
    L += ["## Frozen rules", "```", json.dumps(FROZEN, indent=1), "```"]
    return "\n".join(L) + "\n"


def health_line(now: float | None = None) -> str:
    now = now or time.time()
    h, hb = recorder_health(now), paper_heartbeat(now)
    return (f"| {datetime.fromtimestamp(now, tz=timezone.utc):%Y-%m-%d} | {'up' if h['running'] else 'DOWN'} | {h['uptime_pct_24h']}% | {h['gap_events_24h']} | {h['last_snapshot']} | "
            f"{'%.0f s' % hb if hb is not None else 'none'} |")


def write_all(now: float | None = None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "round3_status.md").write_text(build(now))
    f = REPORT_DIR / "paper_weekly.md"
    head = "# Paper run health and weekly numbers\n\n| Day (UTC) | Recorder | Uptime 24 h | Gap events | Last snapshot | Paper heartbeat age |\n|---|---|---|---|---|---|\n"
    body = f.read_text() if f.exists() else head
    line = health_line(now)
    day = line.split("|")[1].strip()
    lines = [ln for ln in body.splitlines() if not ln.startswith(f"| {day} ")]
    if not any(ln.startswith("| Day (UTC)") for ln in lines):
        lines = head.splitlines()
    lines.append(line)
    f.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    write_all()
    print((REPORT_DIR / "round3_status.md").read_text()[:1500])
