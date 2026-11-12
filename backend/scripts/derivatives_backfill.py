"""Dhan derivatives: master refresh, inventory, request estimates and resumable backfill jobs (run outside the API process).

    python scripts/derivatives_backfill.py refresh-master
    python scripts/derivatives_backfill.py inventory
    python scripts/derivatives_backfill.py plan futures --exchange NSE            # estimate only
    python scripts/derivatives_backfill.py run futures --exchange NSE             # create + run a job
    python scripts/derivatives_backfill.py run chains --priority NIFTY BANKNIFTY
    python scripts/derivatives_backfill.py run rolling --underlying NIFTY --days 10 --max-offset 2
    python scripts/derivatives_backfill.py resume <job_id>
    python scripts/derivatives_backfill.py coverage
"""
import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import engine  # noqa: E402
from app.models import BrokerCredential, DerivativeIngestionJob  # noqa: E402
from app.services import dhan_derivatives as dd  # noqa: E402
from app.services import derivatives_ingest as di  # noqa: E402


def sf() -> Session:
    return Session(engine)


def dhan_user() -> UUID:
    with sf() as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if cred is None:
            raise SystemExit("no Dhan connection stored")
        return cred.user_id


def build(kind: str, a: argparse.Namespace) -> tuple[list[di.TaskSpec], dict]:
    scope = {k: v for k, v in dict(exchange=a.exchange, segment=a.segment, instrument_type=a.instrument_type, underlying=a.underlying, category=getattr(a, "category", None), limit=a.limit).items() if v}
    now = di.utcnow()
    with sf() as db:
        if kind == "futures":
            return di.plan_futures(db, scope, lookback_days=a.days, daily=not a.no_daily, now=now), scope
        if kind == "option-candles":
            if a.underlying:
                return di.plan_option_candles_near_atm(db, a.underlying, exchange=a.exchange or "NSE", half_width=a.half_width, lookback_days=a.days), scope
            return di.plan_option_candles(db, scope, lookback_days=a.days), scope
        if kind == "rolling":
            return di.plan_rolling(db, scope, first=(now - timedelta(days=a.days)).date(), last=(now - timedelta(days=1)).date(), max_offset=a.max_offset, expiry_codes=tuple(a.codes)), scope
        if kind == "chains":
            return di.plan_chains(db, priority_underlyings=a.priority or (), expiry_count=a.expiries, limit=a.limit), scope
    raise SystemExit(f"unknown kind {kind}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh-master")
    sub.add_parser("inventory")
    sub.add_parser("coverage")
    rs = sub.add_parser("resume")
    rs.add_argument("job_id")
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["futures", "option-candles", "rolling", "chains"])
        p.add_argument("--exchange")
        p.add_argument("--segment")
        p.add_argument("--instrument-type", dest="instrument_type")
        p.add_argument("--underlying")
        p.add_argument("--category")
        p.add_argument("--limit", type=int, default=2000)
        p.add_argument("--days", type=int, default=89)
        p.add_argument("--no-daily", action="store_true")
        p.add_argument("--max-offset", type=int, default=2)
        p.add_argument("--codes", type=int, nargs="+", default=[1])
        p.add_argument("--priority", nargs="*")
        p.add_argument("--expiries", type=int, default=1)
        p.add_argument("--half-width", type=int, default=5)
    a = ap.parse_args()
    if a.cmd == "refresh-master":
        with sf() as db:
            print(json.dumps(di.refresh_master(db, dd.parse_master(dd.fetch_master_lines())), indent=1))
    elif a.cmd == "inventory":
        with sf() as db:
            print(json.dumps(di.inventory(db), indent=1, default=str))
    elif a.cmd == "coverage":
        with sf() as db:
            print("coverage rows:", di.refresh_coverage(db))
    elif a.cmd == "plan":
        tasks, scope = build(a.kind, a)
        print(json.dumps({"scope": scope, **di.estimate_requests(tasks)}, indent=1))
    elif a.cmd == "run":
        tasks, scope = build(a.kind, a)
        est = di.estimate_requests(tasks)
        print("estimate:", json.dumps(est))
        with sf() as db:
            job = di.create_job(db, f"{a.kind}_backfill", {"scope": scope, "args": {k: v for k, v in vars(a).items() if k not in ("cmd",)}, "estimate": est}, tasks, user_id=dhan_user())
            job_id = job.id
        _run(job_id)
    elif a.cmd == "resume":
        _run(UUID(a.job_id))


def _run(job_id: UUID) -> None:
    client = di.RenewingClient(sf, dhan_user())
    t = time.time()
    status = di.run_job(sf, job_id, client, sleep=time.sleep)
    with sf() as db:
        job = db.get(DerivativeIngestionJob, job_id)
        print(json.dumps({"job": str(job_id), "status": status, "tasks_total": job.tasks_total, "tasks_done": job.tasks_done, "tasks_failed": job.tasks_failed, "rows_written": job.rows_written,
                          "seconds": round(time.time() - t), "error": job.error}))
        di.refresh_coverage(db)


if __name__ == "__main__":
    main()
