"""Detect and look up session windows a provider does not serve for an instrument.

A window is recorded only when it is a consistent provider limitation, not a missed download: across at least ``MIN_SESSIONS`` complete
trading sessions fetched by the provider, the contiguous run of stored candles ends at the same exchange-local minute on at least
``MIN_MATCH_SHARE`` of them, and the candles up to that minute are gapless. Random fetch failures do not end at one identical minute
session after session. Nothing is ever filled; the window is only removed from the denominator used for the refusal threshold and is
disclosed beside it.
"""
from __future__ import annotations

from collections import Counter
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar
from app.models.provider_coverage import ProviderUnservedWindow
from app.services.timeframes import UnsupportedMarket, profile_for
from app.services.trading_calendar import TRADING, classify_day

MIN_SESSIONS = 5
MIN_MATCH_SHARE = 0.9
LOOKBACK_DAYS = 45
MIN_CONTIGUOUS_SHARE = 0.995


def _fmt(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def unserved_label(start_minute: int, end_minute: int, provider: str) -> str:
    return f"{_fmt(start_minute)}-{_fmt(end_minute - 1)} not served by {provider}"


def detect_unserved_tail(db: Session, instrument: Instrument, provider: str, now: datetime, *, interval: str = "1m") -> ProviderUnservedWindow | None:
    """Record (idempotently) the trailing session window ``provider`` does not serve for ``instrument``, or return None when the evidence
    does not support one. An existing active record is re-confirmed or revoked by the same rule."""
    try:
        prof = profile_for(instrument)
    except UnsupportedMarket:
        return None
    if prof.continuous or prof.session_open is None or prof.session_close is None:
        return None
    open_m = prof.session_open.hour * 60 + prof.session_open.minute
    close_m = prof.session_close.hour * 60 + prof.session_close.minute
    tz = ZoneInfo(prof.tz)
    today = now.astimezone(tz).date()
    start = now - timedelta(days=LOOKBACK_DAYS)
    rows = db.execute(select(MarketBar.timestamp).where(
        MarketBar.instrument_id == instrument.id, MarketBar.interval == interval, MarketBar.source == provider,
        MarketBar.is_synthetic.is_(False), MarketBar.timestamp >= start, MarketBar.timestamp < now)).scalars().all()
    minutes_by_day: dict[date_type, set[int]] = {}
    for ts in rows:
        local = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(tz)
        minute = local.hour * 60 + local.minute
        if open_m <= minute < close_m:
            minutes_by_day.setdefault(local.date(), set()).add(minute)
    last_minutes: list[int] = []
    for day, minutes in minutes_by_day.items():
        if day >= today or classify_day(day, prof.key) != TRADING or min(minutes) != open_m:
            continue
        # The session must have been fetched to its served end: gapless from the open to its last candle.
        last = max(minutes)
        contiguous = sum(1 for m in minutes if m <= last) / (last - open_m + 1)
        if contiguous >= MIN_CONTIGUOUS_SHARE and len(minutes) >= 0.5 * (close_m - open_m):
            last_minutes.append(last)
    existing = db.scalar(select(ProviderUnservedWindow).where(
        ProviderUnservedWindow.instrument_id == instrument.id, ProviderUnservedWindow.provider == provider,
        ProviderUnservedWindow.interval == interval, ProviderUnservedWindow.status == "active"))
    verdict: tuple[int, int, int] | None = None
    if len(last_minutes) >= MIN_SESSIONS:
        common, count = Counter(last_minutes).most_common(1)[0]
        if common < close_m - 1 and count / len(last_minutes) >= MIN_MATCH_SHARE:
            verdict = (common + 1, count, len(last_minutes))
    if verdict is None:
        if existing is not None and len(last_minutes) >= MIN_SESSIONS:
            existing.status = "revoked"          # the provider now serves the tail: stop excluding it
            existing.last_confirmed_at = now
            db.commit()
        return None if existing is None or existing.status == "revoked" else existing
    start_minute, matching, examined = verdict
    evidence: dict[str, Any] = {"rule": f">= {MIN_SESSIONS} complete sessions, >= {int(MIN_MATCH_SHARE * 100)}% ending at the same minute, gapless to it",
                                "last_served_minute": _fmt(start_minute - 1), "lookback_days": LOOKBACK_DAYS}
    if existing is not None and (existing.start_minute, existing.end_minute) == (start_minute, close_m):
        existing.sessions_examined, existing.sessions_matching, existing.last_confirmed_at, existing.evidence = examined, matching, now, evidence
        db.commit()
        return existing
    if existing is not None:
        existing.status = "revoked"
    row = ProviderUnservedWindow(instrument_id=instrument.id, provider=provider, interval=interval, start_minute=start_minute, end_minute=close_m,
                                 sessions_examined=examined, sessions_matching=matching, evidence=evidence, detected_at=now, last_confirmed_at=now)
    db.add(row)
    db.commit()
    return row


def active_windows(db: Session, instrument_id: UUID, provider: str = "dhan", interval: str = "1m") -> list[ProviderUnservedWindow]:
    return list(db.scalars(select(ProviderUnservedWindow).where(
        ProviderUnservedWindow.instrument_id == instrument_id, ProviderUnservedWindow.provider == provider,
        ProviderUnservedWindow.interval == interval, ProviderUnservedWindow.status == "active")))


def unserved_ranges(db: Session, instrument_id: UUID, provider: str = "dhan") -> list[tuple[int, int]]:
    return [(w.start_minute, w.end_minute) for w in active_windows(db, instrument_id, provider)]


def describe(db: Session, instrument_id: UUID, provider: str = "dhan") -> dict[str, Any] | None:
    windows = active_windows(db, instrument_id, provider)
    if not windows:
        return None
    w = windows[0]
    return {"provider": provider, "start": _fmt(w.start_minute), "end": _fmt(w.end_minute - 1), "minutes_per_session": w.end_minute - w.start_minute,
            "last_served": w.evidence.get("last_served_minute"), "reason": w.reason_code,
            "sessions_matching": w.sessions_matching, "sessions_examined": w.sessions_examined, "detected_at": w.detected_at.isoformat(),
            "label": unserved_label(w.start_minute, w.end_minute, provider)}
