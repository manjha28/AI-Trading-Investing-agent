"""Seed day-1 shadow records: run the v2 decision (Gate -> Quality -> Timing -> Rank) over each research-universe stock's LATEST stored brief and store it beside v1 in pipeline_v2_shadow
(exactly what every research run now records on its own). Idempotent per run: skips a stock whose latest shadow row is newer than its latest brief. Run from backend/:  python scripts/pipeline_v2_shadow_seed.py"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import User  # noqa: E402
from app.models.pipeline_v2 import PipelineV2Shadow  # noqa: E402
from app.research_daily import user_universe  # noqa: E402
from app.services import pipeline_v2, research_pipeline as rp  # noqa: E402

done = skipped = failed = 0
with Session(engine) as db:
    uid = db.scalar(select(User.id).where(User.email_verified.is_(True)))
    bench = rp._benchmark_series(db)
    for inst in user_universe(db, uid):
        latest = rp.latest_brief(db, uid, inst.id)
        if latest is None:
            continue
        run, brief = latest
        prev = db.scalar(select(PipelineV2Shadow.computed_at).where(PipelineV2Shadow.instrument_id == inst.id, PipelineV2Shadow.user_id == uid).order_by(PipelineV2Shadow.computed_at.desc()).limit(1))
        if prev is not None and prev > brief.created_at:
            skipped += 1
            continue
        try:
            daily = rp._daily_frame(db, inst.id, (brief.sections or {}).get("technicals", {}).get("source"))
            ok = pipeline_v2.record_shadow(db, uid, inst, brief, daily, bench, datetime.now(timezone.utc))
            done += int(ok)
            failed += int(not ok)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed += 1
            print("failed", inst.symbol, type(exc).__name__, flush=True)
        if (done + failed) % 25 == 0:
            print(f"{done} recorded, {failed} failed", flush=True)
print(f"done: {done} recorded, {skipped} skipped, {failed} failed")
