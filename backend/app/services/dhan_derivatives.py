"""Dhan derivatives: empirically-verified capabilities, instrument-master parsing/classification, payload parsers and a thin client.

Every limit and support flag in ``CAPABILITIES`` was measured against the live API with the stored Dhan connection (scripts/dhan_derivatives_probe.py,
2026-11-05). Where the documentation and the API disagree (rolling-options window), the API's behaviour is what is recorded here.
Nothing in this module hardcodes security ids, lot sizes, tick sizes, strike intervals or expiry weekdays: they all come from the master or the responses.
"""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Iterator, Mapping
from zoneinfo import ZoneInfo

from app.broker_adapters import BrokerAdapterError, DhanAdapter, _request, _wait_for_dhan_market_data

IST = ZoneInfo("Asia/Kolkata")
MASTER_URL = DhanAdapter.scrip_master_url
DERIVATIVE_TYPES = ("FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR", "OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR")
FUTURE_TYPES = ("FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR")
OPTION_TYPES = ("OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR")
SEGMENT_OF = {("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO", ("MCX", "M"): "MCX_COMM", ("NSE", "M"): "NSE_COMM", ("NSE", "C"): "NSE_CURRENCY", ("BSE", "C"): "BSE_CURRENCY"}
CATEGORY_OF = {"FUTIDX": "index", "OPTIDX": "index", "FUTSTK": "stock", "OPTSTK": "stock", "FUTCOM": "commodity", "OPTFUT": "commodity", "FUTCUR": "currency", "OPTCUR": "currency"}
# Time of day used ONLY to turn an expiry date into an instant for time-to-expiry maths; it is not an exchange specification.
EXPIRY_CLOSE_IST = {"NSE_FNO": (15, 30), "BSE_FNO": (15, 30), "MCX_COMM": (23, 30), "NSE_COMM": (23, 30), "NSE_CURRENCY": (17, 0), "BSE_CURRENCY": (17, 0)}

INTRADAY_MAX_DAYS = 90            # documented and enforced by the existing adapter
ROLLING_MAX_SPAN_DAYS = 1         # MEASURED: toDate may be at most fromDate + 1 calendar day (inclusive) for every interval; the docs say 30
ROLLING_EARLIEST = date(2021, 4, 1)   # MEASURED: candles exist from about 2021-04; 2020 requests return empty
CHAIN_MIN_INTERVAL_SECONDS = 3.0

# Measured 2026-11-05. status: ok | partial | unsupported | unavailable. `note` states exactly what was observed.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "master": {"endpoint": "GET images.dhan.co/api-data/api-scrip-master-detailed.csv", "status": "ok", "note": "Current snapshot of ACTIVE contracts only (plus a few expired currency rows). Expired NSE/BSE/MCX F&O contracts are not listed, so their security ids cannot be discovered."},
    "futures_intraday_1m": {"endpoint": "POST /v2/charts/intraday", "status": "ok", "segments": ["NSE_FNO", "BSE_FNO", "MCX_COMM"], "instruments": ["FUTIDX", "FUTSTK", "FUTCOM"], "max_days": INTRADAY_MAX_DAYS,
                            "fields": ["open", "high", "low", "close", "volume", "timestamp", "open_interest"], "note": "Verified NSE index, NSE stock, BSE index and MCX commodity futures. History exists only from the contract's listing (about 2-3 months back)."},
    "futures_daily": {"endpoint": "POST /v2/charts/historical", "status": "ok", "segments": ["NSE_FNO", "BSE_FNO", "MCX_COMM"], "note": "Returns about 2.8 years of daily candles with open interest for any expiryCode; the series is Dhan-rolled with an UNDOCUMENTED rollover and is stored as 'provider_rolled', never spliced by us."},
    "futures_expired": {"endpoint": "POST /v2/charts/intraday", "status": "unavailable", "note": "No expired NSE/BSE/MCX F&O contract appears in the master, so no security id exists to request. The only expired contracts listed (NSE/BSE currency) were rejected with DH-905. Expired futures minute history is therefore NOT obtainable."},
    "options_active_intraday_1m": {"endpoint": "POST /v2/charts/intraday", "status": "ok", "segments": ["NSE_FNO", "BSE_FNO", "MCX_COMM"], "instruments": ["OPTIDX", "OPTSTK", "OPTFUT"], "max_days": INTRADAY_MAX_DAYS,
                                   "fields": ["open", "high", "low", "close", "volume", "timestamp", "open_interest"], "note": "Verified on an active NIFTY option contract (1-minute OHLC, volume, OI)."},
    "options_rolling_1m": {"endpoint": "POST /v2/charts/rollingoption", "status": "partial", "max_span_days": ROLLING_MAX_SPAN_DAYS, "earliest": ROLLING_EARLIEST.isoformat(),
                           "segments": {"NSE_FNO": ["OPTIDX", "OPTSTK"], "BSE_FNO": ["OPTIDX"], "MCX_COMM": ["OPTFUT"]},
                           "expiry_flags": {"OPTIDX": ["WEEK", "MONTH"], "OPTSTK": ["MONTH"], "OPTFUT": ["MONTH"]}, "expiry_codes": [1, 2, 3], "intervals": ["1", "5", "15", "60"],
                           "strike_offsets": "ATM, ATM+n / ATM-n (verified to about n=10; ATM+20 returns empty arrays)", "fields": ["open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike", "timestamp"],
                           "note": "Measured: only calendar windows of fromDate..fromDate+1 are accepted (docs say 30 days). expiryCode must be >= 1. The underlying securityId is the INDEX/EQUITY security id, not the master's F&O underlying id (stocks return empty with the latter). "
                                   "Unsupported/empty: NSE_COMM (400), MCX index options (empty), stock WEEK (empty)."},
    "option_chain_live": {"endpoint": "POST /v2/optionchain", "status": "ok", "segments": ["NSE_FNO underlyings", "BSE_FNO underlyings", "MCX_COMM underlyings"], "min_interval_seconds": CHAIN_MIN_INTERVAL_SECONDS,
                          "fields": ["last_price", "oc.{strike}.ce|pe.{greeks, iv, oi, previous_oi, previous_volume, volume, top_bid/ask price+quantity, security_id}"],
                          "note": "Verified NSE index (NIFTY), NSE stock (RELIANCE), BSE index (SENSEX) and MCX options. No provider timestamp in the response: our capture time is recorded."},
    "option_expiry_list": {"endpoint": "POST /v2/optionchain/expirylist", "status": "ok", "note": "Verified for NSE index/stock, BSE index and MCX underlyings."},
    "historical_bid_ask": {"endpoint": "-", "status": "unavailable", "note": "Dhan offers no historical bid/ask. It exists here only from live chain snapshots that we capture ourselves, from the day capture starts."},
    "live_feed": {"endpoint": "wss://api-feed.dhan.co", "status": "ok", "segments": ["NSE_FNO", "BSE_FNO", "MCX_COMM", "NSE_CURRENCY", "BSE_CURRENCY"], "note": "Handled by the existing live-feed hub (segment codes already decoded); futures need an Instrument row with a Dhan identifier to be subscribed."},
    "commodity_nse_comm": {"endpoint": "POST /v2/charts/rollingoption", "status": "unsupported", "note": "NSE_COMM (NSE commodity options in the master) is rejected by the rolling API (400); no data-API segment name is documented for it."},
}


