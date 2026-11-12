"""The one transaction-cost engine. Backtests, trade analytics, the legacy daily backtest and the cost-estimate API all call this module.

Nothing else in the application may contain brokerage/STT/GST/stamp/exchange/SEBI/IPFT formulas.

Method (Dhan retail, India equity; rates are stored as PERCENT values and converted to fractions here, so 0.03 means 0.03%):
- Executions are grouped into ORDERS (``order_id``). Brokerage is charged once per order: MIN(flat, rate x that order's turnover). Several fills
  of one order never add brokerage again; two separate orders (e.g. two partial exits) each pay their own.
- Turnover is per executed quantity x executed price (slippage-inclusive prices are the executed prices). Buy and sell turnover are kept apart.
- STT on sell turnover, stamp duty on buy turnover, exchange transaction charge / SEBI / IPFT on total turnover, GST on the configured taxable
  components (brokerage + exchange transaction charge + SEBI; IPFT is NOT taxed here - verify).
- Rounding (Dhan convention): STT and stamp duty to the nearest rupee, everything else to 2 decimals, half-up, each component rounded once.
  Intermediate turnover and prices are never rounded.
- Intraday (MIS) and delivery are different products with different schedules. F&O is a separate category and is deliberately not computed here.
- Income tax is NOT a transaction cost: see ``indian_tax.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

D = Decimal
ZERO = D(0)
TWO = D("0.01")
RUPEE = D(1)
HUNDRED = D(100)
VALID_SIDES = ("buy", "sell")


class InvalidExecution(ValueError):
    pass


class UnsupportedProduct(ValueError):
    pass


@dataclass(frozen=True)
class Execution:
    order_id: str
    side: str            # "buy" | "sell" - the ACTUAL executed side, never inferred from a long/short label
    quantity: Decimal
    price: Decimal
    liquidity: str = "taker"   # crypto only: "maker" | "taker"


@dataclass(frozen=True)
class FeeSchedule:
    id: str
    label: str
    broker: str
    exchange: str          # NSE | BSE | DELTA
    product: str           # intraday | delivery | crypto_derivative
    kind: str              # "equity" | "crypto"
    effective_from: str
    source: str
    verification: str
    params: dict[str, Decimal]   # PERCENT values (0.03 = 0.03%) except *_flat which are rupees
    gst_on: tuple[str, ...] = ("brokerage", "exchange_txn", "sebi")

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "broker": self.broker, "exchange": self.exchange, "product": self.product, "market": "crypto" if self.kind == "crypto" else "equity_in",
                "effective_from": self.effective_from, "assumed": True, "source": self.source, "verification": self.verification, "source_note": self.verification,
                "gst_applies_to": list(self.gst_on), "params": {k: str(v) for k, v in self.params.items()}}


@dataclass
class ChargeBreakdown:
    schedule_id: str
    buy_turnover: Decimal
    sell_turnover: Decimal
    brokerage: Decimal = ZERO
    stt: Decimal = ZERO
    exchange_txn: Decimal = ZERO
    sebi: Decimal = ZERO
    ipft: Decimal = ZERO
    stamp_duty: Decimal = ZERO
    gst: Decimal = ZERO
    dp_charges: Decimal = ZERO
    other: Decimal = ZERO
    orders: list[dict[str, str]] = field(default_factory=list)

    @property
    def turnover(self) -> Decimal:
        return self.buy_turnover + self.sell_turnover

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.ipft + self.stamp_duty + self.gst + self.dp_charges + self.other

    @property
    def pct_of_turnover(self) -> Decimal:
        return (self.total / self.turnover * HUNDRED) if self.turnover else ZERO

    def as_dict(self) -> dict[str, Any]:
        return {"schedule_id": self.schedule_id, "buy_turnover": str(self.buy_turnover), "sell_turnover": str(self.sell_turnover), "turnover": str(self.turnover),
                "brokerage": str(self.brokerage), "stt": str(self.stt), "exchange_txn": str(self.exchange_txn), "sebi": str(self.sebi), "ipft": str(self.ipft),
                "stamp_duty": str(self.stamp_duty), "gst": str(self.gst), "dp_charges": str(self.dp_charges), "other": str(self.other), "total": str(self.total),
                "pct_of_turnover": str(self.pct_of_turnover.quantize(D("0.0001"), ROUND_HALF_UP)), "orders": self.orders}


def _pct(v: str) -> Decimal:
    return D(v)


def _eq(id_: str, label: str, exchange: str, product: str, params: dict[str, str], verification: str) -> FeeSchedule:
    return FeeSchedule(id_, label, "dhan", exchange, product, "equity", "2026-11-01", "Dhan published retail pricing (Nov 2026) as supplied by the owner",
                       verification, {k: _pct(v) for k, v in params.items()})


INTRADAY_COMMON = {"brokerage_flat": "20", "brokerage_pct": "0.03", "stt_sell_pct": "0.025", "stt_buy_pct": "0", "sebi_pct": "0.0001", "stamp_buy_pct": "0.003",
                   "ipft_pct": "0.0000001", "gst_pct": "18", "dp_flat": "0", "auto_square_off_flat": "0", "call_and_trade_flat": "0"}
DELIVERY_COMMON = {"brokerage_flat": "0", "brokerage_pct": "0", "stt_sell_pct": "0.1", "stt_buy_pct": "0.1", "sebi_pct": "0.0001", "stamp_buy_pct": "0.015",
                   "ipft_pct": "0.0000001", "gst_pct": "18", "dp_flat": "0", "auto_square_off_flat": "0", "call_and_trade_flat": "0"}
VERIFY_INTRADAY = ("Rates as listed in the owner's Nov-2026 Dhan schedule. Verify against a Dhan contract note: rounding of each component, whether GST applies to IPFT, "
                   "and the exact NSE/BSE transaction-charge rates.")
VERIFY_DELIVERY = ("ASSUMED delivery schedule (zero brokerage, STT 0.1% on both sides, stamp 0.015%); DP charge left at 0 and must be configured. Not verified against a contract note.")

SCHEDULES: dict[str, FeeSchedule] = {s.id: s for s in (
    _eq("dhan_nse_intraday_2026_11", "Dhan · NSE · Equity Intraday (MIS)", "NSE", "intraday", {**INTRADAY_COMMON, "exchange_txn_pct": "0.0030699"}, VERIFY_INTRADAY),
    _eq("dhan_bse_intraday_2026_11", "Dhan · BSE · Equity Intraday (MIS)", "BSE", "intraday", {**INTRADAY_COMMON, "exchange_txn_pct": "0.00375"}, VERIFY_INTRADAY),
    _eq("dhan_nse_delivery_2026_11", "Dhan · NSE · Equity Delivery", "NSE", "delivery", {**DELIVERY_COMMON, "exchange_txn_pct": "0.0030699"}, VERIFY_DELIVERY),
    _eq("dhan_bse_delivery_2026_11", "Dhan · BSE · Equity Delivery", "BSE", "delivery", {**DELIVERY_COMMON, "exchange_txn_pct": "0.00375"}, VERIFY_DELIVERY),
    FeeSchedule("dhan_nse_options_2026_11", "Dhan · NSE · Index options (buying), on premium turnover", "dhan", "NSE", "options", "equity", "2026-11-01", "Best-known NSE/Dhan options structure; rates NOT supplied by the owner",
                "UNVERIFIED DEFAULTS: brokerage flat Rs 20 per order, STT 0.1% on sell premium, NSE options transaction charge 0.03503%, SEBI 0.0001%, IPFT 0.0005%, stamp 0.003% on buy. "
                "STT and exchange rates for F&O change; confirm every rate against a Dhan contract note before trusting net P&L.",
                {"brokerage_flat": _pct("20"), "brokerage_pct": _pct("100"), "stt_sell_pct": _pct("0.1"), "stt_buy_pct": _pct("0"), "exchange_txn_pct": _pct("0.03503"), "sebi_pct": _pct("0.0001"),
                 "stamp_buy_pct": _pct("0.003"), "ipft_pct": _pct("0.0005"), "gst_pct": _pct("18"), "dp_flat": _pct("0"), "auto_square_off_flat": _pct("0"), "call_and_trade_flat": _pct("0")}),
    FeeSchedule("crypto_delta_india_2024", "Delta Exchange India derivatives (assumed)", "delta", "DELTA", "crypto_derivative", "crypto", "2024-01-01", "Assumed published fee tier",
                "Assumed taker 0.05% / maker 0.02% of notional plus 18% GST on fees. Confirm the current tier and product on the exchange.",
                {"taker_pct": _pct("0.05"), "maker_pct": _pct("0.02"), "gst_pct": _pct("18")}, gst_on=("exchange_fee",)),
)}
DEFAULT_SCHEDULE = {"equity_in": "dhan_nse_intraday_2026_11", "crypto": "crypto_delta_india_2024"}


def get_schedule(schedule_id: str | None = None, *, broker: str = "dhan", exchange: str = "NSE", product: str = "intraday") -> FeeSchedule:
    if schedule_id:
        try:
            return SCHEDULES[schedule_id]
        except KeyError:
            raise ValueError(f"unknown fee schedule {schedule_id!r}") from None
    if product in ("fno", "futures", "options"):
        raise UnsupportedProduct("F&O is a separate charge and tax category and is not calculated with equity schedules")
    for s in SCHEDULES.values():
        if s.broker == broker.lower() and s.exchange == exchange.upper() and s.product == product:
            return s
    raise ValueError(f"no fee schedule for broker={broker} exchange={exchange} product={product}")


def schedule_for_market(market: str, schedule_id: str | None = None) -> FeeSchedule:
    s = get_schedule(schedule_id or DEFAULT_SCHEDULE[market])
    if (market == "crypto") != (s.kind == "crypto"):
        raise ValueError(f"fee schedule {s.id!r} is not valid for market {market!r}")
    return s


def _round(x: Decimal, unit: Decimal) -> Decimal:
    return x.quantize(unit, rounding=ROUND_HALF_UP)


def _validate(executions: Iterable[Execution]) -> list[Execution]:
    out = list(executions)
    if not out:
        raise InvalidExecution("no executions")
    for e in out:
        if e.side not in VALID_SIDES:
            raise InvalidExecution(f"invalid side {e.side!r}")
        if not isinstance(e.quantity, Decimal) or not isinstance(e.price, Decimal):
            raise InvalidExecution("quantity and price must be Decimal (convert floats via string)")
        if not e.quantity.is_finite() or e.quantity <= 0:
            raise InvalidExecution("executed quantity must be positive")
        if not e.price.is_finite() or e.price <= 0:
            raise InvalidExecution("executed price must be positive")
        if not e.order_id:
            raise InvalidExecution("every execution needs an order_id")
    return out


def compute_charges(executions: Iterable[Execution], schedule: FeeSchedule, *, overrides: dict[str, Any] | None = None,
                    auto_square_off: bool = False, call_and_trade_orders: int = 0, dp_scrips_sold: int | None = None) -> ChargeBreakdown:
    """Charges for the given executions of ONE instrument/product. ``overrides`` replaces schedule params (same percent units)."""
    ex = _validate(executions)
    p = {**schedule.params, **{k: D(str(v)) for k, v in (overrides or {}).items() if k in schedule.params}}
    buy_t = sum((e.quantity * e.price for e in ex if e.side == "buy"), ZERO)
    sell_t = sum((e.quantity * e.price for e in ex if e.side == "sell"), ZERO)
    out = ChargeBreakdown(schedule.id, buy_t, sell_t)
    total_t = buy_t + sell_t
    frac = lambda k: p[k] / HUNDRED  # noqa: E731
    if schedule.kind == "crypto":
        fee = ZERO
        for e in ex:
            fee += e.quantity * e.price * frac("maker_pct" if e.liquidity == "maker" else "taker_pct")
        out.other = _round(fee, TWO)     # exchange fee, reported once
        out.gst = _round(out.other * frac("gst_pct"), TWO)
        return out
    orders: dict[str, Decimal] = {}
    for e in ex:
        orders[e.order_id] = orders.get(e.order_id, ZERO) + e.quantity * e.price
    brokerage = ZERO
    for oid, turnover in orders.items():       # once per ORDER, never per fill, share or candle
        b = _round(min(p["brokerage_flat"], turnover * frac("brokerage_pct")), TWO) if p["brokerage_flat"] > 0 or p["brokerage_pct"] > 0 else ZERO
        brokerage += b
        out.orders.append({"order_id": oid, "turnover": str(turnover), "brokerage": str(b)})
    out.brokerage = brokerage
    out.stt = _round(sell_t * frac("stt_sell_pct") + buy_t * frac("stt_buy_pct"), RUPEE)
    out.stamp_duty = _round(buy_t * frac("stamp_buy_pct"), RUPEE)
    out.exchange_txn = _round(total_t * frac("exchange_txn_pct"), TWO)
    out.sebi = _round(total_t * frac("sebi_pct"), TWO)
    out.ipft = _round(total_t * frac("ipft_pct"), TWO)
    if sell_t > 0 and p["dp_flat"] > 0 and schedule.product == "delivery":
        out.dp_charges = _round(p["dp_flat"] * D(dp_scrips_sold if dp_scrips_sold is not None else 1), TWO)
    if auto_square_off:   # only when Dhan's system actually squared the position off
        out.other += _round(p["auto_square_off_flat"], TWO)
    if call_and_trade_orders:
        out.other += _round(p["call_and_trade_flat"] * D(call_and_trade_orders), TWO)
    taxable = {"brokerage": out.brokerage, "exchange_txn": out.exchange_txn, "sebi": out.sebi, "ipft": out.ipft}
    out.gst = _round(sum((taxable[k] for k in schedule.gst_on), ZERO) * frac("gst_pct"), TWO)
    return out


def trade_executions(side: str, quantity: Decimal, entry_price: Decimal, exit_price: Decimal, *, liquidity_exit: str = "taker") -> list[Execution]:
    """Entry order then exit order with the ACTUAL executed sides: a long buys then sells, a short sells then buys."""
    if side not in ("long", "short"):
        raise InvalidExecution(f"invalid trade side {side!r}")
    first, second = ("buy", "sell") if side == "long" else ("sell", "buy")
    return [Execution("entry", first, quantity, entry_price), Execution("exit", second, quantity, exit_price, liquidity_exit)]


def gross_pnl(side: str, quantity: Decimal, entry_price: Decimal, exit_price: Decimal) -> Decimal:
    return (exit_price - entry_price) * quantity if side == "long" else (entry_price - exit_price) * quantity


def trade_charges(side: str, quantity: Decimal, entry_price: Decimal, exit_price: Decimal, schedule: FeeSchedule, **kw: Any) -> ChargeBreakdown:
    return compute_charges(trade_executions(side, quantity, entry_price, exit_price), schedule, **kw)


def per_unit_round_trip(side: str, entry_price: Decimal, exit_price: Decimal, schedule: FeeSchedule, reference_notional: Decimal = D(100000)) -> Decimal:
    """Charges per share for an analytical one-unit trade (legacy daily backtest): costed on a realistic reference position, then scaled down,
    so the brokerage cap and rupee rounding apply to a real order rather than to a single share."""
    qty = max(D(1), (reference_notional / entry_price).to_integral_value(rounding="ROUND_FLOOR"))
    return trade_charges(side, qty, entry_price, exit_price, schedule).total / qty
