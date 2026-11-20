"""Phase 1 data-coverage audit (read-only SQL). Writes diagnostics/phase1_coverage.json (replaced each run).

Run from backend/:  python scripts/phase1_coverage.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase1_coverage.json"
IST = "at time zone 'Asia/Kolkata'"


def rows(c, sql: str, p: dict | None = None) -> list[dict]:
    r = c.execute(text(sql), p or {})
    keys = list(r.keys())
    return [dict(zip(keys, x)) for x in r.fetchall()]


def main() -> None:
    e = create_engine(settings.database_url)
    out: dict = {}
    with e.connect() as c:
        out["futures_1m_by_type"] = rows(c, f"""select d.instrument_type, count(distinct d.underlying_symbol) underlyings, count(distinct d.id) contracts, count(*) bars,
            min(f.ts)::date first, max(f.ts)::date last, count(distinct (f.ts {IST})::date) sessions from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id group by 1 order by 1""")
        out["nse_equity_futures_sessions_per_underlying"] = rows(c, f"""with x as (select d.underlying_symbol u, count(distinct (f.ts {IST})::date) s from futures_candles_1m f join derivative_contracts d on d.id=f.contract_id
            where d.instrument_type in ('FUTIDX','FUTSTK') group by 1) select s sessions, count(*) underlyings from x group by 1 order by 1 desc""")
        # session shape: bars per session for the main near-month contract, and clipping beyond 15:30
        out["futures_session_shape_nifty_near_month"] = rows(c, f"""with x as (select (f.ts {IST})::date d, count(*) n, max((f.ts {IST})::time) last_t from futures_candles_1m f join derivative_contracts d on d.id=f.contract_id
            where d.trading_symbol = 'NIFTY-Nov2026-FUT' group by 1) select n bars, last_t, count(*) sessions from x group by 1, 2 order by 3 desc limit 5""")
        # zero-volume rate inside the regular session: the data-quality red flag
        out["futures_zero_volume_minutes_pct_inside_session"] = rows(c, f"""select d.underlying_symbol u, d.trading_symbol contract, round(100 * avg((f.volume = 0)::int)::numeric, 1) zero_minute_pct,
            count(*) minutes from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id
            where d.trading_symbol in ('NIFTY-Nov2026-FUT','BANKNIFTY-Nov2026-FUT','RELIANCE-Nov2026-FUT','HDFCBANK-Nov2026-FUT','SBIN-Nov2026-FUT','INFY-Nov2026-FUT','TCS-Nov2026-FUT','ICICIBANK-Nov2026-FUT')
            and (f.ts {IST})::time between '09:15' and '15:29' group by 1, 2 order by 1""")
        out["futures_zero_volume_minutes_by_hour_nifty"] = rows(c, f"""select extract(hour from f.ts {IST})::int hour_ist, round(100*avg((f.volume=0)::int)::numeric,1) zero_pct from futures_candles_1m f join derivative_contracts d on d.id=f.contract_id
            where d.trading_symbol='NIFTY-Nov2026-FUT' and (f.ts {IST})::time between '09:15' and '15:29' group by 1 order by 1""")
        # stray / non-equity-derivative contracts present in the same table
        out["non_nse_equity_contracts_in_futures_candles"] = rows(c, """select d.instrument_type, d.exchange, count(distinct d.underlying_symbol) underlyings, count(distinct d.id) contracts from futures_candles_1m f
            join derivative_contracts d on d.id = f.contract_id where d.instrument_type not in ('FUTIDX','FUTSTK') or d.exchange <> 'NSE' group by 1, 2""")
        out["stray_equity_style_tickers_non_standard_symbol"] = rows(c, """select d.underlying_symbol, d.trading_symbol, d.tick_size, d.lot_size, d.expiry, count(*) bars from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id
            where d.instrument_type in ('FUTIDX','FUTSTK') and d.trading_symbol not like '%-FUT' group by 1,2,3,4,5 order by 1""")
        # equity cash 1m
        out["equity_cash_1m_by_symbol"] = rows(c, f"""with s as (select i.symbol, (b.timestamp {IST})::date d, count(*) n, max((b.timestamp {IST})::time) last_t from market_bars b join instruments i on i.id = b.instrument_id
            where b.interval = '1m' and b.source = 'dhan' and i.asset_class = 'equity' and i.venue = 'NSE' group by 1, 2)
            select symbol, count(*) sessions, count(*) filter (where n >= 370) full_sessions, count(*) filter (where n between 355 and 365) truncated_at_1514,
            min(d) first, max(d) last from s group by 1 order by 2 desc""")
        out["equity_cash_other_intervals_nse"] = rows(c, """select b.interval, b.source, count(distinct i.symbol) symbols, count(*) bars, min(b.timestamp)::date first, max(b.timestamp)::date last from market_bars b
            join instruments i on i.id = b.instrument_id where i.asset_class = 'equity' and i.venue = 'NSE' and b.interval in ('5m','15m','1h') group by 1, 2 order by 1, 2""")
        out["futures_tick_grid_anomalies_note"] = "INFY-Nov2026-FUT: prices on a 0.05 grid from 2026-10-01 while the master tick is 0.10 (tick size is date-dependent); such sessions are excluded by the VAR loader"
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=str))
    print(OUT)


if __name__ == "__main__":
    main()