class MasterError(ValueError):
    pass


@dataclass(frozen=True)
class MasterContract:
    exchange: str
    master_segment: str
    segment: str
    instrument_type: str
    security_id: str
    underlying_symbol: str
    underlying_security_id: str | None
    trading_symbol: str
    display_name: str | None
    expiry: date
    expiry_flag: str | None
    strike: Decimal | None
    option_type: str | None
    lot_size: Decimal | None
    tick_size: Decimal | None
    category: str
    cash_security_id: str | None
    cash_segment: str | None

    @property
    def is_option(self) -> bool:
        return self.instrument_type in OPTION_TYPES

    @property
    def spec_hash(self) -> str:
        raw = "|".join(str(x) for x in (self.trading_symbol, self.expiry, self.strike, self.option_type, self.lot_size, self.tick_size, self.underlying_security_id, self.expiry_flag, self.instrument_type))
        return hashlib.sha1(raw.encode()).hexdigest()

    @property
    def expiry_at(self) -> datetime:
        h, m = EXPIRY_CLOSE_IST.get(self.segment, (15, 30))
        return datetime(self.expiry.year, self.expiry.month, self.expiry.day, h, m, tzinfo=IST).astimezone(timezone.utc)


@dataclass
class MasterParse:
    contracts: list[MasterContract]
    underlyings: dict[tuple[str, str, str], dict[str, Any]]
    rows_total: int
    skipped: dict[str, int]
    sha256: str


