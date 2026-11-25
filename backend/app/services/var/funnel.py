"""Candidate funnel for VAR: raw candidate -> executed trade, with every rejection attributed to a reason.

A CANDIDATE is a setup that reached its trigger bar (pattern formed). Patterns that never complete are counted separately as
``pattern_stages`` so you can see where a branch dies (e.g. reversal: probe -> failure -> retest). Reasons are attributed two ways:
``first_failed`` (exclusive: the first failing condition in evaluation order, sums to the rejected count) and ``any_failed`` (a candidate
failing several conditions counts once per condition, so these overlap and exceed the rejected count)."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def tod_bucket(iso: str) -> str:
    t = datetime.fromisoformat(iso).astimezone(IST)
    m = (t.minute // 30) * 30
    return f"{t.hour:02d}:{m:02d}"


def _reason_key(r: str) -> str:
    return r.split(":")[0].strip()


def _pct(n: int, d: int) -> float | None:
    return round(n / d * 100, 1) if d else None


def build_funnel(rejections: list[dict], signals: list[dict], trades: list[dict], stage_counts: dict | None = None, *, engine: str = "VAR") -> dict:
    strat = [r for r in rejections if r.get("stage") == "strategy"]
    risk = [r for r in rejections if r.get("stage") == "risk"]
    execr = [r for r in rejections if r.get("stage") == "execution"]
    n_sig, n_trades = len(signals), len(trades)
    candidates = len(strat) + n_sig

    def table(items: list[dict], mode: str) -> list[dict]:
        c: Counter = Counter()
        for r in items:
            reasons = r["reasons"]
            if mode == "first":
                c[_reason_key(reasons[0])] += 1
            else:
                for k in {_reason_key(x) for x in reasons}:
                    c[k] += 1
        return [{"reason": k, "n": v, "pct_of_stage_rejected": _pct(v, len(items))} for k, v in c.most_common()]

    rows: list[dict] = []

    def add(stage: str, items: list[dict], mode: str = "first") -> None:
        for r in items:
            reasons = r["reasons"][:1] if mode == "first" else r["reasons"]
            for reason in reasons:
                rows.append({"engine": engine, "stage": stage, "reason": _reason_key(reason), "instrument": r.get("instrument"), "session": r["bar_end"][:10],
                             "tod_bucket": tod_bucket(r["bar_end"]), "setup": r.get("setup"), "direction": r.get("direction")})

    add("strategy", strat); add("risk", risk); add("execution", execr)
    for s in signals:
        rows.append({"engine": engine, "stage": "signal", "reason": "signal_generated", "instrument": s["instrument"], "session": s["bar_end"][:10],
                     "tod_bucket": tod_bucket(s["bar_end"]), "setup": s["setup"], "direction": s["direction"]})
    for t in trades:
        rows.append({"engine": engine, "stage": "trade", "reason": "trade_executed", "instrument": t["instrument"], "session": t["date"], "tod_bucket": tod_bucket(t["entry_ts"].isoformat()),
                     "setup": t["setup"], "direction": t["direction"]})
    reconciles = (n_sig == len(risk) + len(execr) + n_trades)
    return {
        "engine": engine, "definition": "candidate = a setup that reached its trigger bar; first_failed sums to the stage's rejected count, any_failed overlaps",
        "candidates_generated": candidates,
        "stages": [
            {"stage": "strategy_conditions", "entered": candidates, "rejected": len(strat), "passed": n_sig, "first_failed": table(strat, "first"), "any_failed": table(strat, "any")},
            {"stage": "risk_gates", "entered": n_sig, "rejected": len(risk), "first_failed": table(risk, "first"), "any_failed": table(risk, "any")},
            {"stage": "execution", "entered": n_sig - len(risk), "rejected": len(execr), "first_failed": table(execr, "first")},
        ],
        "trades_executed": n_trades, "reconciles": reconciles, "reconciliation": f"signals {n_sig} = risk-rejected {len(risk)} + execution-rejected {len(execr)} + trades {n_trades}",
        "pattern_stages": stage_counts or {}, "rows": rows,
    }


def filter_rows(rows: list[dict], *, instrument: str | None = None, tod_bucket_: str | None = None, setup: str | None = None) -> list[dict]:
    return [r for r in rows if (instrument is None or r["instrument"] == instrument) and (tod_bucket_ is None or r["tod_bucket"] == tod_bucket_) and (setup is None or r["setup"] == setup)]
