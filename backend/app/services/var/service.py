"""Orchestration for stored-data VAR backtests and data-capability reporting (DB read-only for market data)."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .backtest import BacktestResult, instrument_data_from_underlying, run_backtest
from .config import VarConfig
from app.services import indian_charges as ic

from .funnel import build_funnel
from .loader import LOOKBACK_CALENDAR_DAYS, MIN_5M_BARS, UnderlyingData, load_equity, load_underlying
from .universe import rank_stock_universe


def jsonable(obj: Any) -> Any:
    def default(o: Any) -> Any:
        if isinstance(o, (Decimal, date, datetime)):
            return o.isoformat() if isinstance(o, (date, datetime)) else str(o)
        if is_dataclass(o):
            return asdict(o)
        return str(o)
    return json.loads(json.dumps(obj, default=default))


def data_capabilities(db: Session) -> dict:
    """What the stored data can and cannot support. Never claims order-flow capability that the store does not have."""
    q = lambda s: db.execute(text(s)).all()
    fut = q("""select d.underlying_symbol, d.instrument_type, count(distinct d.id), count(*), min(f.ts)::date, max(f.ts)::date, count(distinct (f.ts at time zone 'Asia/Kolkata')::date)
               from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id
               where d.underlying_symbol in ('NIFTY','BANKNIFTY') group by 1, 2 order by 1""")
    stock = q("""select count(distinct d.underlying_symbol), count(*) from futures_candles_1m f join derivative_contracts d on d.id = f.contract_id where d.instrument_type = 'FUTSTK'""")
    ticks = q("select count(*), min(timestamp), max(timestamp) from dhan_market_ticks")[0]
    return jsonable({
        "futures_candles_1m": [{"underlying": r[0], "type": r[1], "contracts": r[2], "bars": r[3], "first": r[4], "last": r[5], "sessions": r[6]} for r in fut],
        "stock_futures": {"underlyings_with_candles": stock[0][0], "bars": stock[0][1]},
        "stored_ticks": {"rows": ticks[0], "first": ticks[1], "last": ticks[2], "fields": "snapshot OHLC + cumulative volume only"},
        "data_level": "C_candle_approximate",
        "supports": ["volume profile (approximate, candle-distributed)", "VWAP (typical-price approximation)", "TPO (candle-range approximation)", "Initial Balance",
                     "ATR / regime / breakout and reversal rules without order flow (variant A)"],
        "does_not_support": ["trade-level volume at price", "bid/ask-classified or tick-rule delta", "CVD, delta divergence, absorption (variants B-D)",
                             "historical spread filter", "F&O ban list and scheduled-results exclusion"],
        "order_flow": "unavailable: no stored last-traded quantity, bid/ask or depth; order-flow signals are suppressed, never estimated from candles",
        "volume_unit": "underlying units (stored volume is always a multiple of the lot size; inferred, not documented)",
        "tick_size_unit": "paise in derivative_contracts (verified against observed price granularity at load time)",
        "lot_size_note": "lot size comes from the current instrument master row, not an effective-dated history",
        "session_note": "stored candles extend past 15:30; the loader clips to the effective-dated regular session",
    })


SEGMENTS = ("index_futures", "stock_futures", "equity_cash")
ORDER_FLOW_STORED = False          # no last-traded quantity / bid-ask / depth is stored, so delta-dependent variants (B-D) can never produce a trade
USABLE_MINUTES = 365               # >= 365 of the 375 regular-session minutes stored => the session can form >= 73 complete 5m bars (approximate; the loader is authoritative)
_IST = ZoneInfo("Asia/Kolkata")


def _day_bounds(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    lo = datetime.combine((start or date(2000, 1, 1)) - timedelta(days=LOOKBACK_CALENDAR_DAYS), time(0), tzinfo=_IST)
    hi = datetime.combine((end or date(2100, 1, 1)) + timedelta(days=1), time(0), tzinfo=_IST)
    return lo, hi


def classify_symbol(row: dict) -> dict:
    """Pure classification of one symbol's stored-data facts (tested without a database)."""
    if not row["exists"]:
        status, why = "not_in_storage", "no instrument / contract with this symbol is stored"
    elif row["sessions_available"] == 0:
        status, why = "no_bars_in_window", "the instrument exists but has no stored 1-minute candles in the window (plus lookback)"
    elif row["sessions_usable"] == 0:
        status, why = "no_usable_sessions", f"{row['sessions_available']} sessions stored but none has >= {USABLE_MINUTES} regular-session minutes (the provider serves 1-minute candles only to 15:14 for most windows)"
    else:
        status, why = "ok", ""
    warnings = []
    if status == "ok" and row["sessions_usable"] < 21:
        warnings.append(f"only {row['sessions_usable']} usable sessions: the 20-session relative-volume baseline cannot complete, so breakout candidates will be rejected for lack of a baseline")
    return {**row, "status": status, "reason": why, "warnings": warnings}