def _dec(v: str | None) -> Decimal | None:
    if v is None or v.strip() in ("", "NA"):
        return None
    try:
        d = Decimal(v.strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def parse_master(lines: Iterable[str]) -> MasterParse:
    """Parse the detailed scrip master into derivative contracts. Fails loudly if the columns we depend on are missing."""
    h = hashlib.sha256()
    buffered: list[str] = []
    for ln in lines:
        h.update(ln.encode() if isinstance(ln, str) else ln)
        buffered.append(ln)
    reader = csv.DictReader(buffered)
    required = {"EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SECURITY_ID", "UNDERLYING_SYMBOL", "SYMBOL_NAME", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE"}
    if not required <= set(reader.fieldnames or ()):
        raise MasterError(f"instrument master is missing columns: {sorted(required - set(reader.fieldnames or ()))}")
    index_by: dict[tuple[str, str], str] = {}
    equity_by: dict[tuple[str, str], tuple[int, str]] = {}
    raw_deriv: list[dict[str, str]] = []
    skipped: dict[str, int] = defaultdict(int)
    total = 0
    for row in reader:
        total += 1
        ins = row["INSTRUMENT"].strip()
        exch = row["EXCH_ID"].strip()
        if ins == "INDEX":
            for k in (row["UNDERLYING_SYMBOL"].strip(), row["SYMBOL_NAME"].strip()):
                index_by.setdefault((exch, k), row["SECURITY_ID"].strip())
        elif ins == "EQUITY":
            rank = 0 if row.get("SERIES", "").strip() == "EQ" else 1
            key = (exch, row["UNDERLYING_SYMBOL"].strip())
            if key not in equity_by or rank < equity_by[key][0]:
                equity_by[key] = (rank, row["SECURITY_ID"].strip())
        elif ins in DERIVATIVE_TYPES:
            raw_deriv.append(row)
    contracts: list[MasterContract] = []
    underlyings: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in raw_deriv:
        exch, mseg, ins = row["EXCH_ID"].strip(), row["SEGMENT"].strip(), row["INSTRUMENT"].strip()
        segment = SEGMENT_OF.get((exch, mseg))
        sid = row["SECURITY_ID"].strip()
        try:
            expiry = date.fromisoformat(row["SM_EXPIRY_DATE"].strip()[:10])
        except ValueError:
            skipped["bad_expiry"] += 1
            continue
        if segment is None or not sid.isdigit():
            skipped["unmapped_segment_or_id" if segment is None else "bad_security_id"] += 1
            continue
        sym = row["UNDERLYING_SYMBOL"].strip()
        category = CATEGORY_OF[ins]
        cash_id: str | None = None
        cash_seg: str | None = None
        if category == "index" and ins in ("FUTIDX", "OPTIDX") and (exch, sym) in index_by:
            cash_id, cash_seg = index_by[(exch, sym)], "IDX_I"
        elif category == "stock" and (exch, sym) in equity_by:
            cash_id, cash_seg = equity_by[(exch, sym)][1], f"{exch}_EQ"
        elif category in ("commodity", "index") and exch == "MCX":
            cash_id, cash_seg = (row["UNDERLYING_SECURITY_ID"].strip() or None), "MCX_COMM"     # MEASURED: the rolling API accepts the master's underlying id for MCX
        strike = _dec(row["STRIKE_PRICE"]) if ins in OPTION_TYPES else None
        otype = row["OPTION_TYPE"].strip() if ins in OPTION_TYPES else None
        if ins in OPTION_TYPES and (otype not in ("CE", "PE") or strike is None):
            skipped["bad_option_fields"] += 1
            continue
        c = MasterContract(exch, mseg, segment, ins, sid, sym, row["UNDERLYING_SECURITY_ID"].strip() or None, row["SYMBOL_NAME"].strip(), row.get("DISPLAY_NAME", "").strip() or None, expiry,
                           row.get("EXPIRY_FLAG", "").strip() or None, strike, otype, _dec(row["LOT_SIZE"]), _dec(row["TICK_SIZE"]), category, cash_id, cash_seg)
        contracts.append(c)
        u = underlyings.setdefault((exch, mseg, sym), {"exchange": exch, "master_segment": mseg, "segment": segment, "category": category, "underlying_symbol": sym,
                                                       "underlying_security_id": c.underlying_security_id, "cash_security_id": cash_id, "cash_segment": cash_seg, "display_name": sym,
                                                       "has_futures": False, "has_options": False})
        u["has_options" if c.is_option else "has_futures"] = True
    return MasterParse(contracts, underlyings, total, dict(skipped), h.hexdigest())


def fetch_master_lines(timeout: float = 60.0) -> Iterator[str]:
    import httpx
    with httpx.stream("GET", MASTER_URL, timeout=timeout, follow_redirects=False) as r:
        r.raise_for_status()
        for ln in r.iter_lines():
            yield ln + "\n"


# ───────────────────────── payload parsers (pure) ─────────────────────────
class PayloadError(ValueError):
    pass


@dataclass(frozen=True)
class CandleRow:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    open_interest: int | None


def _arrays(payload: Mapping[str, Any], required: tuple[str, ...]) -> dict[str, list]:
    if not isinstance(payload, Mapping):
        raise PayloadError("payload is not an object")
    arrs = {k: payload.get(k) for k in required}
    if any(not isinstance(v, list) for v in arrs.values()):
        raise PayloadError("payload arrays are missing")
    if len({len(v) for v in arrs.values()}) > 1:
        raise PayloadError("payload arrays do not align")
    return arrs


def _int(v: Any) -> int | None:
    return None if v is None else int(Decimal(str(v)))


class CandleBatch(list):
    """List of CandleRow plus the count/examples of provider candles skipped for internal inconsistency (never silently repaired)."""
    rejected: int = 0
    rejected_examples: list


def parse_candles(payload: Mapping[str, Any]) -> CandleBatch:
    """Dhan /v2/charts/intraday|historical response -> candles. A candle with inconsistent OHLC is SKIPPED and counted; open_interest is optional."""
    a = _arrays(payload, ("open", "high", "low", "close", "volume", "timestamp"))
    oi = payload.get("open_interest")
    if oi is not None and (not isinstance(oi, list) or len(oi) != len(a["open"])):
        raise PayloadError("open_interest array does not align")
    out = CandleBatch()
    out.rejected, out.rejected_examples = 0, []
    for i in range(len(a["open"])):
        try:
            o, h, l, c = (Decimal(str(a[k][i])) for k in ("open", "high", "low", "close"))
            ts = datetime.fromtimestamp(float(a["timestamp"][i]), timezone.utc)
            vol = _int(a["volume"][i]) or 0
            oival = _int(oi[i]) if oi else None
        except (InvalidOperation, ValueError, TypeError, OverflowError):
            raise PayloadError("unparseable candle values") from None
        if not (l <= min(o, c) and h >= max(o, c) and l <= h) or min(o, h, l, c) <= 0 or vol < 0:
            out.rejected += 1
            if len(out.rejected_examples) < 5:
                out.rejected_examples.append({"ts": ts.isoformat(), "ohlc": [str(o), str(h), str(l), str(c)]})
            continue
        out.append(CandleRow(ts, o, h, l, c, vol, oival))
    return out


@dataclass(frozen=True)
class RollingRow:
    ts: datetime
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: int | None
    open_interest: int | None
    iv: Decimal | None
    spot: Decimal | None
    strike: Decimal | None


def parse_rolling(payload: Mapping[str, Any], option_type: str) -> list[RollingRow]:
    """/v2/charts/rollingoption -> rows for CE or PE. An empty/absent side is an EMPTY result (not an error)."""
    side = payload.get("ce" if option_type == "CE" else "pe") if isinstance(payload, Mapping) else None
    if not side:
        return []
    ts = side.get("timestamp")
    if not isinstance(ts, list):
        raise PayloadError("rolling payload has no timestamp array")
    n = len(ts)
    cols = {k: side.get(k) for k in ("open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike")}
    for k, v in cols.items():
        if v is not None and (not isinstance(v, list) or len(v) != n):
            raise PayloadError(f"rolling array {k} does not align")
    def dv(k: str, i: int) -> Decimal | None:
        v = cols[k][i] if cols[k] is not None else None
        return None if v is None else Decimal(str(v))
    try:
        return [RollingRow(datetime.fromtimestamp(float(ts[i]), timezone.utc), dv("open", i), dv("high", i), dv("low", i), dv("close", i), _int(cols["volume"][i]) if cols["volume"] else None,
                           _int(cols["oi"][i]) if cols["oi"] else None, dv("iv", i), dv("spot", i), dv("strike", i)) for i in range(n)]
    except (InvalidOperation, ValueError, TypeError, OverflowError) as e:
        raise PayloadError(f"unparseable rolling row: {type(e).__name__}") from None


@dataclass(frozen=True)
class ChainQuote:
    strike: Decimal
    option_type: str
    security_id: str | None
    ltp: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int | None
    ask_qty: int | None
    volume: int | None
    oi: int | None
    previous_oi: int | None
    previous_volume: int | None
    iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None


def parse_chain(data: Mapping[str, Any]) -> tuple[Decimal | None, list[ChainQuote]]:
    """/v2/optionchain ``data`` -> (underlying price, quotes). Zero/missing bid or ask is None, never 0."""
    oc = data.get("oc") if isinstance(data, Mapping) else None
    if not isinstance(oc, Mapping):
        raise PayloadError("option-chain payload has no strike map")
    price = _dec(str(data.get("last_price"))) if data.get("last_price") is not None else None
    out: list[ChainQuote] = []
    for raw_strike, sides in oc.items():
        strike = _dec(str(raw_strike))
        if strike is None or not isinstance(sides, Mapping):
            raise PayloadError("option-chain strike is invalid")
        for key, typ in (("ce", "CE"), ("pe", "PE")):
            q = sides.get(key)
            if not isinstance(q, Mapping):
                continue
            g = q.get("greeks") if isinstance(q.get("greeks"), Mapping) else {}
            pos = lambda v: (lambda d: d if d is not None and d > 0 else None)(_dec(str(v)) if v is not None else None)  # noqa: E731
            out.append(ChainQuote(strike, typ, None if q.get("security_id") is None else str(q["security_id"]), _dec(str(q.get("last_price"))) if q.get("last_price") is not None else None,
                                  pos(q.get("top_bid_price")), pos(q.get("top_ask_price")), _int(q.get("top_bid_quantity")), _int(q.get("top_ask_quantity")), _int(q.get("volume")), _int(q.get("oi")),
                                  _int(q.get("previous_oi")), _int(q.get("previous_volume")), _dec(str(q.get("implied_volatility"))) if q.get("implied_volatility") is not None else None,
                                  *[(_dec(str(g.get(k))) if g.get(k) is not None else None) for k in ("delta", "gamma", "theta", "vega")]))
    return price, out


# ───────────────────────── windows ─────────────────────────
def intraday_windows(start: datetime, end: datetime, max_days: int = INTRADAY_MAX_DAYS - 1) -> list[tuple[datetime, datetime]]:
    """Split [start, end) into requests strictly under the 90-day limit."""
    if start >= end:
        return []
    out, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=max_days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def rolling_windows(first: date, last: date, max_span_days: int = ROLLING_MAX_SPAN_DAYS) -> list[tuple[date, date]]:
    """Non-overlapping (fromDate, toDate) pairs, toDate = fromDate + max_span_days (both inclusive on Dhan's side)."""
    if first > last:
        return []
    out, cur = [], first
    while cur <= last:
        out.append((cur, min(cur + timedelta(days=max_span_days), last)))
        cur += timedelta(days=max_span_days + 1)
    return out


def strike_offsets(max_offset: int) -> list[str]:
    return ["ATM"] + [f"ATM{sign}{n}" for n in range(1, max_offset + 1) for sign in ("+", "-")]


# ───────────────────────── client ─────────────────────────
class DhanDerivativesClient:
    """Authenticated Dhan calls for derivatives. Uses the existing adapter for headers, the shared 1 req/s data limiter and the 3 s option-chain limiter."""

    def __init__(self, adapter: DhanAdapter, tokens: Mapping[str, str]):
        self.adapter, self.tokens = adapter, tokens

    def _post(self, path: str, body: dict[str, Any], stage: str) -> Any:
        _wait_for_dhan_market_data()
        return _request("POST", f"{self.adapter.api_root}{path}", diagnostic_stage=stage,
                        headers={**self.adapter.account_headers(self.tokens["access_token"], self.tokens), "Accept": "application/json", "Content-Type": "application/json"}, json=body)

    def intraday(self, *, segment: str, security_id: str, instrument: str, start: datetime, end: datetime, oi: bool = True, interval: str = "1") -> list[CandleRow]:
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            raise BrokerAdapterError("invalid derivative history range")
        if end - start >= timedelta(days=INTRADAY_MAX_DAYS):
            raise BrokerAdapterError("Dhan intraday history is limited to 90 days per request", category="provider_window_exceeded")
        s, e = start.astimezone(IST), end.astimezone(IST)
        payload = self._post("/v2/charts/intraday", {"securityId": security_id, "exchangeSegment": segment, "instrument": instrument, "interval": interval, "oi": oi,
                                                      "fromDate": s.strftime("%Y-%m-%d %H:%M:%S"), "toDate": e.strftime("%Y-%m-%d %H:%M:%S")}, "derivatives_intraday")
        rows = parse_candles(payload)
        kept = CandleBatch(r for r in rows if start <= r.ts < end)
        kept.rejected, kept.rejected_examples = rows.rejected, rows.rejected_examples
        return kept

    def daily(self, *, segment: str, security_id: str, instrument: str, start: date, end: date, expiry_code: int = 0, oi: bool = True) -> list[CandleRow]:
        payload = self._post("/v2/charts/historical", {"securityId": security_id, "exchangeSegment": segment, "instrument": instrument, "expiryCode": expiry_code, "oi": oi,
                                                        "fromDate": start.isoformat(), "toDate": (end + timedelta(days=1)).isoformat()}, "derivatives_daily")
        return parse_candles(payload)

    def rolling(self, *, segment: str, cash_security_id: str, instrument: str, expiry_flag: str, expiry_code: int, strike: str, option_type: str, first: date, last: date,
                interval: str = "1") -> list[RollingRow]:
        if (last - first).days > ROLLING_MAX_SPAN_DAYS:
            raise BrokerAdapterError(f"rolling options window exceeds {ROLLING_MAX_SPAN_DAYS + 1} calendar days", category="provider_window_exceeded")
        body = {"exchangeSegment": segment, "interval": interval, "securityId": int(cash_security_id), "instrument": instrument, "expiryFlag": expiry_flag, "expiryCode": expiry_code, "strike": strike,
                "drvOptionType": "CALL" if option_type == "CE" else "PUT", "requiredData": ["open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike"],
                "fromDate": first.isoformat(), "toDate": last.isoformat()}
        payload = self._post("/v2/charts/rollingoption", body, "derivatives_rolling")
        data = payload.get("data") if isinstance(payload, Mapping) and "data" in payload else payload
        return parse_rolling(data, option_type)

    def expiries(self, cash_security_id: str, cash_segment: str) -> list[date]:
        raw = self.adapter.get_option_expiries(self.tokens["access_token"], self.tokens, int(cash_security_id), cash_segment)
        return sorted({date.fromisoformat(x) for x in raw})

    def chain(self, cash_security_id: str, cash_segment: str, expiry: date) -> tuple[Decimal | None, list[ChainQuote]]:
        return parse_chain(self.adapter.get_option_chain(self.tokens["access_token"], self.tokens, int(cash_security_id), cash_segment, expiry.isoformat()))
