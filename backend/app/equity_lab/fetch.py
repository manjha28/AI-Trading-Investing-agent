"""Download Dhan 1-minute history for the Market watch NSE stocks and keep it as 5-minute bars (research only, READ-ONLY Dhan data API).

Run on the API machine (the Dhan token never leaves it):  python -m app.equity_lab.fetch /tmp/eq5m 2025-11-01
Writes one compressed .npz per stock: t (bar open, epoch s), o, h, l, c, v (float32), 09:15-15:30 IST sessions only. Paced by the adapter
(one Dhan market-data call a second). Resumable: stocks already written are skipped."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.broker_adapters import DhanAdapter
from app.db import engine
from app.models import BrokerCredential, Instrument, InstrumentProviderIdentifier

IST = timezone(timedelta(hours=5, minutes=30))


def main(out: str, since: str) -> None:
    from app.api.brokers import decrypt_tokens
    dest = Path(out)
    dest.mkdir(parents=True, exist_ok=True)
    with Session(engine) as db:
        cred = db.scalar(select(BrokerCredential).where(BrokerCredential.broker == "dhan"))
        tokens = decrypt_tokens(cred.encrypted_tokens)            # the stored read token as is: a refresh is the app's job, never this script's
        rows = db.execute(select(Instrument.symbol, InstrumentProviderIdentifier.provider_instrument_id).join(
            InstrumentProviderIdentifier, InstrumentProviderIdentifier.instrument_id == Instrument.id).where(
            InstrumentProviderIdentifier.provider == "dhan", InstrumentProviderIdentifier.is_verified.is_(True), Instrument.country == "IN",
            Instrument.is_synthetic.is_(False))).all()
    adapter = DhanAdapter()
    start0 = datetime.fromisoformat(since).replace(tzinfo=IST)
    end0 = datetime.now(IST)
    done = 0
    for symbol, pid in sorted(set(rows)):
        sec, _, seg = pid.partition("|")
        seg = seg or "NSE_EQ"
        f = dest / f"{symbol.replace('/', '_')}.npz"
        if f.exists() or seg != "NSE_EQ" or not sec.isdigit():
            continue
        frames, s = [], start0
        while s < end0:
            e = min(end0, s + timedelta(days=85))
            try:
                bars = adapter.get_historical_bars(tokens["access_token"], tokens, seg, sec, "1m", s, e)
                frames += [(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume or 0)) for b in bars]
            except Exception as error:  # noqa: BLE001 - one window failing must not stop the run; it is logged
                print(f"{symbol} {s:%Y-%m-%d} {type(error).__name__}: {str(error)[:80]}", flush=True)
            s = e
        if not frames:
            continue
        d = pd.DataFrame(frames, columns=["ts", "o", "h", "l", "c", "v"]).drop_duplicates("ts").sort_values("ts")
        d["t"] = pd.to_datetime(d.ts, utc=True).dt.tz_convert("Asia/Kolkata")
        d = d[(d.t.dt.time >= pd.Timestamp("09:15").time()) & (d.t.dt.time < pd.Timestamp("15:30").time())].set_index("t")
        r = d.resample("5min", label="left", closed="left").agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}).dropna()
        np.savez_compressed(f, t=(r.index.tz_convert("UTC").astype("int64") // 10**9).to_numpy(), **{k: r[k].to_numpy(np.float32) for k in "ohlcv"})
        done += 1
        if done % 25 == 0:
            print(f"{done} stocks written", flush=True)
    print(f"DONE {done} stocks", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "2025-11-01")
