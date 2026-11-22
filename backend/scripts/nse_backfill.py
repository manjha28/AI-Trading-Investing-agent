"""Backfill NSE filed results and shareholding patterns for the research universe (stocks only), politely (>= 2.2 s between requests, backoff, raw files cached with their download date). Resumable:
filings already stored are skipped and cached responses cost no request. Stops and logs if NSE blocks. Requires the owner's flags (nse_results_enabled / nse_shareholding_enabled) in .env.
Run from backend/:  python scripts/nse_backfill.py [--since 2023-01-01] [--quarters 8] [--symbols A,B] [--no-results] [--no-shareholding]"""
import argparse
import logging
import sys
from datetime import date

sys.path.insert(0, ".")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import Instrument, User  # noqa: E402
from app.research_daily import user_universe  # noqa: E402
from app.services import nse_data  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
ap = argparse.ArgumentParser()
ap.add_argument("--since", default="2023-01-01")
ap.add_argument("--quarters", type=int, default=8)
ap.add_argument("--symbols", default="")
ap.add_argument("--no-results", action="store_true")
ap.add_argument("--no-shareholding", action="store_true")
a = ap.parse_args()
do_res = settings.nse_results_enabled and not a.no_results
do_shp = settings.nse_shareholding_enabled and not a.no_shareholding
print("results:", do_res, "shareholding:", do_shp, flush=True)
from app.broker_adapters import DhanAdapter  # noqa: E402
from app.services import instrument_kind  # noqa: E402

DhanAdapter._scrip_master(force=False)          # load the Dhan master once so funds (ETFs, index and liquid funds) are skipped: they have no company filings
print("fund symbols known:", len(instrument_kind.fund_symbols()), flush=True)
client = nse_data.PoliteNse(settings.nse_raw_dir, min_interval=settings.nse_min_interval_seconds)
tot_r, tot_s = {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}, {"inserted": 0, "skipped": 0, "failed": 0}
with Session(engine) as db:
    uid = db.scalar(select(User.id).where(User.email_verified.is_(True)))
    insts = [i for i in db.scalars(select(Instrument).where(Instrument.symbol.in_([x.strip().upper() for x in a.symbols.split(",")]), Instrument.venue == "NSE"))] if a.symbols else user_universe(db, uid)
    try:
        for n, inst in enumerate(insts, 1):
            try:
                if do_res:
                    r = nse_data.sync_results(db, client, inst, since=date.fromisoformat(a.since))
                    for k in tot_r:
                        tot_r[k] += r[k]
                if do_shp:
                    r = nse_data.sync_shareholding(db, client, inst, max_quarters=a.quarters)
                    for k in tot_s:
                        tot_s[k] += r[k]
            except nse_data.NseBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one stock must not stop the backfill
                db.rollback()
                print("failed", inst.symbol, type(exc).__name__, str(exc)[:100], flush=True)
            if n % 10 == 0 or n == len(insts):
                print(f"{n}/{len(insts)} results {tot_r} shareholding {tot_s} requests {client.requests} cache_hits {client.cache_hits}", flush=True)
    except nse_data.NseBlocked as exc:
        print("STOPPED:", exc, flush=True)
    finally:
        client.close()
print("done", flush=True)
