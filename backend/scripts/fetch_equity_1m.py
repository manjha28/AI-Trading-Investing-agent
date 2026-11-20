"""Fetch missing NSE equity 1-minute bars through the app's own read-only machinery (instrument search upsert + rate-limit-aware 1m backfill).

Run from backend/:  python scripts/fetch_equity_1m.py SYMBOL [SYMBOL ...] --start 2026-01-01 --end 2026-11-17

Read-only market data only: it never places, modifies or cancels an order. Nothing is padded; whatever the provider does not return stays missing and is
reported with the recorded reason. Dhan serves 1-minute candles only up to 15:14 for many windows (provider_tail_not_served), so a fetched session may
still be too short for VAR; the post-fetch report states how many fetched sessions are complete."""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api import market_data as md  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import BrokerCredential, Instrument  # noqa: E402
from app.services import intraday_backfill  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def nse_instrument(db: Session, symbol: str) -> Instrument | None:
    return db.scalar(select(Instrument).where(Instrument.symbol == symbol, Instrument.asset_class == "equity", Instrument.venue == "NSE"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    a = ap.parse_args()
    start = datetime.combine(datetime.fromisoformat(a.start).date(), dtime(3, 45), tzinfo=timezone.utc)      # 09:15 IST
    end = datetime.combine(datetime.fromisoformat(a.end).date(), dtime(10, 30), tzinfo=timezone.utc)         # 16:00 IST
    eng = create_engine(settings.database_url)
    report: dict[str, dict] = {}
    with Session(eng) as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        if cred is None:
            sys.exit("no connected Dhan credential: cannot fetch")
        user = cred.user if hasattr(cred, "user") else None
        user_id = cred.user_id
        from app.models import User
        user = db.get(User, user_id)
        provider = md.market_data_provider_for_user(db, user_id)
        ids = []
        for sym in a.symbols:
            inst = nse_instrument(db, sym)
            if inst is None:
                try:
                    md.search_instruments(query=sym, limit=20, provider=provider, _user=user, db=db)   # the app's own search + upsert path
                except Exception as e:  # noqa: BLE001 - recorded per symbol
                    report[sym] = {"status": "instrument_search_failed", "error": f"{type(e).__name__}: {getattr(e, 'detail', e)}"}
                    db.rollback()
                    continue
                inst = nse_instrument(db, sym)
            if inst is None:
                report[sym] = {"status": "no_nse_equity_instrument_found_by_provider_search"}
                continue
            has_dhan = any(m.provider == "dhan" and m.is_verified for m in inst.provider_identifiers)
            if not has_dhan:
                report[sym] = {"status": "instrument_has_no_verified_dhan_identifier"}
                continue
            ids.append(inst.id)
            report[sym] = {"status": "queued", "instrument_id": str(inst.id)}
        if ids:
            insts = [db.get(Instrument, i) for i in ids]
            job = intraday_backfill.new_job(user_id, insts, start, end)
            with md._history_sync_jobs_lock:
                md._history_sync_jobs[job["id"]] = job
                md._history_sync_jobs_in_flight[(user_id, intraday_backfill.BATCH_KEY)] = job["id"]
            t0 = time.time()
            intraday_backfill.run_batch(job["id"], user_id, ids, start, end)
            snap = md._history_sync_jobs[job["id"]]
            for row in snap["instruments"]:
                report[row["symbol"]] = {"status": row["status"], "bars_fetched": row["bars_fetched"], "reason": row["reason"], "attempts": row["attempts"],
                                         "after_missing_pct": (row.get("after") or {}).get("missing_pct"), "unrecoverable_gaps": len(row.get("unrecoverable") or [])}
            print(f"backfill finished in {round(time.time() - t0)} s, job status {snap['status']}", flush=True)
        # completeness of what is now stored, in the IST regular session
        for sym in a.symbols:
            r = db.execute(text("""with s as (select (b.timestamp at time zone 'Asia/Kolkata')::date d, count(*) n from market_bars b join instruments i on i.id = b.instrument_id
                where i.symbol = :s and i.venue = 'NSE' and i.asset_class = 'equity' and b.interval = '1m' and b.source = 'dhan'
                and b.timestamp >= :a and b.timestamp < :b group by 1)
                select count(*), count(*) filter (where n >= 370), count(*) filter (where n between 355 and 369), min(d), max(d) from s"""),
                {"s": sym, "a": start, "b": end}).first()
            report.setdefault(sym, {})["stored_after"] = {"sessions": r[0], "full_sessions_ge_370_bars": r[1], "truncated_sessions": r[2], "first": str(r[0] and r[3]), "last": str(r[0] and r[4])}
    for sym, v in report.items():
        print(sym, v, flush=True)
    _ = func


if __name__ == "__main__":
    main()