def preflight(db: Session, segment: str, symbols: list[str], start: date | None, end: date | None) -> list[dict]:
    """Validate every requested symbol against STORED data before a run starts (one cheap SQL pass per symbol)."""
    lo, hi = _day_bounds(start, end)
    out = []
    for sym in [x.upper() for x in symbols]:
        if segment == "equity_cash":
            exists = bool(db.execute(text("select 1 from instruments where symbol = :s and asset_class = 'equity' and venue = 'NSE' limit 1"), {"s": sym}).first())
            q = """with s as (select (b.timestamp at time zone 'Asia/Kolkata')::date d, count(*) filter (where (b.timestamp at time zone 'Asia/Kolkata')::time between '09:15' and '15:29') n
                   from market_bars b join instruments i on i.id = b.instrument_id where i.symbol = :s and i.asset_class = 'equity' and i.venue = 'NSE' and b.interval = '1m'
                   and b.source = 'dhan' and b.is_synthetic = false and b.timestamp >= :lo and b.timestamp < :hi group by 1)
                   select count(*), count(*) filter (where n >= :need), min(d), max(d) from s"""
        else:
            kind = "FUTIDX" if segment == "index_futures" else "FUTSTK"
            exists = bool(db.execute(text("select 1 from derivative_contracts where underlying_symbol = :s and exchange = 'NSE' and instrument_type = :k limit 1"), {"s": sym, "k": kind}).first())
            q = """with s as (select (f.ts at time zone 'Asia/Kolkata')::date d, f.contract_id, count(*) filter (where (f.ts at time zone 'Asia/Kolkata')::time between '09:15' and '15:29') n
                   from futures_candles_1m f join derivative_contracts c on c.id = f.contract_id where c.underlying_symbol = :s and c.exchange = 'NSE' and c.instrument_type = '""" + kind + """'
                   and f.ts >= :lo and f.ts < :hi group by 1, 2), t as (select d, max(n) n from s group by d)
                   select count(*), count(*) filter (where n >= :need), min(d), max(d) from t"""
        r = db.execute(text(q), {"s": sym, "lo": lo, "hi": hi, "need": USABLE_MINUTES}).first() if exists else (0, 0, None, None)
        out.append(classify_symbol({"symbol": sym, "exists": exists, "sessions_available": int(r[0] or 0), "sessions_usable": int(r[1] or 0),
                                    "first": r[2].isoformat() if r[2] else None, "last": r[3].isoformat() if r[3] else None}))
    return out


def run_status(audits: list[dict], bars_replayed: int) -> tuple[str, str]:
    """('completed' | 'no_usable_data', reason). A run that processed zero sessions is never reported as completed."""
    if sum(a["sessions_loaded"] for a in audits) == 0:
        return "no_usable_data", "no session of any requested symbol could be loaded (see the per-symbol exclusions)"
    if bars_replayed == 0:
        return "no_usable_data", "sessions were loaded but none was tradable in the requested window (no prior session, or the window is outside the loaded sessions)"
    return "completed", ""


