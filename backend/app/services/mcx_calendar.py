"""MCX trading calendar and session state (display and feed gating; the feed's own packets stay the source of truth).

Hours: Mon-Fri 09:00 to 23:30 IST while US daylight time is on, 23:55 in the US winter (early Nov to mid Mar).
Holidays: MCX publishes its own list (different from NSE) and many holidays close only the morning session (09:00-17:00);
the evening session (17:00 to close) then still trades. The table below holds only the dates cross-checked against
broker pages for the remaining 2026 weeks; the official circular MCX/TRD/636/2025 is the reference and must be
used to extend it. A date not in the table is treated as a normal trading day, and the live "no ticks" check catches the rest.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN = time(9, 0)
EVENING_OPEN = time(17, 0)

FULL_CLOSURES: dict[date, str] = {date(2026, 12, 25): "Christmas"}
MORNING_CLOSURES: dict[date, str] = {date(2026, 11, 10): "Diwali-Balipratipada", date(2026, 11, 24): "Guru Nanak Jayanti"}


def close_time(now: datetime) -> time:
    us_winter = not now.astimezone(ZoneInfo("America/New_York")).dst()
    return time(23, 55) if us_winter else time(23, 30)


def session_state(now: datetime | None = None) -> dict:
    """{open: bool, reason: str|None, hours: 'HH:MM-HH:MM', next_open: iso|None}. Closed states always carry a reason."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local = now.astimezone(IST)
    close = close_time(now)
    hours = f"{OPEN:%H:%M}–{close:%H:%M} IST Mon–Fri"
    day = local.date()
    if local.weekday() >= 5:
        reason = "weekend"
    elif day in FULL_CLOSURES:
        reason = f"holiday: {FULL_CLOSURES[day]}"
    elif day in MORNING_CLOSURES and local.time() < EVENING_OPEN:
        reason = f"holiday: {MORNING_CLOSURES[day]} (morning session closed, evening session opens {EVENING_OPEN:%H:%M} IST)"
    elif local.time() < OPEN:
        reason = f"before the {OPEN:%H:%M} IST open"
    elif local.time() >= close:
        reason = f"after the {close:%H:%M} IST close"
    else:
        return {"open": True, "reason": None, "hours": hours, "next_open": None}
    return {"open": False, "reason": reason, "hours": hours, "next_open": _next_open(local).isoformat()}


def _next_open(local: datetime) -> datetime:
    probe = local
    for _ in range(14 * 24 * 60):
        probe = (probe + timedelta(minutes=1)).replace(second=0, microsecond=0)
        if session_state_open(probe):
            return probe
    return local


def session_state_open(local: datetime) -> bool:
    now = local.astimezone(timezone.utc)
    day = local.astimezone(IST).date()
    t = local.astimezone(IST).time()
    if local.astimezone(IST).weekday() >= 5 or day in FULL_CLOSURES:
        return False
    if day in MORNING_CLOSURES and t < EVENING_OPEN:
        return False
    return OPEN <= t < close_time(now)
