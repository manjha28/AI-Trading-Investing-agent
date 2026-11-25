"""Thin adapter from VAR contracts to the single authoritative calculator (app.services.indian_charges). NO rates live here."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services import indian_charges as ic

from .config import ChargeCfg
from .vtypes import Contract


def order_charges(contract: Contract, cfg: ChargeCfg, day: date, order_id: str, side: str, price: Decimal, units: Decimal) -> ic.ChargeBreakdown:
    """One executed order. ``units`` are UNDERLYING UNITS (lots x lot_size already applied exactly once by the caller)."""
    return ic.calculate_trade_charges(exchange=cfg.exchange, segment=contract.segment, instrument_type=contract.instrument_type, broker_profile=cfg.broker_profile,
                                      trading_date=day, order_id=order_id, side=side, execution_price=price, executed_quantity=units,
                                      fee_schedule_version=cfg.fee_schedule_version)


def round_trip_estimate(contract: Contract, cfg: ChargeCfg, day: date, direction: str, entry: Decimal, exit_: Decimal, units: Decimal) -> dict:
    """Entry order + one exit order (a pre-trade estimate; real trades may add partial-exit orders)."""
    first, second = ("buy", "sell") if direction == "LONG" else ("sell", "buy")
    return ic.sum_charges([order_charges(contract, cfg, day, "entry", first, entry, units), order_charges(contract, cfg, day, "exit", second, exit_, units)])
