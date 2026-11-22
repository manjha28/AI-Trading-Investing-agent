"""Writers for the dated, append-only point-in-time stores (Phase 6 step 1). Insert-only: nothing here updates or deletes. A failure to write a snapshot is logged and never breaks the refresh that triggered it."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.fundamental_snapshots import FundamentalSnapshot, ShareholdingSnapshot

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
VALUE_FIELDS = ("revenue", "net_income", "eps", "operating_margin", "total_assets", "total_liabilities", "total_equity", "operating_cash_flow", "free_cash_flow", "total_debt", "shares_outstanding")


def record_fundamental_snapshot(db: Session, instrument_id: Any, rows: list[Any], valuation_inputs: dict[str, Any] | None = None, *, source: str = "yfinance", now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        periods = [{"period_type": r.period_type, "period_end": r.period_end.isoformat(), "currency": r.currency, "source": r.source, "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
                    "statement_basis": r.statement_basis, "values": {f: (None if getattr(r, f) is None else str(getattr(r, f))) for f in VALUE_FIELDS}, "extra_metrics": r.extra_metrics or {}} for r in rows]
        db.add(FundamentalSnapshot(instrument_id=instrument_id, captured_on=now.astimezone(IST).date(), captured_at=now, source=source,
                                   payload={"periods": periods, "valuation_inputs": _plain(valuation_inputs or {})}))
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - never break the refresh that called us
        db.rollback()
        logger.warning("fundamental_snapshot_failed error_class=%s", type(exc).__name__)
        return False


def record_shareholding_snapshot(db: Session, instrument_id: Any, snapshot: dict[str, Any], *, source: str = "dhan", now: datetime | None = None) -> bool:
    """One row per instrument per day per source from the Dhan snapshot's shareholding section; Dhan gives no pledge figure, so pledged stays NULL (unknown, never zero)."""
    now = now or datetime.now(timezone.utc)
    sh = (snapshot or {}).get("shareholding") or {}
    if sh.get("status") != "available":
        return False
    pct = {h["key"]: _dec(h.get("percent")) for h in sh.get("holdings") or []}
    day = now.astimezone(IST).date()
    try:
        if db.scalar(select(ShareholdingSnapshot.id).where(ShareholdingSnapshot.instrument_id == instrument_id, ShareholdingSnapshot.captured_on == day, ShareholdingSnapshot.source == source)) is not None:
            return False
        db.add(ShareholdingSnapshot(instrument_id=instrument_id, captured_on=day, captured_at=now, source=source, promoter_pct=pct.get("promoter"), fii_pct=pct.get("fii"), dii_pct=pct.get("dii"),
                                    public_pct=pct.get("public"), pledged_pct_of_promoter=None))
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("shareholding_snapshot_failed error_class=%s", type(exc).__name__)
        return False


def _dec(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _plain(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return str(x) if isinstance(x, (Decimal, datetime)) else x
