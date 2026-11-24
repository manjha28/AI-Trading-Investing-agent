"""Why the journal's net P&L for a day differs from the Dhan "Closed today" realised figure, shown as numbers instead of left as a mystery.

Dhan's realised profit is GROSS (before brokerage, STT, exchange charges, GST and stamp duty). The journal subtracts charges: real ones when Dhan has supplied them, estimated ones
(labelled) when it has not. So  journal net = journal gross - fees and taxes,  and  Dhan realised - journal gross  is what is left unexplained (basis differences such as
average-price vs FIFO lots, fills from before the sync window, or a stale position snapshot). Read-only.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.research import require_verified_session
from app.api.trades import _OPTION_PATTERN, _dhan_trade_rows, _match_dhan_fills
from app.db import get_db
from app.models import BrokerMirror, UserSession
from app.models.strategies import OptionContract

router = APIRouter(prefix="/brokers/dhan", tags=["dhan-reconciliation"])
IST = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")
ZERO = Decimal(0)


def _q(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(CENT))


def build(db: Session, user_id: Any, day: date) -> dict[str, Any]:
    rows = _dhan_trade_rows(db, user_id)
    by_id = {str(r.id): r for r in rows}
    matching = _match_dhan_fills(rows)
    gross = fees_real = fees_est = net = ZERO
    trades = unknown_fee = est_trades = 0
    per_security: dict[str, dict[str, Any]] = {}
    for m in matching["matches"]:
        if m["exit_time"].astimezone(IST).date() != day:
            continue
        entry, exit_fill = by_id.get(m["entry_fill_id"]), by_id.get(m["exit_fill_id"])
        if entry is None or exit_fill is None:
            continue
        qty, sign = m["quantity"], (1 if m["side"] == "long" else -1)
        g = (exit_fill.price - entry.price) * qty * sign
        gross += g
        trades += 1
        sec = per_security.setdefault(str(exit_fill.security_id or exit_fill.symbol), {"symbol": exit_fill.symbol, "journal_gross": ZERO, "dhan_realised": None})
        sec["journal_gross"] += g
        if m["fees"] is None or m["net_pnl"] is None:
            unknown_fee += 1
            continue
        estimated = bool(getattr(entry, "charges_estimated", False) or getattr(exit_fill, "charges_estimated", False))
        est_trades += estimated
        if estimated:
            fees_est += m["fees"]
        else:
            fees_real += m["fees"]
        net += m["net_pnl"]
    # Dhan: the realised profit of the latest synced positions snapshot
    latest = db.scalar(select(func.max(BrokerMirror.synced_at)).where(BrokerMirror.user_id == user_id, BrokerMirror.broker == "dhan", BrokerMirror.kind == "position"))
    dhan_realised: Decimal | None = None
    positions = []
    if latest is not None:
        positions = list(db.scalars(select(BrokerMirror).where(BrokerMirror.user_id == user_id, BrokerMirror.broker == "dhan", BrokerMirror.kind == "position", BrokerMirror.synced_at == latest)))
        known = [p.realized_profit for p in positions if p.realized_profit is not None]
        dhan_realised = sum(known, ZERO) if known else None
        for p in positions:
            sec = per_security.setdefault(str(p.security_id or p.symbol), {"symbol": p.symbol, "journal_gross": ZERO, "dhan_realised": None})
            sec["dhan_realised"] = (sec["dhan_realised"] or ZERO) + p.realized_profit if p.realized_profit is not None else sec["dhan_realised"]
    unexplained = None if dhan_realised is None else dhan_realised - gross
    # net quantity per contract: the journal's fills against Dhan's position, to explain open-lot differences
    fill_net: dict[str, Decimal] = {}
    for r in rows:
        key = str(r.security_id or r.symbol)
        fill_net[key] = fill_net.get(key, ZERO) + (r.quantity if r.side == "BUY" else -r.quantity)
    dhan_net = {str(p.security_id or p.symbol): (p.quantity if (p.position_type or "").upper() != "SHORT" else -p.quantity) for p in positions}
    qty_differences = [{"security_id": k, "journal_net_quantity": format(fill_net.get(k, ZERO).normalize(), "f"), "dhan_net_quantity": format(dhan_net.get(k, ZERO).normalize(), "f")}
                       for k in sorted(set(fill_net) | set(dhan_net)) if k in dhan_net and fill_net.get(k, ZERO) != dhan_net[k]]
    # contracts the stored Dhan master does not list (expired weeklies drop out of the master)
    missing: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        if (r.exchange_segment or "").upper() not in ("NSE_FNO", "BSE_FNO") or not r.security_id or r.security_id in seen:
            continue
        seen.add(r.security_id)
        if db.scalar(select(func.count()).select_from(OptionContract).where(OptionContract.security_id == r.security_id)) == 0:
            m = _OPTION_PATTERN.search(r.symbol or "")
            missing.append({"symbol": r.symbol, "security_id": r.security_id, "first_trade": r.trade_at.astimezone(IST).date().isoformat(),
                            "reason": "not in the stored Dhan master: the contract expired and expired contracts drop out of Dhan's master file" + (f" (expiry {m.group('day')} {m.group('month').title()})" if m else "")})
    return {
        "day": day.isoformat(), "currency": "INR", "trades_matched_that_day": trades,
        "dhan": {"realised": _q(dhan_realised), "basis": "gross: before brokerage, STT, exchange charges, GST and stamp duty", "snapshot_at": latest.isoformat() if latest else None},
        "journal": {"gross": _q(gross), "fees_and_taxes_real": _q(fees_real), "fees_and_taxes_estimated": _q(fees_est), "net": _q(net), "trades_with_estimated_charges": est_trades, "trades_with_unknown_charges": unknown_fee},
        "unexplained": _q(unexplained), "reconciles_within_1_rupee": None if unexplained is None else abs(unexplained) <= 1,
        "by_contract": [{"symbol": v["symbol"], "journal_gross": _q(v["journal_gross"]), "dhan_realised": _q(v["dhan_realised"]), "difference": _q(None if v["dhan_realised"] is None else v["dhan_realised"] - v["journal_gross"])} for v in sorted(per_security.values(), key=lambda v: v["symbol"] or "")],
        "open_lot_differences": qty_differences,
        "contracts_without_master": missing,
        "notes": ["Dhan's realised figure is gross; the journal's net subtracts fees and taxes. Charges Dhan has not supplied yet are estimated from the app's own fee schedule (unverified rates), so check the contract note.",
                  "Dhan values a position at its average price; the journal matches fills first-in first-out, so a day with several partial fills can differ by a few rupees per contract."],
        "disclaimer": "Decision support, not investment advice.",
    }


@router.get("/reconciliation")
def reconciliation(day: date | None = Query(default=None), session: UserSession = Depends(require_verified_session), db: Session = Depends(get_db)) -> dict[str, Any]:
    return build(db, session.user_id, day or datetime.now(timezone.utc).astimezone(IST).date())
