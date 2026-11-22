"""Nexus v2 Phase 3: tradability gates for EVERY horizon (flag ``pipeline_v2_enabled``; off = v1 untouched). They are about tradability, not quality, so they apply to swing, positional and long-term alike.

1. Liquidity: 20-session median traded value from stored completed daily bars (close x volume, in rupees crore; the stored bars carry no turnover figure, so this is an estimate that tracks NSE's turnover
   closely). Floors: swing Rs 10 crore, positional Rs 5, long-term Rs 2. Fewer than 15 usable sessions = unknown, which blocks like a failure (missing data is never favourable).
2. Corporate events: no ENTER_NOW from 3 trading sessions before up to the day of a results meeting, a record date or an ex-date. Sources: stored NSE filings (board-meeting intimations that name a
   financial-results agenda, record-date and revised-record-date notices) and the corporate-action table (ex-dates). The event calendar is only as complete as the stored filings: absence of an event
   is shown as "no scheduled event found in stored filings (coverage partial)", never as "clear".
   A STALE calendar (the NSE filings feed has not refreshed for 3 days) additionally blocks ENTER_NOW for SWING only (owner decision; positional and long-term get the warning).
3. Stale data: the existing daily-bar staleness rule stays; additionally a daily bar that is still forming (today's session before the exchange close plus the publication grace) is dropped before any
   level is computed, because the radar pass runs 09:00-20:00 IST and a live bar must never be treated as completed.
Thresholds live in GateConfig (starting hypotheses). Decision support, not investment advice.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
CRORE = 1e7
VERSION = "gates-v2/1"
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(r"(\d{1,2})[-\s]([A-Za-z]{3})[a-z]*[-,\s]+(\d{4})")


@dataclass
class GateConfig:
    floor_crore: dict[str, float] = field(default_factory=lambda: {"swing": 10.0, "positional": 5.0, "long_term": 2.0})
    window_sessions: int = 20
    min_sessions: int = 15
    event_sessions_before: int = 3
    close_time: time = time(15, 30)
    publication_grace: timedelta = timedelta(hours=2)
    lookback_days_for_filings: int = 90


def median_traded_value_crore(daily: pd.DataFrame, cfg: GateConfig | None = None) -> tuple[float | None, int]:
    cfg = cfg or GateConfig()
    tail = daily.tail(cfg.window_sessions)
    tv = (tail["close"] * tail["volume"]).replace([np.inf, -np.inf], np.nan).dropna()
    tv = tv[tv > 0]
    return (None, len(tv)) if len(tv) < cfg.min_sessions else (float(tv.median() / CRORE), len(tv))


def liquidity_gate(daily: pd.DataFrame, horizon: str, cfg: GateConfig | None = None) -> dict[str, Any]:
    cfg = cfg or GateConfig()
    med, n = median_traded_value_crore(daily, cfg)
    floor = cfg.floor_crore[horizon]
    if med is None:
        return {"status": "unknown", "pass": False, "median_traded_value_crore": None, "floor_crore": floor, "sessions_used": n,
                "reason": f"Only {n} sessions with a usable price and volume are stored (need {cfg.min_sessions} of the last {cfg.window_sessions}); liquidity cannot be confirmed."}
    ok = med >= floor
    return {"status": "pass" if ok else "fail", "pass": ok, "median_traded_value_crore": round(med, 2), "floor_crore": floor, "sessions_used": n,
            "reason": f"20-session median traded value Rs {med:,.1f} crore {'meets' if ok else 'is below'} the {horizon.replace('_', '-')} floor of Rs {floor:g} crore (close x volume estimate)."}


def parse_event_dates(headline: str, category: str | None) -> list[tuple[str, date]]:
    """[(kind, date)] from an NSE filing headline. kind: results | record_date. Dates are as written in the filing; anything unparseable yields nothing."""
    text = headline or ""
    cat = (category or "").lower()
    found = [(int(d), MONTHS.get(m.lower()), int(y)) for d, m, y in DATE_RE.findall(text) if MONTHS.get(m.lower())]
    dates = []
    for d, m, y in found:
        try:
            dates.append(date(y, m, d))
        except ValueError:
            continue
    if not dates:
        return []
    low = text.lower()
    if "board meeting" in cat and "outcome" not in cat and "financial result" in low:
        m = re.search(r"held on (\d{1,2}[-\s][A-Za-z]{3}[a-z]*[-,\s]+\d{4})", text)
        d = _one(m.group(1)) if m else dates[0]
        return [("results", d)] if d else []
    if "record date" in cat or "record date" in low:
        revised = re.search(r"revised to (\d{1,2}[-\s][A-Za-z]{3}[a-z]*[-,\s]+\d{4})", text, re.I)
        d = _one(revised.group(1)) if revised else dates[-1]
        return [("record_date", d)] if d else []
    return []


def _one(s: str) -> date | None:
    m = DATE_RE.search(s)
    if not m or not MONTHS.get(m.group(2).lower()):
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:
        return None


def sessions_between(start: date, end: date) -> int:
    """Trading sessions strictly after ``start`` up to and including ``end`` (0 if end <= start)."""
    from app.services.trading_calendar import TRADING, classify_day
    n, d = 0, start
    while d < end:
        d += timedelta(days=1)
        if classify_day(d, "equity_in") == TRADING:
            n += 1
    return n


def upcoming_events(db: Any, instrument_id: Any, user_id: Any, today: date, cfg: GateConfig | None = None) -> list[dict[str, Any]]:
    """Events inside the blocking window: from today through ``event_sessions_before`` trading sessions ahead."""
    from sqlalchemy import select
    from app.models import CorporateAction, NewsItem
    cfg = cfg or GateConfig()
    out: list[dict[str, Any]] = []
    since = datetime.combine(today - timedelta(days=cfg.lookback_days_for_filings), time.min, tzinfo=timezone.utc)
    for it in db.scalars(select(NewsItem).where(NewsItem.instrument_id == instrument_id, NewsItem.source == "nse", NewsItem.published_at >= since)):
        for kind, d in parse_event_dates(it.headline, it.category):
            if d >= today and sessions_between(today, d) <= cfg.event_sessions_before:
                out.append({"kind": kind, "date": d.isoformat(), "sessions_ahead": sessions_between(today, d), "source": "nse_filing", "detail": (it.headline or "")[:160], "url": it.source_url})
    if user_id is not None:
        for a in db.scalars(select(CorporateAction).where(CorporateAction.user_id == user_id, CorporateAction.instrument_id == instrument_id, CorporateAction.ex_date >= today)):
            if sessions_between(today, a.ex_date) <= cfg.event_sessions_before:
                out.append({"kind": "ex_date", "date": a.ex_date.isoformat(), "sessions_ahead": sessions_between(today, a.ex_date), "source": f"corporate_action:{a.source}", "detail": a.action_type, "url": a.source_reference})
    uniq = {(e["kind"], e["date"]): e for e in out}
    return sorted(uniq.values(), key=lambda e: (e["date"], e["kind"]))


def filings_feed_status(db: Any, now: datetime, cfg: GateConfig | None = None, max_age_days: int = 3) -> dict[str, Any]:
    """Freshness of the NSE filings feed (news_source_polls). A feed that has not succeeded for max_age_days means the event calendar cannot be trusted; this is reported, not hidden."""
    from sqlalchemy import select
    from app.models import NewsSourcePoll
    poll = db.scalar(select(NewsSourcePoll).where(NewsSourcePoll.source == "nse"))
    last = poll.last_successful_at if poll is not None else None
    age = None if last is None else (now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc))).days
    return {"last_successful_at": None if last is None else last.date().isoformat(), "age_days": age, "stale": last is None or age > max_age_days, "last_error": getattr(poll, "last_error_category", None)}


def drop_forming_bar(daily: pd.DataFrame, now: datetime, cfg: GateConfig | None = None) -> pd.DataFrame:
    """Never treat a still-forming daily bar as completed: a bar dated today (IST) is kept only after the exchange close plus the publication grace."""
    cfg = cfg or GateConfig()
    if daily is None or len(daily) == 0:
        return daily
    local = now.astimezone(IST)
    last = daily.index[-1]
    last_date = last.date() if hasattr(last, "date") else last
    complete_at = datetime.combine(last_date, cfg.close_time, tzinfo=IST) + cfg.publication_grace
    return daily.iloc[:-1] if local < complete_at else daily


def compute(db: Any, instrument_id: Any, user_id: Any, daily: pd.DataFrame, now: datetime, cfg: GateConfig | None = None) -> dict[str, Any]:
    cfg = cfg or GateConfig()
    today = now.astimezone(IST).date()
    events = upcoming_events(db, instrument_id, user_id, today, cfg)
    feed = filings_feed_status(db, now, cfg)
    note = "Event dates come from stored NSE filings and corporate actions; coverage is partial, so no event found is not proof that none is scheduled."
    if feed["stale"]:
        note = f"EVENT CALENDAR UNRELIABLE: the NSE filings feed has not updated since {feed['last_successful_at'] or 'ever'} ({feed['age_days']} days). " + note
    return {"version": VERSION, "as_of": now.isoformat(), "liquidity": {h: liquidity_gate(daily, h, cfg) for h in cfg.floor_crore}, "events": events, "event_feed": feed,
            "event_calendar_note": note,
            "event_window_sessions": cfg.event_sessions_before}


def apply_to_levels(levels: dict[str, Any], gates: dict[str, Any] | None) -> dict[str, Any]:
    """Applies the gates to a research_levels.build_levels result (in place; returns it). Liquidity fail/unknown removes the ladder for that horizon; an event in the window turns ENTER_NOW into a wait."""
    if not gates:
        return levels
    for h, card in (levels.get("horizons") or {}).items():
        liq = (gates.get("liquidity") or {}).get(h)
        if card.get("status") != "available" or liq is None:
            continue
        if not liq["pass"]:
            levels["horizons"][h] = {"status": "gated", "reason": f"Liquidity gate ({liq['status']}): {liq['reason']}", "gate": "liquidity", "liquidity": liq}
            continue
        card["gates"] = {"liquidity": liq, "events": gates.get("events") or [], "event_calendar_note": gates.get("event_calendar_note"), "event_feed": gates.get("event_feed")}
        feed = gates.get("event_feed") or {}
        if h == "swing" and feed.get("stale") and card.get("entry_status") == "ENTER_NOW":      # owner decision: a results gap is the main overnight risk of a swing trade, so a stale calendar blocks it
            card["entry_status"], card["ready_to_buy"] = "WAIT_FOR_ENTRY", False
            card["event_gate"] = f"The NSE filings feed has not refreshed for {feed.get('age_days')} day(s) (limit 3), so a results date cannot be ruled out: no entry-now signal for a swing trade until it recovers."
            if card.get("entry_label"):
                card["entry_label"] = "Wait: event calendar stale (swing)"
        if gates.get("events"):
            e = gates["events"][0]
            note = f"{e['kind'].replace('_', ' ')} on {e['date']} ({e['sessions_ahead']} session(s) ahead): no entry-now signal until it has passed."
            if card.get("entry_status") == "ENTER_NOW":
                card["entry_status"], card["ready_to_buy"] = "WAIT_FOR_ENTRY", False
                if card.get("entry_label"):
                    card["entry_label"] = f"Wait: {note}"
            card["event_gate"] = note
    return levels
