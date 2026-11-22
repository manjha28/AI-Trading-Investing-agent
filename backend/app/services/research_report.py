"""Daily research report: which stocks to consider buying (with levels), which to hold, and which to avoid for new entry.

Pure classification over the stored research briefs (already computed in Python); the model adds nothing. Horizons are swing, positional and
long-term only. Intraday is a separate system. Missing or stale research is reported as such, never promoted to a recommendation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Instrument, Portfolio
from app.research_daily import user_universe
from app.services import research_alerts, research_confidence, research_pipeline
from app.services.research_levels import PARAMS

HORIZONS = tuple(PARAMS)            # swing, positional, long_term
STALE_AFTER = timedelta(hours=40)   # a nightly run missed by more than a day is flagged, not trusted silently
BUCKETS = ("buy", "radar", "hold", "no_new_entry", "not_enough_data")
BUCKETS_V2 = ("buy", "setup_waiting", "radar", "hold", "no_new_entry", "not_enough_data")      # Nexus v2: "Ready to buy" holds only in-band / enter-now cards


def held_details(db: Session, user_id: UUID) -> dict[str, dict[str, Any]]:
    """Held stocks from BOTH the manual ledger and the latest synced Dhan holdings, with the source of each quantity. A symbol in both sources is counted once (the larger quantity), not added:
    a manual ledger line and the Dhan mirror usually describe the same shares."""
    from app.api.portfolio_transactions import calculate_portfolio_ledger
    from app.models import BrokerMirror
    ledger: dict[str, Decimal] = {}
    for portfolio_id in db.scalars(select(Portfolio.id).where(Portfolio.user_id == user_id)):
        for symbol, position in calculate_portfolio_ledger(db, user_id, portfolio_id).positions.items():
            if position.quantity > 0:
                ledger[symbol.upper()] = ledger.get(symbol.upper(), Decimal(0)) + position.quantity
    dhan: dict[str, Decimal] = {}
    for m in db.scalars(select(BrokerMirror).where(BrokerMirror.user_id == user_id, BrokerMirror.broker == "dhan", BrokerMirror.kind == "holding")):
        if m.quantity and m.quantity > 0:
            dhan[m.symbol.strip().upper()] = dhan.get(m.symbol.strip().upper(), Decimal(0)) + m.quantity
    out: dict[str, dict[str, Any]] = {}
    for sym in set(ledger) | set(dhan):
        src = {k: v for k, v in (("ledger", ledger.get(sym)), ("dhan", dhan.get(sym))) if v is not None}
        out[sym] = {"quantity": max(src.values()), "sources": {k: str(v) for k, v in src.items()},
                    "text": "Held at Dhan: " + f"{dhan[sym].normalize():f}" if sym in dhan and sym not in ledger else "Held in your ledger: " + f"{ledger[sym].normalize():f}" if sym not in dhan else
                            f"Held at Dhan: {dhan[sym].normalize():f} (also in your ledger: {ledger[sym].normalize():f})"}
    return out


def held_quantities(db: Session, user_id: UUID) -> dict[str, Decimal]:
    return {sym: d["quantity"] for sym, d in held_details(db, user_id).items()}


def _insufficient_reasons(sections: dict[str, Any], horizons: dict[str, Any]) -> list[str]:
    """The reasons every horizon is INSUFFICIENT_EVIDENCE, from the assessment's own blockers and the data notes (deduplicated, stored text only)."""
    out: list[str] = []
    tech = sections.get("technicals") or {}
    if tech.get("status") not in (None, "available") and tech.get("message"):
        out.append(str(tech["message"]))
    for h in HORIZONS:
        for b in ((horizons.get(h) or {}).get("blockers") or []):
            out.append(str(b))
        for g in ((horizons.get(h) or {}).get("gates") or []):
            if g.get("detail"):
                out.append(str(g["detail"]))
    comps = ((sections.get("assessment") or {}).get("components") or {})
    for name, comp in comps.items():
        for b in (comp.get("blockers") or []):
            out.append(f"{name.replace('_', ' ')}: {b}")
    fund = sections.get("fundamentals") or {}
    if not any(p.get("period_type") == "annual" for p in (fund.get("periods") or [])):
        out.append("no annual statements stored")
    seen: list[str] = []
    for r in out:
        if r and r not in seen:
            seen.append(r)
    return seen[:6]


