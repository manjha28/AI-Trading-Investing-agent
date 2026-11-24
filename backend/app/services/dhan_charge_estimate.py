"""Estimated charges for Dhan fills the broker has not supplied charges for yet (today's trades).

An estimate is NEVER stored and never replaces a real charge: it is applied to a copy of the row for display, flagged `charges_estimated`, and
the journal labels every fee and net P&L built on it as an estimate. Rates are the app's own Dhan schedules (transaction_costs); they are
unverified defaults, so the broker's contract note remains the authority. Fills whose product the schedules do not cover stay unknown.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal
from types import SimpleNamespace
from typing import Any

from app.services import transaction_costs as costs

CENT = Decimal("0.01")
OPTIONS_SCHEDULE = "dhan_nse_options_2026_11"


def _schedule_for(row: Any, is_option: Callable[[Any], bool]) -> str | None:
    segment = (row.exchange_segment or "").upper()
    if segment == "NSE_FNO" and is_option(row):
        return OPTIONS_SCHEDULE
    if segment == "NSE_EQ":
        return costs.DEFAULT_SCHEDULE["equity_in"]            # intraday assumed; delivery charges differ
    return None


def estimate_missing_charges(rows: list[Any], is_option: Callable[[Any], bool]) -> list[Any]:
    """Return the rows with an estimated `charges` on copies of those that have none. Rows with real charges are returned untouched."""
    # An order with ANY real charge from Dhan is never estimated: its remaining fills stay unknown rather than getting a second, double-counted brokerage.
    actual_orders = {str(r.order_id or r.trade_id) for r in rows if r.charges is not None}
    groups: dict[tuple[str, str], list[int]] = {}
    for i, row in enumerate(rows):
        if str(row.order_id or row.trade_id) in actual_orders:
            continue
        if row.charges is not None or row.quantity is None or row.price is None or row.price <= 0 or row.side not in ("BUY", "SELL"):
            continue
        schedule = _schedule_for(row, is_option)
        if schedule is None:
            continue
        groups.setdefault((schedule, str(row.order_id or row.trade_id)), []).append(i)
    out = list(rows)
    for (schedule_id, order_id), indexes in groups.items():
        fills = [rows[i] for i in indexes]
        executions = [costs.Execution(order_id, "buy" if f.side == "BUY" else "sell", Decimal(f.quantity), Decimal(f.price)) for f in fills]
        try:
            total = costs.compute_charges(executions, costs.get_schedule(schedule_id)).total       # brokerage once per order, then allocated by turnover
        except (costs.InvalidExecution, costs.UnsupportedProduct, KeyError, ValueError):
            continue
        turnovers = [Decimal(f.quantity) * Decimal(f.price) for f in fills]
        whole = sum(turnovers, Decimal(0))
        if whole <= 0:
            continue
        allocated = Decimal(0)
        for n, (i, fill, turnover) in enumerate(zip(indexes, fills, turnovers)):
            share = (total - allocated) if n == len(fills) - 1 else (total * turnover / whole).quantize(CENT, rounding=ROUND_HALF_EVEN)
            allocated += share
            copy = SimpleNamespace(**{c.key: getattr(fill, c.key) for c in fill.__table__.columns})
            copy.charges, copy.charges_estimated = share, True
            out[i] = copy
    return out
