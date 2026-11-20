"""Phase 1 scalp_hold diagnostics on STORED data (read-only). Writes diagnostics/phase1_scalp.json (replaced each run).

Run from backend/:  python scripts/phase1_scalp.py
Mirrors the app's 'single' run (seasonal fitted on the first 40% of bars, the last 60% traded) for each instrument, with the venue built from the
instrument's OWN lot size and tick (the app's venue_for would price every future at the NIFTY lot/tick)."""
from __future__ import annotations

import json
import sys
import time as clock
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.trade_stats import net_gross_block  # noqa: E402
from scalp_hold import evaluate as ev  # noqa: E402
from scalp_hold.app_adapter import DEFAULT_PARAMS, prepare_frame  # noqa: E402
from scalp_hold.barriers import BarrierCfg  # noqa: E402
from scalp_hold.features import fit_seasonal  # noqa: E402
from scalp_hold.funnel import cost_decomposition, s_cap_feasibility, scalp_funnel  # noqa: E402
from scalp_hold.sim import SimCfg, simulate  # noqa: E402
from scalp_hold.venues import VENUES, VenueProfile, nse_index_fut, nse_stock_fut  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase1_scalp.json"
EQUITY = 2_000_000.0
log = lambda *a: print(*a, flush=True)  # noqa: E731
D = Decimal


def futures_frame(db: Session, underlying: str) -> tuple[pd.DataFrame, VenueProfile, str] | None:
    row = db.execute(text("""select c.id, c.trading_symbol, c.lot_size, c.tick_size, c.instrument_type, count(distinct (f.ts at time zone 'Asia/Kolkata')::date) n
        from derivative_contracts c join futures_candles_1m f on f.contract_id = c.id where c.underlying_symbol = :u and c.instrument_type like 'FUT%' and c.exchange = 'NSE'
        group by 1, 2, 3, 4, 5 order by n desc, c.expiry limit 1"""), {"u": underlying}).first()
    if row is None:
        return None
    rows = db.execute(text("select ts, open, high, low, close, volume from futures_candles_1m where contract_id = :i order by ts"), {"i": row.id}).all()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").astype(float)
    lot, tick = str(int(row.lot_size)), str(Decimal(row.tick_size) / 100)
    venue = nse_index_fut(lot, tick) if row.instrument_type == "FUTIDX" else nse_stock_fut(lot, tick)
    return df, venue, row.trading_symbol


def equity_frame(db: Session, symbol: str) -> tuple[pd.DataFrame, VenueProfile, str] | None:
    rows = db.execute(text("""select b.timestamp, b.open, b.high, b.low, b.close, b.volume from market_bars b join instruments i on i.id = b.instrument_id
        where i.symbol = :s and i.asset_class = 'equity' and i.venue = 'NSE' and b.interval = '1m' and b.source = 'dhan' order by b.timestamp"""), {"s": symbol}).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").astype(float), VENUES["equity"], f"{symbol}-EQ"


def to_trade_dicts(t: pd.DataFrame) -> list[dict]:
    out = []
    for r in t.itertuples():
        out.append({"gross_pnl": D(repr(float(r.gross_pnl))), "net_pnl": D(repr(float(r.net_pnl))), "charges": D(repr(float(r.cost_total))), "turnover": D(repr(float(r.notional * 2))),
                    "r_multiple": float(r.R), "entry_ts": r.ts_entry.to_pydatetime(), "exit_ts": r.ts_exit.to_pydatetime(), "brokerage": D(0), "exchange_charge": D(0), "ipft": D(0),
                    "sebi": D(0), "stt": D(0), "stamp_duty": D(0), "gst": D(0), "other_charges": D(0)})
    return out