def is_radar(sections: dict[str, Any]) -> bool:
    """A BUY on some horizon but no entry range could be determined for any BUY horizon (history too short, no usable structure, data gap)."""
    buys = research_alerts.buy_horizons(sections)
    if not buys:
        return False
    level_sets = (sections.get("levels") or {}).get("horizons") or {}
    return not any((level_sets.get(h) or {}).get("status") == "available" for h in buys)


def v2_bucket(bucket: str, recs: dict[str, str | None], level_sets: dict[str, Any]) -> str:
    """Nexus v2: "Ready to buy" holds only cards with a BUY horizon that is in its buy band or ENTER_NOW; the others with levels are "Set up, waiting"."""
    if bucket == "buy" and not any((level_sets.get(h) or {}).get("ready_to_buy") for h in HORIZONS if recs.get(h) == "BUY"):
        return "setup_waiting"
    return bucket


def classify(recs: dict[str, str | None], held: bool, fresh: bool, has_range: bool = True) -> tuple[str, str]:
    """-> (bucket, plain-language action). ``recs`` maps horizon -> final recommendation."""
    values = [recs.get(h) for h in HORIZONS]
    if not fresh or all(v in (None, "INSUFFICIENT_EVIDENCE") for v in values):
        return "not_enough_data", ("Keep holding; research data is not current enough to say more." if held else "No call: research data is missing or not current.")
    if "BUY" in values and not has_range:
        return "radar", "On radar: a buy signal exists but no entry range can be determined yet. It is rechecked more often than other stocks."
    if "BUY" in values:
        return "buy", ("In your portfolio; a buy signal exists, so adding is possible at the levels below." if held else "Can be bought at the levels below.")
    if held:
        if all(v in ("AVOID", None, "INSUFFICIENT_EVIDENCE") for v in values):
            return "hold", "In your portfolio: keep holding, but make no new purchase."
        return "hold", "In your portfolio: keep holding."
    return "no_new_entry", "Do not make a new entry now."


def _buy_conf(sections: dict[str, Any], horizon: str) -> dict[str, Any] | None:
    buys = research_alerts.buy_horizons(sections)
    if horizon not in buys:
        return None
    assessment = sections.get("assessment") or {}
    threshold = float((assessment.get("thresholds") or {}).get("buy", 65))
    return research_confidence.buy_confidence(assessment["horizons"][horizon], ((sections.get("levels") or {}).get("horizons") or {}).get(horizon), len(buys), threshold, PARAMS[horizon]["max_risk_atr"], sections.get("universe_rank"), horizon_key=horizon, factor=sections.get("factor_rank"))


