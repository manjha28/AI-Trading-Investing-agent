"""Populate the valuation inputs the engine reads: Dhan Company Info for the tracked NSE universe (sector peers) and Yahoo Finance for the subjects.

    python scripts/refresh_valuation_inputs.py --dhan-universe [--limit N] [--max-age-hours 12]
    python scripts/refresh_valuation_inputs.py --yahoo-subjects

Reuses the app's Dhan credentials and throttle (>= 1 s between Dhan data requests). Credentials are never printed or stored by this script.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.api.market_data import _market_data_credentials  # noqa: E402
from app.db import engine  # noqa: E402
from app.models import BrokerCredential, Instrument  # noqa: E402
from app.services import peer_universe, research_pipeline as rp  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dhan-universe", action="store_true")
    parser.add_argument("--yahoo-subjects", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-age-hours", type=float, default=12)
    args = parser.parse_args()
    with Session(engine) as db:
        if args.dhan_universe:
            credential = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
            if credential is None:
                print("no Dhan credential stored")
                return 2
            _, provider, _ = _market_data_credentials(db, credential.user_id, "dhan")
            if provider is None:
                print("no authenticated Dhan connection")
                return 2
            summary = peer_universe.refresh_dhan_universe(db, provider.adapter, provider.tokens["access_token"], provider.tokens, max_age_hours=args.max_age_hours,
                                                          limit=args.limit, log=print)
            print({k: v for k, v in summary.items() if k != "failures"}, "failures:", summary["failures"][:5])
            sizes = peer_universe.peer_group_sizes(db)
            big = {k: v for k, v in sorted(sizes["industry"].items(), key=lambda kv: -kv[1])[:15]}
            print("industries with the most symbols:", big)
            print("industries with >= 6 symbols:", sum(1 for v in sizes["industry"].values() if v >= 6), "of", len(sizes["industry"]), "| sectors:", len(sizes["sector"]))
        if args.yahoo_subjects:
            now = datetime.now(timezone.utc)
            done = 0
            for inst in db.scalars(select(Instrument).where(Instrument.country == "IN", Instrument.is_synthetic.is_(False), Instrument.asset_class == "equity").order_by(Instrument.symbol)):
                q: list = []
                _f, periods = rp._fundamentals_section(db, inst.id, q)
                if not periods:
                    continue
                rp._refresh_yahoo_valuation_inputs(db, inst, now)
                done += 1
            print("Yahoo valuation inputs refreshed for", done, "instruments with stored statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