def run_stored_backtest(db: Session, request: dict, config: dict) -> dict:
    cfg = VarConfig(**config)
    unders = [u.upper() for u in request["underlyings"]]
    segment = request.get("segment") or "index_futures"
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}")
    start = date.fromisoformat(request["start"]) if request.get("start") else None
    end = date.fromisoformat(request["end"]) if request.get("end") else None
    if cfg.uses_orderflow and not ORDER_FLOW_STORED:
        return {"status": "order_flow_unavailable", "segment": segment, "variant": cfg.variant, "data_audit": [],
                "reason": f"variant {cfg.variant} needs trade-level order flow (delta/CVD), which is not stored; it cannot produce a trade on candle data. Use variant A."}
    # load history BEFORE the requested start so ATR, the previous-session profile and the relative-volume baseline are warm on the first traded session
    load_start = (start - timedelta(days=LOOKBACK_CALENDAR_DAYS)) if start else None
    loaded: list[UnderlyingData] = [(load_equity(db, u, load_start, end) if segment == "equity_cash" else load_underlying(db, u, load_start, end)) for u in unders]
    notes = [n for u in loaded for n in u.notes]
    audits = [u.audit(start) for u in loaded]
    usable = [u for u in loaded if u.series]
    if not usable:
        status, why = run_status(audits, 0)
        return {"status": status, "reason": why, "notes": notes[:50], "segment": segment, "data_audit": audits}
    data = [instrument_data_from_underlying(u) for u in usable]
    top_n = request.get("stock_universe_top_n")
    universe_rows: list[dict] = []
    if top_n and segment == "stock_futures":
        stocks = [(u, d) for u, d in zip(usable, data) if u.kind == "stock"]
        cfg = VarConfig(**{**config, "universe": {**cfg.universe.model_dump(), "max_stock_contracts": int(top_n)}})
        days = sorted({d for _u, x in stocks for d in x.sessions})
        prior = None
        for day in days:
            picked, rows = rank_stock_universe([u for u, _x in stocks], day, prior, cfg)
            prior = day
            universe_rows.append({"day": day.isoformat(), "selected": picked, "ranking": rows})
            for u, x in stocks:
                if u.underlying not in picked:
                    x.excluded_days[day] = "not in the point-in-time top-N liquid universe"
    capital = Decimal(str(request.get("initial_capital", "2000000")))
    res: BacktestResult = run_backtest(data, cfg, capital, start=start, end=end)
    status, why = run_status(audits, int(res.window["bars"]))  # bars actually replayed, not sessions merely present in the data
    if status != "completed":
        return {"status": status, "reason": why, "notes": notes[:50], "segment": segment, "data_audit": audits, "skipped_sessions": res.skipped_sessions[:200], "window": res.window}
    sample = data[0].day_contract and next(iter(data[0].day_contract.values()))
    sample_day = date.fromisoformat(res.window["end"]) if res.window.get("end") else date.today()
    seg_key, itype = ("equity_cash", "cash") if segment == "equity_cash" else ("futures", "index_future" if segment == "index_futures" else "stock_future")
    try:
        schedule = ic.schedule_report(seg_key, itype, sample_day, cfg.charges.broker_profile, cfg.charges.exchange)
    except ic.ChargeError as e:
        schedule = {"unavailable": str(e)}
    funnel = build_funnel(res.rejections, res.signals, res.trades, res.stage_counts)
    ledger = [{k: v for k, v in t.items() if k not in ("fills",)} for t in res.trades]
    return {"status": "completed", "hypothetical": True, "segment": segment, "window": res.window, "data_audit": audits, "summary": res.summary, "by_setup": res.by_setup,
            "by_instrument": res.by_instrument, "by_direction": res.by_direction, "by_data_level": res.by_data_level, "assumptions": res.assumptions,
            "skipped_sessions": res.skipped_sessions[:200], "notes": notes[:50], "funnel": funnel, "pattern_stages": res.stage_counts, "fee_schedule": schedule,
            "configurations_evaluated": 1, "config_variant": cfg.variant, "net_reward_to_cost_k": cfg.risk.min_net_reward_to_cost, "sample_contract": getattr(sample, "symbol", None),
            "signals": res.signals[:500], "trades": ledger[:2000], "universe": universe_rows[-3:],
            "variant_note": ("order-flow variants (B-D) cannot produce trades on this data: stored candles carry no delta" if cfg.uses_orderflow else "")}
