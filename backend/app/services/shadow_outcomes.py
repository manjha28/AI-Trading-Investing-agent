"""Forward outcomes of the v2 shadow ladders, and the separate review of the PROVISIONAL near-52-week-high blue-sky ladders (owner decision 2026-11-22).

Every v2 BUY recorded in ``pipeline_v2_shadow`` is followed forward on later stored daily bars with the same rules as the replay: ENTER_NOW fills at the next open; a waiting order must fill at its
planned entry within 5 sessions; the stop is checked before T1 on the same bar (and on the fill bar only the stop is checked for a resting order); a filled trade that hits neither inside the horizon
window (swing 15 / positional 60 / long-term 60 sessions) is a time exit. Costs: the verified delivery schedule on a Rs 1,00,000 position (dates before 2024-10-01 use its earliest declared schedule).
Ladders that exist ONLY because of the blue-sky rule (``relaxation_dependent``) are reported as their own line in every report. If, after at least 60 days of shadow data and 30 resolved trades,
their mean net R's 95% interval includes zero, ``review`` says so (TELL THE OWNER). Hypothetical; never real P&L.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)
HOLD = {"swing": 15, "positional": 60, "long_term": 60}
FILL_SESSIONS = 5
MIN_SHADOW_DAYS = 60
MIN_TRADES = 30
POSITION_RUPEES = 100000
EARLIEST_SCHEDULE = date(2024, 10, 1)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p, den = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (c - h, c + h)


def mean_ci(xs: list[float]) -> tuple[float, float, float]:
    n = len(xs)
    if n < 2:
        return (float("nan"), float("nan"), float("nan"))
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    se = sd / math.sqrt(n)
    return (m, m - 1.96 * se, m + 1.96 * se)


def _cost_fraction(entry: float, exit_px: float, d_in: date, d_out: date) -> float:
    from app.services import indian_charges as ic
    qty = max(1, int(POSITION_RUPEES // entry))
    clamp = lambda d: max(d, EARLIEST_SCHEDULE)  # noqa: E731
    rt = ic.delivery_round_trip(buy_price=Decimal(str(round(entry, 2))), sell_price=Decimal(str(round(exit_px, 2))), quantity=Decimal(qty), buy_date=clamp(d_in), sell_date=clamp(d_out))
    return float(rt["total"]) / (qty * entry)


def simulate(levels: dict[str, Any], bars: list[tuple[date, float, float, float, float]], horizon: str) -> dict[str, Any]:
    """``bars`` = completed daily bars AFTER the decision candle: (date, open, high, low, close). Returns the outcome, 'pending' while the window is still open."""
    planned, stop, t1 = float(levels["planned_entry"]), float(levels["stop"]), float(levels["t1"])
    now_fill = levels.get("entry_status") == "ENTER_NOW"
    filled, entry, k_in = False, None, None
    for k, (d, o, h, low, c) in enumerate(bars):
        if not filled:
            if now_fill and k == 0:
                entry, filled, k_in = o, True, 0
            elif not now_fill and k < FILL_SESSIONS and low <= planned:
                entry, filled, k_in = min(planned, o), True, k
            elif k >= (1 if now_fill else FILL_SESSIONS):
                return {"outcome": "unfilled"}
            if not filled:
                continue
        if low <= stop:
            return _done("stop", entry, stop, bars[k_in][0], d, planned, stop)
        if h >= t1 and not (k == k_in and not now_fill):
            return _done("t1", entry, t1, bars[k_in][0], d, planned, stop)
        if k >= HOLD[horizon]:
            return _done("time", entry, c, bars[k_in][0], d, planned, stop)
    if not filled:
        return {"outcome": "pending" if len(bars) < (1 if now_fill else FILL_SESSIONS) else "unfilled"}
    return {"outcome": "pending"}


def _done(kind: str, entry: float, exit_px: float, d_in: date, d_out: date, planned: float, stop: float) -> dict[str, Any]:
    risk = entry - stop
    cf = _cost_fraction(entry, exit_px, d_in, d_out)
    return {"outcome": kind, "entry": entry, "exit": exit_px, "entry_date": str(d_in), "exit_date": str(d_out), "net_r": ((exit_px - entry) / risk - cf * entry / risk) if risk > 0 else None,
            "ret_net": exit_px / entry - 1 - cf}


def summarise(trades: list[dict[str, Any]]) -> dict[str, Any]:
    done = [t for t in trades if t["outcome"] in ("t1", "stop", "time") and t.get("net_r") is not None]
    n, k = len(done), sum(1 for t in done if t["outcome"] == "t1")
    lo, hi = wilson(k, n)
    m, mlo, mhi = mean_ci([t["net_r"] for t in done])
    return {"ladders": len(trades), "resolved_trades": n, "pending": sum(1 for t in trades if t["outcome"] == "pending"), "unfilled": sum(1 for t in trades if t["outcome"] == "unfilled"),
            "t1_before_stop": None if n == 0 else k / n, "t1_ci95": None if n == 0 else [lo, hi], "mean_net_r": None if n < 2 else m, "net_r_ci95": None if n < 2 else [mlo, mhi]}


def review_message(stats: dict[str, Any], shadow_days: int) -> dict[str, Any]:
    """The owner's rule: after 2 months of shadow data, if the blue-sky ladders' net R interval includes zero, say so."""
    n = stats["resolved_trades"]
    if shadow_days < MIN_SHADOW_DAYS:
        return {"due": False, "message": f"Review not due: {shadow_days} of {MIN_SHADOW_DAYS} shadow days so far ({n} resolved blue-sky trades)."}
    if n < MIN_TRADES:
        return {"due": True, "tell_owner": False, "message": f"{shadow_days} shadow days but only {n} resolved blue-sky trades (need {MIN_TRADES}); the confidence interval cannot be judged yet."}
    lo, hi = stats["net_r_ci95"]
    if lo <= 0:                                  # the interval includes zero (or lies below it)
        return {"due": True, "tell_owner": True, "message": f"TELL THE OWNER: after {shadow_days} shadow days the blue-sky ladders' mean net R is {stats['mean_net_r']:+.2f}R with a 95% interval [{lo:+.2f}, {hi:+.2f}] that includes zero ({n} trades). The provisional rule is not yet supported."}
    return {"due": True, "tell_owner": False, "message": f"After {shadow_days} shadow days the blue-sky ladders' mean net R is {stats['mean_net_r']:+.2f}R, 95% interval [{lo:+.2f}, {hi:+.2f}] (above zero, {n} trades)."}


def review(db: Any, user_id: Any, now: datetime | None = None) -> dict[str, Any]:
    """Outcomes of every recorded v2 BUY ladder, split into 'blue-sky only (provisional)' and 'all other v2 buys', plus the 2-month review message."""
    from sqlalchemy import select

    from app.models import MarketBar
    from app.models.pipeline_v2 import PipelineV2Shadow
    now = now or datetime.now(timezone.utc)
    rows = list(db.scalars(select(PipelineV2Shadow).where(PipelineV2Shadow.user_id == user_id).order_by(PipelineV2Shadow.computed_at)))
    if not rows:
        return {"shadow_rows": 0, "shadow_days": 0, "review": review_message(summarise([]), 0)}
    seen, groups = set(), {"blue_sky_provisional": [], "other_v2_buys": []}
    bar_cache: dict[Any, list[tuple[date, float, float, float, float]]] = {}
    for rec in rows:
        for h, d in (rec.payload.get("horizons") or {}).items():
            lv = d.get("levels")
            if d.get("v2_recommendation") != "BUY" or not lv or rec.as_of_candle is None:
                continue
            key = (rec.instrument_id, h, rec.as_of_candle)
            if key in seen:
                continue
            seen.add(key)
            if rec.instrument_id not in bar_cache:
                bar_cache[rec.instrument_id] = [(b.timestamp.date(), float(b.open), float(b.high), float(b.low), float(b.close)) for b in db.scalars(
                    select(MarketBar).where(MarketBar.instrument_id == rec.instrument_id, MarketBar.interval == "1d", MarketBar.is_synthetic.is_(False)).order_by(MarketBar.timestamp))]
            after = [b for b in bar_cache[rec.instrument_id] if b[0] > rec.as_of_candle]
            res = simulate(lv, after, h)
            res.update(symbol_instrument=str(rec.instrument_id), horizon=h, as_of=str(rec.as_of_candle))
            groups["blue_sky_provisional" if (d.get("blue_sky") or {}).get("relaxation_dependent") else "other_v2_buys"].append(res)
    days = (now - (rows[0].computed_at if rows[0].computed_at.tzinfo else rows[0].computed_at.replace(tzinfo=timezone.utc))).days
    stats = {g: summarise(t) for g, t in groups.items()}
    return {"shadow_rows": len(rows), "shadow_days": days, "groups": stats, "review": review_message(stats["blue_sky_provisional"], days),
            "label": "Hypothetical follow-up of recorded v2 ladders on stored candles; not real P&L, never an order."}


def log_review(db: Any, user_id: Any) -> None:
    """Called from the nightly cycle: one log line; a WARNING only when the owner must be told."""
    try:
        out = review(db, user_id)
        r = out["review"]
        (logger.warning if r.get("tell_owner") else logger.info)("blue_sky_review %s", r["message"])
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the nightly cycle
        logger.warning("blue_sky_review_failed error_class=%s", type(exc).__name__)
