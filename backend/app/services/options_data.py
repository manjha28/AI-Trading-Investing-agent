"""Options data access with strict as-of semantics, plus the data-coverage audit and live-chain ingestion.

Nothing here fabricates data: a missing contract, quote, Greek or candle is returned as missing and the callers must refuse or say NO TRADE.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import NSE_CALENDAR_YEARS
from app.models import Instrument, MarketBar
from app.models.strategies import OptionContract, OptionQuote

D = Decimal
IST = ZoneInfo("Asia/Kolkata")


def aware(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


@dataclass(frozen=True)
class Contract:
    id: str
    underlying: str
    symbol: str
    expiry: datetime
    strike: Decimal
    option_type: str          # CE | PE
    lot_size: int | None
    valid_from: datetime
    valid_to: datetime | None
    master_version: str
    source: str
    instrument_id: str | None = None

    @property
    def expiry_date(self):
        return aware(self.expiry).astimezone(IST).date()


@dataclass(frozen=True)
class Quote:
    contract_id: str
    time: datetime
    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int | None
    ask_qty: int | None
    ltp: Decimal | None
    volume: int | None
    implied_volatility: Decimal | None      # percent, as supplied by the provider
    delta: Decimal | None
    greeks_source: str | None               # provider | calculated | None
    underlying_price: Decimal | None
    source: str


@dataclass(frozen=True)
class Candle:
    time: datetime            # open time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class OptionDataSource(Protocol):
    def contracts_as_of(self, underlying: str, ts: datetime) -> list[Contract]: ...
    def latest_quote(self, contract_id: str, ts: datetime) -> Quote | None: ...
    def quotes_between(self, contract_id: str, start: datetime, end: datetime) -> list[Quote]: ...
    def candles_between(self, contract_id: str, start: datetime, end: datetime) -> list[Candle]: ...


class InMemoryOptionData:
    """Deterministic source for tests and for loading exported datasets. as-of rules are identical to the DB source."""

    def __init__(self, contracts: list[Contract], quotes: list[Quote] | None = None, candles: dict[str, list[Candle]] | None = None):
        self.contracts, self.quotes, self.candles = contracts, sorted(quotes or [], key=lambda q: q.time), candles or {}

    def contracts_as_of(self, underlying: str, ts: datetime) -> list[Contract]:
        ts = aware(ts)
        return [c for c in self.contracts if c.underlying == underlying and aware(c.valid_from) <= ts and (c.valid_to is None or ts < aware(c.valid_to)) and aware(c.expiry) > ts]

    def latest_quote(self, contract_id: str, ts: datetime) -> Quote | None:
        ts = aware(ts)
        best = None
        for q in self.quotes:
            if q.contract_id == contract_id and aware(q.time) <= ts:
                best = q
        return best

    def quotes_between(self, contract_id: str, start: datetime, end: datetime) -> list[Quote]:
        return [q for q in self.quotes if q.contract_id == contract_id and aware(start) <= aware(q.time) <= aware(end)]

    def candles_between(self, contract_id: str, start: datetime, end: datetime) -> list[Candle]:
        return [c for c in self.candles.get(contract_id, []) if aware(start) <= aware(c.time) <= aware(end)]


class DbOptionData:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _contract(r: OptionContract) -> Contract:
        return Contract(str(r.id), r.underlying, r.trading_symbol, aware(r.expiry), r.strike, r.option_type, r.lot_size, aware(r.valid_from),
                        aware(r.valid_to) if r.valid_to else None, r.master_version, r.source, str(r.instrument_id) if r.instrument_id else None)

    @staticmethod
    def _quote(q: OptionQuote) -> Quote:
        return Quote(str(q.contract_id), aware(q.quote_time), q.bid, q.ask, q.bid_qty, q.ask_qty, q.ltp, q.volume, q.implied_volatility, q.delta, q.greeks_source, q.underlying_price, q.source)

    def contracts_as_of(self, underlying: str, ts: datetime) -> list[Contract]:
        ts = aware(ts)
        rows = self.db.scalars(select(OptionContract).where(OptionContract.underlying == underlying, OptionContract.valid_from <= ts, OptionContract.expiry > ts))
        return [self._contract(r) for r in rows if r.valid_to is None or ts < aware(r.valid_to)]

    def latest_quote(self, contract_id: str, ts: datetime) -> Quote | None:
        q = self.db.scalar(select(OptionQuote).where(OptionQuote.contract_id == UUID(contract_id), OptionQuote.quote_time <= aware(ts)).order_by(OptionQuote.quote_time.desc()).limit(1))
        return self._quote(q) if q else None

    def quotes_between(self, contract_id: str, start: datetime, end: datetime) -> list[Quote]:
        rows = self.db.scalars(select(OptionQuote).where(OptionQuote.contract_id == UUID(contract_id), OptionQuote.quote_time >= aware(start), OptionQuote.quote_time <= aware(end)).order_by(OptionQuote.quote_time))
        return [self._quote(q) for q in rows]

    def candles_between(self, contract_id: str, start: datetime, end: datetime) -> list[Candle]:
        """1-minute candles of THIS exact contract (options_candles_1m). Rolling-strike series are never returned here: they are not fixed contracts."""
        from app.models import OptionCandle1m
        rows = self.db.scalars(select(OptionCandle1m).where(OptionCandle1m.contract_id == UUID(contract_id), OptionCandle1m.ts >= aware(start), OptionCandle1m.ts <= aware(end)).order_by(OptionCandle1m.ts))
        return [Candle(aware(r.ts), r.open, r.high, r.low, r.close, D(r.volume)) for r in rows]


# ───────────────────────── coverage audit ─────────────────────────
def coverage_report(db: Session, now: datetime | None = None, underlying: str = "NIFTY") -> dict[str, Any]:
    """What NIFTY options data actually exists, measured from the database. Never assumes: a dataset is 'available' only if rows exist."""
    now = aware(now or datetime.now(timezone.utc))

    def bars(cls_filter) -> dict[str, Any]:
        q = (select(func.count(), func.count(func.distinct(MarketBar.instrument_id)), func.min(MarketBar.timestamp), func.max(MarketBar.timestamp))
             .join(Instrument, Instrument.id == MarketBar.instrument_id).where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False), *cls_filter))
        n, inst, lo, hi = db.execute(q).one()
        return {"rows": int(n), "instruments": int(inst), "first": lo.isoformat() if lo else None, "last": hi.isoformat() if hi else None, "status": "available" if n else "missing"}

    from app.models import DerivativeContract, FuturesCandle1m, OptionCandle1m, RollingOptionCandle1m
    name = Instrument.symbol.ilike(f"{underlying}%")
    spot = bars([Instrument.asset_class == "index", name])

    def span(model, ts_col, *where) -> dict[str, Any]:
        n, lo, hi, k = db.execute(select(func.count(), func.min(ts_col), func.max(ts_col), func.count(func.distinct(func.date(ts_col)))).where(*where)).one()
        return {"rows": int(n), "first": aware(lo).isoformat() if lo else None, "last": aware(hi).isoformat() if hi else None, "sessions": int(k), "status": "available" if n else "missing"}

    futures = span(FuturesCandle1m, FuturesCandle1m.ts, FuturesCandle1m.contract_id.in_(select(DerivativeContract.id).where(DerivativeContract.underlying_symbol == underlying, DerivativeContract.instrument_type.in_(("FUTIDX", "FUTSTK", "FUTCOM")))))
    futures["instruments"] = int(db.scalar(select(func.count(func.distinct(FuturesCandle1m.contract_id))).where(FuturesCandle1m.contract_id.in_(select(DerivativeContract.id).where(DerivativeContract.underlying_symbol == underlying)))) or 0)
    opt_inst = span(OptionCandle1m, OptionCandle1m.ts, OptionCandle1m.contract_id.in_(select(OptionContract.id).where(OptionContract.underlying == underlying)))
    rolling = span(RollingOptionCandle1m, RollingOptionCandle1m.ts, RollingOptionCandle1m.underlying_symbol == underlying)
    contracts = db.execute(select(func.count(), func.count(func.distinct(OptionContract.expiry)), func.count(func.distinct(OptionContract.strike)), func.min(OptionContract.valid_from),
                                  func.max(OptionContract.valid_from), func.count(OptionContract.lot_size), func.min(OptionContract.master_version), func.max(OptionContract.source))
                           .where(OptionContract.underlying == underlying)).one()
    n_c, n_exp, n_strk, c_lo, c_hi, n_lot, ver, src = contracts
    q = db.execute(select(func.count(), func.count(func.distinct(OptionQuote.contract_id)), func.min(OptionQuote.quote_time), func.max(OptionQuote.quote_time), func.count(OptionQuote.bid),
                          func.count(OptionQuote.ask), func.count(OptionQuote.bid_qty), func.count(OptionQuote.implied_volatility), func.count(OptionQuote.delta), func.max(OptionQuote.source))
                   .join(OptionContract, OptionContract.id == OptionQuote.contract_id).where(OptionContract.underlying == underlying)).one()
    n_q, n_qc, q_lo, q_hi, n_bid, n_ask, n_depth, n_iv, n_delta, q_src = q
    live_age = (now - aware(q_hi)).total_seconds() if q_hi else None
    try:
        from app.models import BrokerCredential
        dhan_creds = db.execute(select(func.count()).select_from(BrokerCredential).where(BrokerCredential.broker == "dhan")).scalar()
    except Exception:  # noqa: BLE001 - schema differences must not break the audit
        dhan_creds = None
    datasets = {
        "spot_1m": {"label": f"{underlying} index (spot) 1-minute candles", "provider": None, **spot},
        "futures_1m": {"label": f"{underlying} futures 1-minute OHLCV + OI", "provider": "dhan_intraday" if futures["rows"] else None, **futures},
        "option_candles_1m": {"label": "Active option contract 1-minute candles (exact contracts)", "provider": "dhan_intraday" if opt_inst["rows"] else None, **opt_inst},
        "rolling_option_candles_1m": {"label": "Expired rolling-strike option candles (ATM-relative; NOT usable as fixed-contract history)", "provider": "dhan_rollingoption" if rolling["rows"] else None, **rolling},
        "contract_master": {"label": "Effective-dated option contract master (expiry, strike, lot size)", "provider": src, "rows": int(n_c), "expiries": int(n_exp), "strikes": int(n_strk),
                            "first": c_lo.isoformat() if c_lo else None, "last": c_hi.isoformat() if c_hi else None, "lot_size_known": int(n_lot), "master_version": ver,
                            "status": "available" if n_c else "missing"},
        "option_quotes": {"label": "Historical option quotes (bid/ask)", "provider": q_src, "rows": int(n_q), "contracts": int(n_qc), "first": q_lo.isoformat() if q_lo else None, "last": q_hi.isoformat() if q_hi else None,
                          "with_bid": int(n_bid), "with_ask": int(n_ask), "status": "available" if n_bid and n_ask else "missing"},
        "option_depth": {"label": "Quote quantities / depth", "provider": q_src, "rows": int(n_depth), "status": "available" if n_depth else "missing"},
        "implied_volatility": {"label": "Implied volatility history", "provider": q_src, "rows": int(n_iv), "status": "available" if n_iv else "missing"},
        "greeks_delta": {"label": "Timestamp-matched delta", "provider": q_src, "rows": int(n_delta), "status": "available" if n_delta else "missing"},
    }
    blockers: list[str] = []
    if futures["status"] == "missing":
        blockers.append("No NIFTY futures 1-minute data: the underlying signal (traded price and volume) cannot be evaluated.")
    if spot["status"] == "missing":
        blockers.append("No NIFTY index (spot) data: ATM strike mapping would have to use the futures price as a labelled proxy.")
    if not n_c:
        blockers.append("No option contract master: expiries, strikes and lot sizes are unknown, so no contract can be identified.")
    if not n_lot and n_c:
        blockers.append("Contract master rows have no lot size.")
    quote_ok = datasets["option_quotes"]["status"] == "available"
    candle_ok = opt_inst["status"] == "available"
    if not quote_ok and not candle_ok:
        blockers.append("No historical option quotes and no option candles: option premiums are unavailable. Performance results are refused rather than inferred from the index or futures.")
    if quote_ok and not (n_delta or n_iv):
        blockers.append("Quotes exist but no delta or implied volatility: delta-based contract selection cannot run (Greeks are never invented).")
    ready_core = futures["status"] == "available" and bool(n_c) and bool(n_lot)
    if ready_core and quote_ok and (n_delta or n_iv):
        mode = "quote_accurate"
    elif ready_core and candle_ok and (n_delta or n_iv):
        mode = "indicative_ohlc"
    else:
        mode = "unavailable"
    calendar_years = sorted(NSE_CALENDAR_YEARS)
    return {
        "underlying": underlying, "as_of": now.isoformat(), "datasets": datasets, "backtest_mode": mode, "backtest_ready": mode != "unavailable", "blockers": blockers,
        "live": {
            "option_chain_api": "Dhan /v2/optionchain (live snapshot only; 3 s rate limit); not persisted before this change",
            "dhan_broker_credentials_present": bool(dhan_creds) if dhan_creds is not None else None,
            "stored_quote_freshness_seconds": live_age, "live_ready": bool(quote_ok and live_age is not None and live_age < 120),
            "note": "Live chain requires a Dhan-connected account and a stored NIFTY underlying instrument with a Dhan identifier; none exists." if not futures["rows"] and not spot["rows"] else "",
        },
        "calendar": {"nse_calendar_years_configured": calendar_years, "note": "Trading-day logic only covers the configured years; other years rely on data presence."},
        "disclaimer": "Coverage is measured from stored rows. Missing datasets are never substituted with index/futures prices, synthetic prices or invented Greeks.",
    }


def dataset_fingerprint(contracts: list[Contract], quotes: list[Quote]) -> str:
    h = hashlib.sha256()
    for c in sorted(contracts, key=lambda x: x.id):
        h.update(f"{c.id}|{c.symbol}|{c.strike}|{c.expiry.isoformat()}|{c.lot_size}|{c.master_version}".encode())
    for q in sorted(quotes, key=lambda x: (x.contract_id, x.time)):
        h.update(f"{q.contract_id}|{q.time.isoformat()}|{q.bid}|{q.ask}|{q.bid_qty}|{q.ask_qty}|{q.delta}|{q.implied_volatility}".encode())
    return h.hexdigest()
