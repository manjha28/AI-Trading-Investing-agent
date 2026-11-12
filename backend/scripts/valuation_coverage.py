"""Valuation data coverage for every stored Indian equity row: which metrics have a value, from which source, and why the rest do not.
Read-only. Usage (from backend/):

    python scripts/valuation_coverage.py --out ../diagnostics/valuation_coverage_after.json
    python scripts/valuation_coverage.py --module /path/to/old_valuation.py --out ../diagnostics/valuation_coverage_before.json   # baseline
"""
import argparse
import importlib.util
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Instrument  # noqa: E402
from app.services import research_pipeline as rp  # noqa: E402

KEYS = ["market_cap", "enterprise_value", "pe_ttm", "pb", "ev_ebitda", "price_to_sales", "peg", "dividend_yield", "book_value_per_share", "net_debt_ebitda", "roe", "roce", "interest_coverage"]


def load_module(path: str | None):
    if not path:
        from app.services import valuation
        return valuation
    spec = importlib.util.spec_from_file_location("valuation_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module")
    parser.add_argument("--out")
    args = parser.parse_args()
    valuation = load_module(args.module)
    now = datetime.now(timezone.utc)
    rows = []
    with Session(engine) as db:
        for inst in db.scalars(select(Instrument).where(Instrument.country == "IN", Instrument.is_synthetic.is_(False), Instrument.asset_class == "equity").order_by(Instrument.symbol, Instrument.id)):
            q: list = []
            fund, periods = rp._fundamentals_section(db, inst.id, q)
            tech = rp._technicals_section(db, inst.id, q)
            if not periods and not tech.get("latest_close"):
                continue
            sections = {"fundamentals": fund, "technicals": tech, "financials_trend": rp._financial_trend_section(periods, q), "news_sentiment": None}
            val = valuation.build_valuation(db, inst, sections, now)
            metrics = {}
            for key in KEYS:
                m = val["metrics"][key]
                metrics[key] = {"status": m["status"], "value": m["value"], "origin": m.get("origin"), "provider": m.get("provider"), "reason": m.get("reason"),
                                "chain": [f"{c['provider']}:{c['outcome']}" for c in m.get("source_chain", [])], "discrepancies": m.get("discrepancies", [])}
            for key, m in (val.get("supporting_metrics") or {}).items():
                metrics[key] = {"status": m["status"], "value": m["value"], "origin": m.get("origin"), "provider": m.get("provider"), "reason": m.get("reason"),
                                "chain": [f"{c['provider']}:{c['outcome']}" for c in m.get("source_chain", [])], "discrepancies": m.get("discrepancies", [])}
            peers = val.get("peer_comparison") or {}
            scoring = val.get("scoring") or {}
            rows.append({"symbol": inst.symbol, "instrument_id": str(inst.id), "price_status": tech["status"], "metrics": metrics, "peer_status": peers.get("status"),
                         "peer_group": peers.get("peer_group"), "peer_counts": {k: v.get("peer_count") for k, v in (peers.get("metrics") or {}).items()},
                         "scoring_status": scoring.get("status"), "scoring_blockers": scoring.get("blockers", [])[:2]})
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({"generated_at": now.isoformat(), "module": getattr(valuation, "MODEL_VERSION", None), "rows": rows}, indent=1, default=str))
    have = lambda m: m["status"] in ("calculated", "reported")  # noqa: E731
    print(f"{'symbol':11s}{'price':10s}" + "".join(f"{k[:7]:>8s}" for k in KEYS) + "  scoring")
    for r in rows:
        cells = "".join(f"{('R-' + (r['metrics'][k]['provider'] or '?')[:4] if r['metrics'][k]['origin'] == 'provider_reported' else 'calc' if have(r['metrics'][k]) else '-'):>8s}" for k in KEYS)
        print(f"{r['symbol']:11s}{r['price_status']:10s}{cells}  {r['scoring_status']}")
    print("\ncoverage (instruments with a value):", {k: sum(1 for r in rows if have(r["metrics"][k])) for k in KEYS}, "of", len(rows))
    print("origin counts:", dict(Counter((m["provider"] or m["origin"]) for r in rows for m in r["metrics"].values() if have(m))))
    print("scored:", sum(1 for r in rows if r["scoring_status"] == "scored"))


if __name__ == "__main__":
    main()
