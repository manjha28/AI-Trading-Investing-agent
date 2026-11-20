"""THE authoritative Indian trading-charge calculator (NSE equity cash intraday, NSE index futures, NSE stock futures).

Every consumer (VAR, scalp_hold, backtests, replay, reports, API/UI) must call ``calculate_trade_charges``; there is no other
home for these rates. Rules:

* One call = one executed ORDER. Brokerage is charged once per order (not per lot, not per partial fill inside the order);
  turnover-based charges use the order's aggregated fills. Entry + partial exit + final exit = three orders = three brokerages.
* turnover = execution price x executed quantity in UNDERLYING UNITS. Lots are converted exactly once (``quantity_in_lots``).
* Rates are effective-dated PER COMPONENT and selected by the execution date. A date before the first verified rate raises
  ``ScheduleUnavailable``; nothing is extrapolated and nothing silently becomes zero.
* Segments never cross: a cash schedule cannot price a futures instrument and vice versa (``SegmentMismatch``).
* Decimal only. Rounding follows Dhan's contract-note convention (STT and stamp duty to the nearest rupee, all else to 2dp);
  raw and rounded amounts are both returned. Contract notes round on day aggregates, so per-order rounding is an approximation.
* Every rate carries a verification status; unverified components are listed in ``unverified_components`` and must be shown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable

D = Decimal
HUNDRED = D(100)
SCHEDULE_VERSION = "IN-NSE-DHAN-2026.11"
ROUNDING_METHOD = "STT and stamp duty: nearest rupee (half up); all other components: 2dp (half up); raw amounts retained"
LAST_VERIFIED = date(2026, 11, 17)

VERIFIED_PRIMARY = "verified_primary"        # read from the broker/exchange page by this project
VERIFIED_SECONDARY = "verified_secondary"    # consistent across independent secondary sources; primary NSE circular not retrievable
OWNER_STATED = "owner_stated"                # supplied by the owner in the spec; not independently verified
UNVERIFIED = "unverified"                    # no source; value is a placeholder and MUST be surfaced

SOURCES = {
    "dhan": "https://dhan.co/pricing/",
    "nse_circular": "https://nsearchives.nseindia.com/content/circulars/FA73061.pdf",
    "nse_levies": "https://www.nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies",
    "nse_stt": "https://www.nseindia.in/static/products-services/equity-derivatives-securities-transaction-tax",
}


class ChargeError(ValueError):
    pass


class ScheduleUnavailable(ChargeError):
    """No verified/declared rate for this component on this date (or unknown segment/broker/version)."""


class SegmentMismatch(ChargeError):
    """A schedule for one segment was requested for an instrument of another segment."""


class InvalidExecution(ChargeError):
    pass


@dataclass(frozen=True)
class RateRow:
    effective_from: date
    value: Decimal               # percent of turnover (0.05 = 0.05%), or rupees when ``flat``
    status: str
    source: str
    note: str = ""


def _r(y: int, m: int, d: int, value: str, status: str, source: str, note: str = "") -> RateRow:
    return RateRow(date(y, m, d), D(value), status, source, note)


_START = (2024, 10, 1)   # earliest date for which any component here has a declared rate

# ---- shared timelines (percent of turnover) -------------------------------------------------------------------------
_SEBI = [_r(*_START, "0.0001", VERIFIED_PRIMARY, SOURCES["dhan"], "Rs 10/crore")]
_GST = [_r(*_START, "18", VERIFIED_PRIMARY, SOURCES["dhan"], "on brokerage + exchange + IPFT + SEBI; NOT on STT or stamp duty")]
_IPFT = [_r(*_START, "0.0000001", VERIFIED_PRIMARY, SOURCES["dhan"], "Rs 0.01/crore, charged in addition to the exchange rate; the NSE 'Rs 183/crore' futures total = 182.99 + 0.01")]
_FUT_IPFT = [
    _r(*_START, "0", UNVERIFIED, SOURCES["nse_circular"], "before 2026-03-01 IPFT was part of the pre-revision rate; separate amount unknown (0 here, flagged)"),
    _r(2026, 3, 1, "0.0000001", VERIFIED_SECONDARY, SOURCES["nse_circular"], "Rs 0.01/crore"),
]

_SCHEDULES: dict[str, dict] = {
    "equity_cash": {
        "instrument_types": ("cash",),
        "transaction_types": ("intraday",),
        "brokerage": [_r(*_START, "20", VERIFIED_PRIMARY, SOURCES["dhan"], "Rs 20 or 0.03% of trade value per executed order, whichever is LOWER (history before 2026-11 unchecked)")],
        "brokerage_pct_cap": D("0.03"),
        "stt_sell": [_r(*_START, "0.025", VERIFIED_PRIMARY, SOURCES["dhan"], "intraday rate on SELL; not the 0.1% delivery rate")],
        "exchange": [
            _r(*_START, "0.00297", VERIFIED_SECONDARY, "https://www.business-standard.com/amp/markets/stock-market-news/nse-and-bse-revise-their-transaction-fees-to-comply-with-sebi-circular-124092701207_1.html", "Rs 2.97/lakh"),
            _r(2026, 3, 1, "0.0030699", VERIFIED_PRIMARY, SOURCES["dhan"], "Rs 306.99/crore (NSE revision effective 2026-03-01)"),
        ],
        "ipft": _IPFT, "sebi": _SEBI,
        "stamp_buy": [_r(*_START, "0.003", VERIFIED_PRIMARY, SOURCES["dhan"], "intraday rate on BUY")],
        "gst": _GST,
        "dp": [_r(*_START, "0", VERIFIED_PRIMARY, SOURCES["dhan"], "DP charge applies to delivery sells only; none for intraday")],
    },
    "futures": {
        "instrument_types": ("index_future", "stock_future"),
        "transaction_types": ("intraday", "carry"),
        "brokerage": [_r(*_START, "20", VERIFIED_PRIMARY, SOURCES["dhan"], "Rs 20 per executed order, flat (history before 2026-11 unchecked)")],
        "brokerage_pct_cap": None,
        "stt_sell": [
            _r(*_START, "0.02", OWNER_STATED, SOURCES["nse_stt"], "futures STT on SELL before 2026-04-01"),
            _r(2026, 4, 1, "0.05", VERIFIED_SECONDARY, SOURCES["nse_stt"], "futures STT on SELL from 2026-04-01 (several independent secondary sources)"),
        ],
        "exchange": [
            _r(*_START, "0.00173", VERIFIED_SECONDARY, "https://www.outlookbusiness.com/markets/nse-bse-revise-transaction-charges-effective-from-october-1", "Rs 1.73/lakh"),
            _r(2026, 3, 1, "0.0018299", VERIFIED_SECONDARY, SOURCES["nse_circular"], "Rs 182.99/crore (+ IPFT Rs 0.01 = Rs 183/crore); NSE circular PDF not retrievable, secondary sources agree"),
        ],
        "ipft": _FUT_IPFT, "sebi": _SEBI,
        "stamp_buy": [_r(*_START, "0.002", OWNER_STATED, SOURCES["nse_levies"], "futures stamp duty on BUY")],
        "gst": _GST,
        "dp": [_r(*_START, "0", VERIFIED_PRIMARY, SOURCES["dhan"], "not applicable to futures")],
    },
}
_BROKERS = ("dhan",)
_EXCHANGES = ("NSE",)
_COMPONENTS = ("brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst", "dp")


def _pick(rows: list[RateRow], day: date, what: str) -> RateRow:
    best = None
    for row in rows:
        if row.effective_from <= day and (best is None or row.effective_from > best.effective_from):
            best = row
    if best is None:
        raise ScheduleUnavailable(f"no declared rate for {what} on {day.isoformat()} (earliest {min(r.effective_from for r in rows).isoformat()})")
    return best


def resolve_schedule(exchange: str, segment: str, instrument_type: str, broker_profile: str, trading_date: date,
                     transaction_type: str = "intraday", fee_schedule_version: str | None = None) -> dict[str, RateRow]:
    """Component -> RateRow for the execution date. Raises instead of defaulting."""
    if exchange.upper() not in _EXCHANGES:
        raise ScheduleUnavailable(f"exchange {exchange!r} has no schedule")
    if broker_profile.lower() not in _BROKERS:
        raise ScheduleUnavailable(f"broker profile {broker_profile!r} has no schedule")
    if fee_schedule_version is not None and fee_schedule_version != SCHEDULE_VERSION:
        raise ScheduleUnavailable(f"fee schedule version {fee_schedule_version!r} is unknown (have {SCHEDULE_VERSION})")
    sched = _SCHEDULES.get(segment)
    if sched is None:
        raise ScheduleUnavailable(f"segment {segment!r} has no schedule")
    if instrument_type not in sched["instrument_types"]:
        raise SegmentMismatch(f"segment {segment!r} cannot price instrument type {instrument_type!r}")
    if transaction_type not in sched["transaction_types"]:
        raise ScheduleUnavailable(f"transaction type {transaction_type!r} is not supported for {segment} (intraday/MIS only for cash)")
    return {
        "brokerage": _pick(sched["brokerage"], trading_date, "brokerage"), "stt": _pick(sched["stt_sell"], trading_date, "STT"),
        "exchange": _pick(sched["exchange"], trading_date, "exchange transaction charge"), "ipft": _pick(sched["ipft"], trading_date, "IPFT"),
        "sebi": _pick(sched["sebi"], trading_date, "SEBI turnover fee"), "stamp": _pick(sched["stamp_buy"], trading_date, "stamp duty"),
        "gst": _pick(sched["gst"], trading_date, "GST"), "dp": _pick(sched["dp"], trading_date, "DP charge"),
    }


@dataclass(frozen=True)
class ChargeBreakdown:
    order_id: str
    side: str
    segment: str
    instrument_type: str
    trading_date: date
    schedule_version: str
    executed: bool
    turnover: Decimal
    units: Decimal
    raw: dict[str, Decimal]
    rounded: dict[str, Decimal]
    rates: dict[str, dict]        # component -> {rate, effective_from, status, source}
    rounding_method: str = ROUNDING_METHOD
    unverified_components: tuple[str, ...] = ()

    @property
    def total_raw(self) -> Decimal:
        return sum(self.raw.values(), D(0))

    @property
    def total(self) -> Decimal:
        return sum(self.rounded.values(), D(0))

    @property
    def verified(self) -> bool:
        return not self.unverified_components

    def as_dict(self) -> dict:
        return {"order_id": self.order_id, "side": self.side, "segment": self.segment, "instrument_type": self.instrument_type, "trading_date": self.trading_date.isoformat(),
                "schedule_version": self.schedule_version, "turnover": str(self.turnover), "units": str(self.units), "raw": {k: str(v) for k, v in self.raw.items()},
                "rounded": {k: str(v) for k, v in self.rounded.items()}, "total_raw": str(self.total_raw), "total": str(self.total), "rates": self.rates,
                "rounding_method": self.rounding_method, "unverified_components": list(self.unverified_components)}


def _round_rupee(x: D) -> D:
    return x.quantize(D(1), rounding=ROUND_HALF_UP)


def _round_paise(x: D) -> D:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def calculate_trade_charges(*, exchange: str, segment: str, instrument_type: str, broker_profile: str, trading_date: date, order_id: str, side: str,
                            execution_price: Decimal | None = None, executed_quantity: Decimal | None = None, lot_size: int | Decimal | None = None,
                            order_status: str = "executed", transaction_type: str = "intraday", fee_schedule_version: str | None = None,
                            fills: Iterable[tuple[Decimal, Decimal]] | None = None, quantity_in_lots: bool = False) -> ChargeBreakdown:
    """Charges for ONE executed order. ``fills`` (price, quantity) aggregates several fills of the same order (brokerage still once).
    ``executed_quantity`` is in underlying units unless ``quantity_in_lots`` (then multiplied by ``lot_size`` exactly once)."""
    if side not in ("buy", "sell"):
        raise InvalidExecution("side must be 'buy' or 'sell'")
    rows = resolve_schedule(exchange, segment, instrument_type, broker_profile, trading_date, transaction_type, fee_schedule_version)
    if order_status != "executed":     # no execution, no charge: an explicit zero with the reason, never a silent default for executed orders
        zero = {c: D(0) for c in _COMPONENTS}
        return ChargeBreakdown(order_id, side, segment, instrument_type, trading_date, SCHEDULE_VERSION, False, D(0), D(0), zero, dict(zero), {}, ROUNDING_METHOD, ())
    legs = list(fills) if fills is not None else ([(execution_price, executed_quantity)] if execution_price is not None and executed_quantity is not None else [])
    if not legs:
        raise InvalidExecution("an executed order needs a price and quantity (or fills)")
    if quantity_in_lots and (lot_size is None or D(lot_size) <= 0):
        raise InvalidExecution("lot_size is required (and must be positive) to convert lots to underlying units")
    mult = D(lot_size) if quantity_in_lots else D(1)
    turnover = D(0)
    units = D(0)
    for price, qty in legs:
        if price is None or qty is None or D(price) <= 0 or D(qty) <= 0:
            raise InvalidExecution("price and quantity must be positive")
        turnover += D(price) * D(qty) * mult
        units += D(qty) * mult
    sched = _SCHEDULES[segment]
    pct = lambda comp: rows[comp].value  # noqa: E731
    flat = rows["brokerage"].value
    cap = sched["brokerage_pct_cap"]
    brokerage = flat if cap is None else min(flat, turnover * cap / HUNDRED)
    exch = turnover * pct("exchange") / HUNDRED
    ipft = turnover * pct("ipft") / HUNDRED
    sebi = turnover * pct("sebi") / HUNDRED
    stt = turnover * pct("stt") / HUNDRED if side == "sell" else D(0)
    stamp = turnover * pct("stamp") / HUNDRED if side == "buy" else D(0)
    gst_base = brokerage + exch + ipft + sebi          # NOT STT, NOT stamp duty
    gst = gst_base * pct("gst") / HUNDRED
    dp = D(0) * pct("dp")
    raw = {"brokerage": brokerage, "stt": stt, "exchange": exch, "ipft": ipft, "sebi": sebi, "stamp": stamp, "gst": gst, "dp": dp}
    rounded = {k: (_round_rupee(v) if k in ("stt", "stamp") else _round_paise(v)) for k, v in raw.items()}
    rates = {k: {"rate": str(rows[k].value), "effective_from": rows[k].effective_from.isoformat(), "status": rows[k].status, "source": rows[k].source, "note": rows[k].note}
             for k in _COMPONENTS}
    unv = tuple(k for k in _COMPONENTS if rows[k].status in (UNVERIFIED, OWNER_STATED))
    return ChargeBreakdown(order_id, side, segment, instrument_type, trading_date, SCHEDULE_VERSION, True, turnover, units, raw, rounded, rates, ROUNDING_METHOD, unv)


def _round_component(k: str, v: D) -> D:
    return _round_rupee(v) if k in ("stt", "stamp") else _round_paise(v)


def sum_charges(orders: Iterable[ChargeBreakdown]) -> dict:
    """Aggregate per-order breakdowns for one trade/run. Rounding is applied to the AGGREGATED raw amount (a contract note rounds on
    aggregates), so splitting an exit into partial orders adds exactly the extra brokerage/GST and no spurious per-order rounding."""
    orders = list(orders)
    raw = {k: sum((o.raw[k] for o in orders), D(0)) for k in _COMPONENTS}
    rounded = {k: _round_component(k, v) for k, v in raw.items()}
    return {"orders": len(orders), "turnover": sum((o.turnover for o in orders), D(0)), "raw": raw, "rounded": rounded,
            "total": sum(rounded.values(), D(0)), "total_raw": sum(raw.values(), D(0)),
            "unverified_components": sorted({c for o in orders for c in o.unverified_components}), "schedule_version": SCHEDULE_VERSION,
            "rounding_method": ROUNDING_METHOD}


def schedule_report(segment: str, instrument_type: str, on: date, broker_profile: str = "dhan", exchange: str = "NSE", transaction_type: str = "intraday") -> dict:
    """The full applicable schedule for display (UI 'Indian Trading Charges' panel). Raises if unavailable: never a partial or zero schedule."""
    rows = resolve_schedule(exchange, segment, instrument_type, broker_profile, on, transaction_type)
    cap = _SCHEDULES[segment]["brokerage_pct_cap"]
    return {"schedule_version": SCHEDULE_VERSION, "broker": broker_profile, "exchange": exchange, "segment": segment, "instrument_type": instrument_type, "as_of": on.isoformat(),
            "last_verified": LAST_VERIFIED.isoformat(), "rounding_method": ROUNDING_METHOD, "brokerage_pct_cap": str(cap) if cap is not None else None,
            "components": {k: {"rate": str(r.value), "unit": "rupees per executed order" if k == "brokerage" else "percent of turnover", "effective_from": r.effective_from.isoformat(),
                               "status": r.status, "source": r.source, "note": r.note} for k, r in rows.items()}}