def run_one(frame: pd.DataFrame, venue: VenueProfile, symbol: str, params: dict, slip_bps: float, feas: bool) -> dict | None:
    bm = int(params["bar_minutes"])
    df, info = prepare_frame(frame, venue, bm, 100.0)
    if df is None:
        return {"refused": info.get("refused")}
    bcfg = BarrierCfg(k_vol=params["k_vol"], theta_feeR=params["theta_feeR"], s_cap_mult=params["s_cap_mult"], min_tick_mult=params["min_tick_mult"], r_min=params["r_min"], waiver_buffer=params["waiver_buffer"])
    cutoff = params["entry_cutoff"]
    h, m = (int(x) for x in cutoff.split(":")) if cutoff else (None, None)
    cfg = SimCfg(equity=EQUITY, risk_pct=params["risk_pct"], max_lev=params["max_lev"], r=params["r"], H=float(params["H_minutes"]), must_work_enabled=bool(params["must_work_enabled"]),
                 entry_cutoff_minutes=None if h is None else h * 60 + m, daily_loss_R=params["daily_loss_R"], max_trades_day=params["max_trades_day"], lockout_bars=params["lockout_bars"],
                 lockout_atr=params["lockout_atr"], must_work_at_frac=params["must_work_at_frac"], must_work_frac=params["must_work_frac"])
    prep = ev.prepare_instrument(df, venue, symbol, slip_bps)
    cut = int(len(df) * 0.4)
    seasonal = fit_seasonal(df.iloc[:cut], venue)
    test = replace(prep, df=prep.df.iloc[cut:], f=prep.f.iloc[cut:], sig=prep.sig.iloc[cut:])
    res = simulate(test.df, test.f, test.sig, venue, symbol, cfg, slip_bps=slip_bps, seasonal=seasonal, barrier_cfg=bcfg)
    out = {"sessions_traded": int(test.df.index.tz_convert("Asia/Kolkata").normalize().nunique()), "funnel": scalp_funnel(res, symbol), "cost_decomposition": cost_decomposition(res.trades),
           "cost_report": res.cost_report, "trades": int(len(res.trades))}
    out["funnel"].pop("rows")
    if len(res.trades):
        out["net_gross"] = net_gross_block(to_trade_dicts(res.trades), D(str(EQUITY)))
    if feas:
        sig_idx = None
        notionals = [25_000.0, 50_000.0, 100_000.0, 200_000.0, 500_000.0, EQUITY * 2]
        out["s_cap_feasibility"] = s_cap_feasibility(test.df, test.f, venue, cfg, bcfg, seasonal, notionals)
    return out


def main() -> None:
    t0 = clock.time()
    eng = create_engine(settings.database_url)
    insts: list[tuple[str, tuple]] = []
    with Session(eng) as db:
        for u in ("NIFTY", "BANKNIFTY"):
            x = futures_frame(db, u)
            if x:
                insts.append((u, x))
        top = [r[0] for r in db.execute(text("""
            with d as (select c.underlying_symbol as u, (f.ts at time zone 'Asia/Kolkata')::date as dy, sum(f.close * f.volume) as t
                       from futures_candles_1m f join derivative_contracts c on c.id = f.contract_id where c.instrument_type = 'FUTSTK' group by 1, 2)
            select u from d group by u order by percentile_cont(0.5) within group (order by t) desc limit 20"""))]
        for u in top:
            x = futures_frame(db, u)
            if x:
                insts.append((u, x))
        for s in ("SBIN", "RELIANCE", "GROWW"):
            x = equity_frame(db, s)
            if x:
                insts.append((s, x))
    log("instruments", len(insts), round(clock.time() - t0), "s")
    configs = {"legacy_must_work_on_cutoff_1500": {"must_work_enabled": True, "entry_cutoff": ""},
               "corrected_must_work_off_cutoff_1500": {"must_work_enabled": False, "entry_cutoff": ""},
               "corrected_must_work_off_cutoff_1510": {"must_work_enabled": False, "entry_cutoff": "15:10"}}
    out: dict = {"equity": EQUITY, "note": "single run per instrument: seasonal fitted on the first 40% of bars, last 60% traded (same as the app's 'single' run)", "configs": {}, "instruments": []}
    results: dict = {c: {} for c in configs}
    for u, (frame, venue, sym) in insts:
        slip = 0.3 if u in ("NIFTY", "BANKNIFTY") else 2.0
        out["instruments"].append({"underlying": u, "symbol": sym, "venue": venue.id, "lot": str(venue.lot_size), "tick": str(venue.tick_size), "bars_1m": int(len(frame))})
        for cname, over in configs.items():
            p = {**DEFAULT_PARAMS, **over}
            try:
                results[cname][f"{u}|{venue.id}"] = run_one(frame, venue, u, p, slip, feas=(cname == "corrected_must_work_off_cutoff_1500"))
            except Exception as e:   # recorded, never silently dropped
                results[cname][f"{u}|{venue.id}"] = {"error": f"{type(e).__name__}: {e}"}
        log(f"{u}|{venue.id}", {c: (results[c][f"{u}|{venue.id}"] or {}).get("trades", (results[c][f"{u}|{venue.id}"] or {}).get("error", "refused")) for c in configs})
    # phase 0 sanity (raw s, tradeability overlay) on the first instrument whose frame passes validation
    out["phase0_sample"] = {"error": "no instrument prepared"}
    for u0, (f0, v0, _s0) in insts:
        df0, _ = prepare_frame(f0, v0, 5, 100.0)
        if df0 is None:
            continue
        try:
            p0 = ev.phase0(ev.prepare_instrument(df0, v0, u0, 2.0), ev.QUICK_GRID, equity=EQUITY)
            out["phase0_sample"] = {"instrument": u0, "venue": v0.id, "rows": json.loads(p0.head(12).to_json(orient="records")), "columns": list(p0.columns)}
        except Exception as e:
            out["phase0_sample"] = {"instrument": u0, "error": f"{type(e).__name__}: {e}"}
        break
    out["configs"] = results
    out["seconds"] = round(clock.time() - t0)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=str))
    log("done", OUT)


if __name__ == "__main__":
    main()
