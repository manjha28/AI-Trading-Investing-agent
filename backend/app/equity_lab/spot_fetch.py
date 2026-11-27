"""Download Dhan 5-minute bars for the index constituents used by the spot study (research only; market data calls, no order endpoint).

Constituents and weights are TODAY'S large names as recalled, applied to all history (survivorship bias; weights not verified).
SENSEX constituents are read from their NSE listings. Run from backend/:  python -m app.equity_lab.spot_fetch
"""
from __future__ import annotations

import csv
from datetime import date, datetime, time, timedelta, timezone

from app.equity_lab import index_options_common as c
from app.equity_lab.index_options_fetch import FIRST, IST, credentials, save, with_patience

OUT = c.ROOT / "data" / "spot_constituents"
NIFTY = {"HDFCBANK": 13, "ICICIBANK": 9, "RELIANCE": 8.5, "INFY": 5, "BHARTIARTL": 4.5, "LT": 4, "ITC": 3.5, "TCS": 3, "AXISBANK": 3, "KOTAKBANK": 3, "SBIN": 3,
         "M&M": 2.5, "HINDUNILVR": 2, "BAJFINANCE": 2, "SUNPHARMA": 1.7}
BANKNIFTY = {"HDFCBANK": 28, "ICICIBANK": 24, "SBIN": 10, "AXISBANK": 9, "KOTAKBANK": 9, "INDUSINDBK": 4, "BANKBARODA": 3, "FEDERALBNK": 2.5, "PNB": 2, "IDFCFIRSTB": 2, "AUBANK": 2, "CANBK": 2}
SENSEX = {**NIFTY, "HCLTECH": 1.8, "MARUTI": 1.8, "NTPC": 1.7, "ULTRACEMCO": 1.4, "TITAN": 1.4, "POWERGRID": 1.3, "TATASTEEL": 1.3, "ETERNAL": 1.8, "ASIANPAINT": 1.1,
          "ADANIPORTS": 1, "BEL": 1.3, "TECHM": 1, "BAJAJFINSV": 1, "TRENT": 1, "NESTLEIND": 0.9}
WEIGHTS = {"NIFTY": NIFTY, "BANKNIFTY": BANKNIFTY, "SENSEX": SENSEX}


def main() -> None:
    from app.broker_adapters import BrokerAdapterError, DhanAdapter
    from app.services.dhan_derivatives import DhanDerivativesClient, fetch_master_lines

    tokens = credentials()
    if tokens is None:
        raise SystemExit("No Dhan access token.")
    client = DhanDerivativesClient(DhanAdapter(), tokens)
    wanted = {s for w in WEIGHTS.values() for s in w}
    ids: dict[str, str] = {}
    for row in csv.DictReader(fetch_master_lines()):
        if row["INSTRUMENT"].strip() == "EQUITY" and row["EXCH_ID"].strip() == "NSE" and row.get("SERIES", "").strip() == "EQ" and row["UNDERLYING_SYMBOL"].strip() in wanted:
            ids[row["UNDERLYING_SYMBOL"].strip()] = row["SECURITY_ID"].strip()
    print("not in the Dhan master:", sorted(wanted - set(ids)), flush=True)
    last = datetime.now(IST).date() - timedelta(days=1)

    def instant(day: date) -> datetime:
        return datetime.combine(day, time(0), IST).astimezone(timezone.utc)

    for symbol, sid in sorted(ids.items()):
        path = OUT / f"{symbol}.pkl.gz"
        if path.exists():
            continue
        rows, begin = [], FIRST
        while begin <= last:
            end = min(begin + timedelta(days=88), last)
            try:
                rows += list(with_patience(lambda b=begin, e=end: client.intraday(segment="NSE_EQ", security_id=sid, instrument="EQUITY", oi=False, interval="5",
                                                                               start=instant(b), end=instant(e + timedelta(days=1)))))
            except BrokerAdapterError as error:
                print("failed", symbol, begin, str(error)[:120], flush=True)
            begin = end + timedelta(days=1)
        if rows:
            save(path, rows)
        print("saved", symbol, len(rows), flush=True)
    print("constituents finished", flush=True)


if __name__ == "__main__":
    main()
