"""Download Dhan index and expired-option 1-minute data for the index option studies (scalp and short straddle). Market-data calls only: no order endpoint.

Needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in backend/.env or diagnostics/dhan.env. Resumable: one file per series and month under data/index_options/,
a month already on disk is skipped. Run from backend/:  python -m app.equity_lab.index_options_fetch [--from 2021-04-01] [--check]
"""
from __future__ import annotations

import argparse
import pickle
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "index_options"
IST = ZoneInfo("Asia/Kolkata")
FIRST = date(2021, 4, 1)                                   # measured: the rolling API has nothing earlier
INDEX = {"NIFTY": "13", "SENSEX": "51", "BANKNIFTY": "25", "INDIAVIX": "21"}
# (underlying, option segment, expiry flag). BANKNIFTY weeklies ended in Nov 2024, so the monthly contract is used throughout.
OPTIONS = [("NIFTY", "NSE_FNO", "WEEK"), ("SENSEX", "BSE_FNO", "WEEK"), ("BANKNIFTY", "NSE_FNO", "MONTH")]
MAX_OFFSET = 10


def credentials() -> dict[str, str] | None:
    values: dict[str, str] = {}
    for path in (ROOT / "backend" / ".env", ROOT / "diagnostics" / "dhan.env"):
        if path.is_file():
            for line in path.read_text().splitlines():
                key, sep, value = line.strip().removeprefix("export ").partition("=")
                if sep and key.strip() in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN") and value.strip():
                    values[key.strip()] = value.strip().strip('"').strip("'")
    if len(values) < 2:
        return None
    return {"access_token": values["DHAN_ACCESS_TOKEN"], "client_id": values["DHAN_CLIENT_ID"]}


def months(first: date, last: date) -> list[tuple[date, date]]:
    """Calendar months from ``first`` to ``last``, newest first, each clipped to the range."""
    out, cur = [], first
    while cur <= last:
        end = (cur.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        out.append((cur, min(end, last)))
        cur = end + timedelta(days=1)
    return out[::-1]


def save(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(rows))
    tmp.replace(path)                                      # a partly written month can never look finished


def main() -> None:
    from app.broker_adapters import BrokerAdapterError, DhanAdapter
    from app.services.dhan_derivatives import DhanDerivativesClient, rolling_windows, strike_offsets

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=FIRST)
    parser.add_argument("--check", action="store_true", help="one small request, to prove the token works")
    args = parser.parse_args()
    tokens = credentials()
    if tokens is None:
        sys.exit("No Dhan access token: add DHAN_CLIENT_ID= and DHAN_ACCESS_TOKEN= to backend/.env (and save the file).")
    client = DhanDerivativesClient(DhanAdapter(), tokens)
    last = datetime.now(IST).date() - timedelta(days=1)

    def candles(symbol: str, begin: date, end: date) -> list:
        return list(client.intraday(segment="IDX_I", security_id=INDEX[symbol], instrument="INDEX", oi=False,
                                    start=datetime.combine(begin, time(0), IST).astimezone(timezone.utc), end=datetime.combine(end + timedelta(days=1), time(0), IST).astimezone(timezone.utc)))

    if args.check:
        print("index candles in the last week:", len(candles("NIFTY", last - timedelta(days=6), last)))
        print("option rows for one day:", len(client.rolling(segment="NSE_FNO", cash_security_id=INDEX["NIFTY"], instrument="OPTIDX", expiry_flag="WEEK", expiry_code=1,
                                                             strike="ATM", option_type="CE", first=last - timedelta(days=1), last=last)))
        return

    def fetch(path: Path, loader) -> None:
        if path.exists() and path.stat().st_size:
            return
        try:
            save(path, loader())
            print("saved", path.relative_to(OUT), flush=True)
        except BrokerAdapterError as error:                # recorded and skipped; running again retries it
            if "401" in str(error) or "token" in str(error).lower():
                sys.exit("Dhan refused the token. Put a fresh one in backend/.env and run again.")
            print("failed", path.relative_to(OUT), str(error)[:140], flush=True)

    for begin, end in months(args.first, last):
        for symbol in INDEX:
            if end < last.replace(day=1) or not (OUT / "index" / symbol / f"{begin:%Y-%m}.pkl").exists():
                fetch(OUT / "index" / symbol / f"{begin:%Y-%m}.pkl", lambda s=symbol, b=begin, e=end: candles(s, b, e))
    for underlying, segment, flag in OPTIONS:
        for begin, end in months(args.first, last):
            for offset in strike_offsets(MAX_OFFSET):
                for side in ("CE", "PE"):
                    fetch(OUT / "options" / underlying / flag / f"{offset}_{side}" / f"{begin:%Y-%m}.pkl",
                          lambda u=underlying, s=segment, f=flag, o=offset, c=side, b=begin, e=end: [
                              row for a, z in rolling_windows(b, e)
                              for row in client.rolling(segment=s, cash_security_id=INDEX[u], instrument="OPTIDX", expiry_flag=f, expiry_code=1, strike=o, option_type=c, first=a, last=z)])


if __name__ == "__main__":
    main()
