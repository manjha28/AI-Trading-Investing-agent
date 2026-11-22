"""Resumable Yahoo statements backfill for research-universe stocks that have no (or stale) annual statements. One request every --pause seconds, exponential backoff on rate limits,
a 30-minute rest after repeated limits, then it resumes; safe to stop and restart (the queue is recomputed from stored statements and fundamentals_fetch_log).
Run from backend/:  python scripts/fundamentals_backfill.py [--pause 4] [--limit N] [--dry-run]"""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, ".")
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import Instrument, InstrumentFundamental, User  # noqa: E402
from app.research_daily import user_universe  # noqa: E402
from app.services import fundamentals_backfill as fb  # noqa: E402


def universe(db: Session) -> list[Instrument]:
    seen, out = set(), []
    for uid in db.scalars(select(User.id).where(User.email_verified.is_(True))):
        for inst in user_universe(db, uid):
            if inst.id not in seen:
                seen.add(inst.id)
                out.append(inst)
    return out


def coverage(db: Session, insts: list[Instrument]) -> dict:
    ann = {r[0]: r[1] for r in db.execute(select(InstrumentFundamental.instrument_id, func.count().filter(InstrumentFundamental.period_type == "annual")).group_by(InstrumentFundamental.instrument_id))}
    c = Counter("0" if ann.get(i.id, 0) == 0 else "1-2" if ann[i.id] <= 2 else "3+" for i in insts)
    return {"universe": len(insts), "annual_periods_0": c["0"], "annual_periods_1_2": c["1-2"], "annual_periods_3_plus": c["3+"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=4.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with Session(engine) as db:
        insts = universe(db)
        before = coverage(db, insts)
        queue = fb.candidates(db, insts, limit=args.limit)
    print("coverage before:", before, "| queue:", len(queue), dict(Counter(k for _, k in queue)), flush=True)
    if args.dry_run:
        return 0
    started = datetime.now(timezone.utc)
    summary = fb.run_backfill(lambda: Session(engine), universe, pause=args.pause, limit=args.limit,
                              on_progress=lambda p: print(f"{p['symbol']}: {p['outcome']}  (refreshed {p['refreshed']}, no_statements {p['no_statements']}, rate_limited {p['rate_limited']}, failed {p['failed']}, cooloffs {p['cooloffs']})", flush=True))
    with Session(engine) as db:
        after = coverage(db, universe(db))
    out = {"started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(), "before": before, "after": after, "summary": summary}
    json.dump(out, open("../diagnostics/nexus-v2/fundamentals_backfill_result.json", "w"), indent=1)
    print("coverage after:", after, "| summary:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
