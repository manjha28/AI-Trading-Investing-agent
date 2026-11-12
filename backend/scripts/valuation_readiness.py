"""Report which stored instruments can receive a production valuation score, and exactly why the rest cannot. Read-only.

    cd backend && python scripts/valuation_readiness.py
"""
import collections
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Instrument  # noqa: E402
from app.services import research_pipeline as rp, research_scoring as rs, valuation as v  # noqa: E402

now = datetime.now(timezone.utc)
rows, reasons, drift = [], collections.Counter(), []
with Session(engine) as db:
    for inst in db.scalars(select(Instrument).where(Instrument.country == "IN", Instrument.is_synthetic.is_(False), Instrument.asset_class == "equity").order_by(Instrument.symbol)):
        q: list = []
        fund, periods = rp._fundamentals_section(db, inst.id, q)
        tech = rp._technicals_section(db, inst.id, q)
        if not periods and not tech.get("latest_close"):
            continue
        trend = rp._financial_trend_section(periods, q)
        sections = {"fundamentals": fund, "technicals": tech, "financials_trend": trend, "news_sentiment": None}
        sections["valuation"] = v.build_valuation(db, inst, sections, now)
        a = rs.build_assessment(sections, instrument_id=str(inst.id), now=now)
        comp = a["components"]["valuation"]
        val = sections["valuation"]
        shown = sum(1 for m in val["metrics"].values() if m["status"] in ("calculated", "reported"))
        origin = collections.Counter(m["origin"] for m in val["metrics"].values() if m["origin"])
        reason = (comp["blockers"] or ["scored"])[0]
        reasons[reason.split(" (")[0][:90]] += 1
        # equivalence with the previous model: horizons computed with the old weights over the same four components
        old = {k: c for k, c in a["components"].items() if k != "valuation"}
        for h, w in rs.HORIZON_WEIGHTS_V0_1_0.items():
            scored = {k: c for k, c in old.items() if c["status"] == "scored"}
            sw = sum(w[k] for k in scored)
            if sw >= rs.MIN_SCORED_WEIGHT and all(r in scored for r in rs.REQUIRED_COMPONENTS[h]) and comp["status"] != "scored":
                s_old = sum(w[k] * scored[k]["score"] for k in scored) / sw
                c_old = sum(w[k] * scored[k]["confidence"] for k in scored)
                new = a["horizons"][h]
                if new["score"] is None or abs(new["score"] - s_old) > 0.11 or abs(new["confidence"] - c_old) > 0.11:
                    drift.append((inst.symbol, h, s_old, new["score"], c_old, new["confidence"]))
        rows.append((inst.symbol, tech["status"], shown, dict(origin), comp["status"], (val["peer_comparison"] or {}).get("peer_group"), reason[:100], a["horizons"]["positional"]["score"]))
print(f"{'symbol':11s}{'price':10s}{'metrics':>8s}  origins{'':26s} valuation   peer group")
for r in rows:
    print(f"{r[0]:11s}{r[1]:10s}{r[2]:>8d}  {str(r[3]):34s} {r[4]:10s}  {r[5]}  | {r[6]}")
print("\nvaluation scored:", sum(1 for r in rows if r[4] == 'scored'), "of", len(rows))
print("blocking reasons:", dict(reasons))
print("score/confidence drift vs the previous 4-component model (must be empty):", drift or "none")
