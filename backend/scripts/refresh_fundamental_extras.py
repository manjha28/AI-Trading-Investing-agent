"""One-time refresh of Yahoo statements for stocks that already have annual rows but no extra_metrics (interest expense, pretax income, EBIT, fixed assets, cash), which the Phase 6 steps added to the
capture after most rows were stored. Same provider, same breaker and same fetch log as the backfill (rate-limited stocks are logged and retried by the normal queue). Run from backend/."""
import sys
import time

sys.path.insert(0, ".")
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import yahoo_guard  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import InstrumentFundamental, User  # noqa: E402
from app.research_daily import user_universe  # noqa: E402
from app.services import fundamentals_backfill as fb  # noqa: E402

PAUSE = 4.0
counts = {"refreshed": 0, "no_statements": 0, "rate_limited": 0, "failed": 0}
with Session(engine) as db:
    uid = db.scalar(select(User.id).where(User.email_verified.is_(True)))
    todo = []
    for inst in user_universe(db, uid):
        have = db.scalar(select(func.count()).select_from(InstrumentFundamental).where(InstrumentFundamental.instrument_id == inst.id, InstrumentFundamental.period_type == "annual"))
        withx = db.scalar(select(func.count()).select_from(InstrumentFundamental).where(InstrumentFundamental.instrument_id == inst.id, InstrumentFundamental.extra_metrics.is_not(None)))
        if have and not withx:
            todo.append(inst.id)
print(len(todo), "stocks to refresh", flush=True)
streak = 0
for k, iid in enumerate(todo):
    with Session(engine) as db:
        from app.models import Instrument
        inst = db.get(Instrument, iid)
        if not yahoo_guard.allowed():
            time.sleep(1800)
            yahoo_guard.reset()
        out = fb.fetch_one(db, inst, origin="extras_refresh")
    counts[out] += 1
    streak = streak + 1 if out == "rate_limited" else 0
    if streak >= 3:
        time.sleep(1800)
        yahoo_guard.reset()
        streak = 0
    else:
        time.sleep(PAUSE if out != "rate_limited" else min(300, 8 * 2 ** (streak - 1)))
    if k % 20 == 0:
        print(k, len(todo), counts, flush=True)
print("done", counts)
