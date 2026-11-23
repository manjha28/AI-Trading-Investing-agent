"""Backfill delivery % (NSE full bhavcopy, DELIV_PER) for a date range. Writes ALL equity symbols to a pickle for the research replays (--pickle) and the tracked symbols to the delivery_percentages table (--db). Polite: one
request at a time with a pause; weekends and holidays (no file) are skipped. Run from backend/:  python scripts/backfill_delivery.py --from 2019-01-01 --pickle ../diagnostics/nexus-v2/delivery_panel.pkl [--db]"""
import argparse
import pickle
import sys
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")
import pandas as pd  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.services import delivery_ingest as di  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--from", dest="start", default="2019-01-01")
ap.add_argument("--pickle")
ap.add_argument("--db", action="store_true")
ap.add_argument("--pause", type=float, default=0.25)
a = ap.parse_args()
d, end = datetime.fromisoformat(a.start).date(), date.today()
frames, got, none, failed = [], 0, 0, []
syms = None
with Session(engine) as db:
    syms = di.tracked_symbols(db) if a.db else set()
    while d <= end:
        if d.weekday() < 5:
            try:
                rows = di.fetch_day(d)
            except di.FetchError:
                failed.append(d)
                d += timedelta(days=1)
                continue
            if rows is None:
                none += 1
            else:
                got += 1
                frames.append(pd.DataFrame(rows, columns=["symbol", "series", "deliv_per"]).assign(date=pd.Timestamp(d)))
                if a.db:
                    di.store(db, d, rows, syms)
            time.sleep(a.pause)
            if (got + none) % 100 == 0:
                print(d, "files", got, "no file", none, flush=True)
        d += timedelta(days=1)
for d in failed:                                           # one more pass over the days that errored (network); never treated as holidays
    try:
        rows = di.fetch_day(d, retries=5)
    except di.FetchError:
        print("STILL FAILED", d)
        continue
    if rows is not None:
        got += 1
        frames.append(pd.DataFrame(rows, columns=["symbol", "series", "deliv_per"]).assign(date=pd.Timestamp(d)))
df = pd.concat(frames, ignore_index=True)
df["deliv_per"] = df["deliv_per"].astype(float)
if a.pickle:
    pickle.dump(df, open(a.pickle, "wb"))
print("done", got, "files,", none, "without file,", len(df), "rows")
