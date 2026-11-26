"""Download Dhan index and expired-option 1-minute data for the index option studies (scalp and short straddle). Market-data calls only: no order endpoint.

Needs DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in backend/.env, diagnostics/dhan.env or diagnostics/e2e-fixes/e2e.env. Resumable: one file per series and month under data/index_options/,
a month already on disk is skipped. Run from backend/:  python -m app.equity_lab.index_options_fetch [--from 2021-04-01] [--check]
"""
from __future__ import annotations

import argparse
import sys
import time as time_module
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
    for path in (ROOT / "backend" / ".env", ROOT / "diagnostics" / "dhan.env", ROOT / "diagnostics" / "e2e-fixes" / "e2e.env"):
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


COLUMNS = ("open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike")
# Most needed first, so partial results are usable: (underlying, strike offsets either side of the money)
PLAN = [("NIFTY", 0, 4), ("SENSEX", 0, 4), ("BANKNIFTY", 0, 2), ("NIFTY", 5, 10), ("SENSEX", 5, 10)]


def frame(rows: list):
    """Candles as a small table (float32, UTC seconds): this machine has little disk, and research statistics may use floats."""
    import numpy as np
    import pandas as pd

    data = {"ts": np.array([int(r.ts.timestamp()) for r in rows], dtype="int64")}
    for name in COLUMNS:
        values = [getattr(r, "open_interest" if name == "oi" else name, None) for r in rows]
        if any(v is not None for v in values):
            data[name] = np.array([np.nan if v is None else float(v) for v in values], dtype="float32")
    return pd.DataFrame(data)


def save(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame(rows).to_pickle(tmp, compression="gzip")
    tmp.replace(path)                                      # a partly written month can never look finished


def with_patience(call):
    """Dhan's data limit is shared with the live app: wait and retry when it says slow down."""
    for pause in (30, 120, 600, None):
        try:
            return call()
        except Exception as error:  # noqa: BLE001
            limited = "429" in str(error) or "rate" in str(error).lower() or "DH-904" in str(error)
            if not limited or pause is None:
                raise
            time_module.sleep(pause)


def main() -> None:
    from app.broker_adapters import BrokerAdapterError, DhanAdapter
    from app.services.dhan_derivatives import DhanDerivativesClient, fetch_master_lines, parse_master, rolling_windows

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="first", type=date.fromisoformat, default=FIRST)
    parser.add_argument("--check", action="store_true", help="one small request, to prove the token works")
    args = parser.parse_args()
    tokens = credentials()
    if tokens is None:
        sys.exit("No Dhan access token: add DHAN_CLIENT_ID= and DHAN_ACCESS_TOKEN= to backend/.env (and save the file).")
    client = DhanDerivativesClient(DhanAdapter(), tokens)
    last = datetime.now(IST).date() - timedelta(days=1)

    def instant(day: date) -> datetime:
        return datetime.combine(day, time(0), IST).astimezone(timezone.utc)

    def candles(symbol: str, begin: date, end: date) -> list:
        return list(client.intraday(segment="IDX_I", security_id=INDEX[symbol], instrument="INDEX", oi=False, start=instant(begin), end=instant(end + timedelta(days=1))))

    def option(underlying: str, offset: str, side: str, begin: date, end: date) -> list:
        segment, flag = next((s, f) for u, s, f in OPTIONS if u == underlying)
        return [row for a, z in rolling_windows(begin, end) for row in with_patience(lambda: client.rolling(
            segment=segment, cash_security_id=INDEX[underlying], instrument="OPTIDX", expiry_flag=flag, expiry_code=1, strike=offset, option_type=side, first=a, last=z))]

    if args.check:
        print("index candles in the last week:", len(candles("NIFTY", last - timedelta(days=6), last)))
        print("option rows for one day:", len(option("NIFTY", "ATM", "CE", last - timedelta(days=1), last)))
        return

    def fetch(path: Path, loader) -> None:
        if path.exists() and path.stat().st_size:
            return
        try:
            save(path, with_patience(loader))
            print("saved", path.relative_to(OUT), flush=True)
        except BrokerAdapterError as error:                # recorded and skipped; running again retries it
            if "401" in str(error) or "token" in str(error).lower():
                sys.exit("Dhan refused the token. Put a fresh one in the env file and run again.")
            print("failed", path.relative_to(OUT), str(error)[:140], flush=True)

    # index and VIX first, then the current futures (active contracts only: expired futures minute data does not exist)
    for begin, end in months(args.first, last):
        for symbol in INDEX:
            fetch(OUT / "index" / symbol / f"{begin:%Y-%m}.pkl.gz", lambda s=symbol, b=begin, e=end: candles(s, b, e))
    for c in parse_master(fetch_master_lines()).contracts:
        if c.instrument_type == "FUTIDX" and c.underlying_symbol in ("NIFTY", "BANKNIFTY", "SENSEX"):
            fetch(OUT / "futures" / c.underlying_symbol / f"{c.expiry:%Y-%m-%d}.pkl.gz", lambda c=c: list(client.intraday(
                segment=c.segment, security_id=c.security_id, instrument="FUTIDX", start=instant(last - timedelta(days=88)), end=instant(last + timedelta(days=1)))))
    for underlying, near, far in PLAN:
        offsets = [o for n in range(near, far + 1) for o in (["ATM"] if n == 0 else [f"ATM+{n}", f"ATM-{n}"])]
        flag = next(f for u, _, f in OPTIONS if u == underlying)
        for begin, end in months(args.first, last):        # newest months first
            for offset in offsets:
                for side in ("CE", "PE"):
                    fetch(OUT / "options" / underlying / flag / f"{offset}_{side}" / f"{begin:%Y-%m}.pkl.gz",
                          lambda u=underlying, o=offset, s=side, b=begin, e=end: option(u, o, s, b, e))
    print("download finished", flush=True)


if __name__ == "__main__":
    main()