def build_report(db: Session, user_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    held_info = held_details(db, user_id)
    held = {sym: d["quantity"] for sym, d in held_info.items()}
    rows: list[dict[str, Any]] = []
    for instrument in user_universe(db, user_id):
        latest = research_pipeline.latest_brief(db, user_id, instrument.id)
        row: dict[str, Any] = {"instrument_id": str(instrument.id), "symbol": instrument.symbol, "name": instrument.display_name,
                               "held": instrument.symbol.upper() in held, "quantity": str(held[instrument.symbol.upper()]) if instrument.symbol.upper() in held else None,
                               "held_text": (held_info.get(instrument.symbol.upper()) or {}).get("text"), "held_sources": (held_info.get(instrument.symbol.upper()) or {}).get("sources")}
        if latest is None:
            row.update(bucket="not_enough_data", action="Research for this stock is starting now; it will appear here automatically.", researched_at=None, stale=True, horizons={}, levels={}, researching=True)
            rows.append(row)
            continue
        run, brief = latest
        sections = brief.sections or {}
        finished = run.finished_at or run.started_at
        finished = finished if finished.tzinfo else finished.replace(tzinfo=timezone.utc)
        stale = now - finished > STALE_AFTER or (sections.get("technicals") or {}).get("status") != "available"
        horizons = ((sections.get("assessment") or {}).get("horizons") or {})
        recs = {h: (horizons.get(h) or {}).get("recommendation") for h in HORIZONS}
        bucket, action = classify(recs, row["held"], not stale, not is_radar(sections))
        level_sets = ((sections.get("levels") or {}).get("horizons") or {})
        if settings.levels_v2_active and v2_bucket(bucket, recs, level_sets) == "setup_waiting":
            bucket, action = "setup_waiting", "Set up, waiting: a buy signal and levels exist, but price is not in the buy band yet (see each card's entry label)."
        if settings.confidence_v2_active and bucket in ("buy", "setup_waiting"):
            graded = [h for h in HORIZONS if recs[h] == "BUY" and (_buy_conf(sections, h) or {}).get("grade")]
            if not graded:
                bucket, action = "radar", "On radar: a buy signal exists, but no horizon passes the setup checks yet (Not ready); the failed checks are listed on the card."
        if bucket == "not_enough_data":       # two different situations used to share one text: research that is not current, and current research whose every horizon lacks evidence
            if stale:
                why = (sections.get("technicals") or {}).get("message") or ((sections.get("market_data_recovery") or {}).get("latest_candle") or {}).get("reason") or f"the last research run finished more than {int(STALE_AFTER.total_seconds() // 3600)} hours ago"
                row["not_enough_kind"], action = "not_current", f"Not current: {why}" + (" Keep holding; nothing newer to say." if row["held"] else "")
            else:
                blockers = _insufficient_reasons(sections, horizons)
                row["not_enough_kind"], row["insufficient_reasons"] = "insufficient_evidence", blockers
                action = "Insufficient evidence: " + ("; ".join(blockers[:3]) if blockers else "every horizon lacks enough stored data") + ("." if blockers else ".") + (" Keep holding." if row["held"] else "")
        held_buy = bool(row["held"] and bucket in ("buy", "setup_waiting"))
        if held_buy:      # already held (manual ledger or Dhan): not a new purchase candidate, so it is listed under Hold with the buy levels kept for adding
            bucket, action = "hold", f"Already held ({row.get('held_text') or 'in your portfolio'}): a buy signal exists, so adding is possible at the levels below. Listed under Hold, not Ready to buy."
        row.update(
            bucket=bucket, action=action, researched_at=finished.isoformat(), stale=stale,
            horizons={h: {"recommendation": recs[h], "score": (horizons.get(h) or {}).get("score"), "confidence": (horizons.get(h) or {}).get("confidence"),
                          "buy_confidence": _buy_conf(sections, h),
                          "why_not": ((horizons.get(h) or {}).get("blockers") or [g.get("detail") for g in (horizons.get(h) or {}).get("gates", [])])[:2]} for h in HORIZONS},
            levels={h: level_sets.get(h) for h in HORIZONS if recs[h] == "BUY"} if bucket in ("buy", "radar") or held_buy else {},
            radar_reasons=[f"{h.replace('_', '-').title()}: {(level_sets.get(h) or {}).get('reason') or 'no range'}" if (level_sets.get(h) or {}).get("status") != "available" else f"{h.replace('_', '-').title()}: Not ready ({', '.join(((_buy_conf(sections, h) or {}).get('setup') or {}).get('failed_vetoes') or ['setup checks'])})" for h in HORIZONS if recs[h] == "BUY"] if bucket == "radar" else None,
            last_close=(sections.get("technicals") or {}).get("latest_close"),
            confidence=research_alerts.headline_confidence(sections) if bucket == "buy" else None,
            data_note=(((sections.get("market_data_recovery") or {}).get("latest_candle") or {}).get("reason") if stale else None),
        )
        rows.append(row)
    researched = [r["researched_at"] for r in rows if r.get("researched_at")]
    coverage = {"universe": len(rows), "researched_current": sum(1 for r in rows if r.get("researched_at") and not r.get("stale")),
                "stale": sum(1 for r in rows if r.get("researched_at") and r.get("stale")),
                "not_current": sum(1 for r in rows if r.get("not_enough_kind") == "not_current"), "insufficient_evidence": sum(1 for r in rows if r.get("not_enough_kind") == "insufficient_evidence"), "never_researched": sum(1 for r in rows if not r.get("researched_at")),
                "last_research_at": max(researched) if researched else None,
                "universe_rule": "Your watchlists plus every stock with a recorded portfolio transaction (NSE equity).",
                "schedule": "Researched automatically after 16:30 IST on trading days, at app start-up if anything is older than 20 hours, and within minutes of a stock being added."}
    buckets_all = BUCKETS_V2 if settings.levels_v2_active else BUCKETS
    order = {b: i for i, b in enumerate(buckets_all)}
    rows.sort(key=lambda r: (order[r["bucket"]], -((r.get("confidence") or {}).get("score") or ((r.get("horizons") or {}).get("positional") or {}).get("score") or 0), r["symbol"]))
    return {"as_of": now.isoformat(), "buckets": {b: [r for r in rows if r["bucket"] == b] for b in buckets_all},
            "counts": {b: sum(r["bucket"] == b for r in rows) for b in buckets_all}, "coverage": coverage,
            "scope": "Swing, positional and long-term only. Intraday scanning is separate.",
            "disclaimer": "Decision support, not investment advice. Levels are computed from completed daily bars and are not orders."}
