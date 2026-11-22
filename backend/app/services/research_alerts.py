"""Bell notification when a stock newly becomes a BUY candidate (swing, positional or long-term). Never an order."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentResearchBrief, InstrumentResearchRun
from app.models.alerts import AlertEvent, AlertRule
from app.services import research_confidence
from app.services.research_levels import PARAMS

logger = logging.getLogger(__name__)
RULE_TYPE = "research_buy"      # a disabled, hidden carrier rule: the alert evaluator never runs it, the bell lists its events
IST = ZoneInfo("Asia/Kolkata")


def buy_horizons(sections: dict) -> list[str]:
    horizons = ((sections.get("assessment") or {}).get("horizons") or {})
    return [h for h in PARAMS if (horizons.get(h) or {}).get("recommendation") == "BUY"]


def confidence_by_horizon(sections: dict) -> dict:
    """Buy-conviction per BUY horizon (horizons that are not BUY are absent)."""
    assessment = sections.get("assessment") or {}
    horizons = assessment.get("horizons") or {}
    threshold = float((assessment.get("thresholds") or {}).get("buy", 65))
    buys = buy_horizons(sections)
    level_sets = (sections.get("levels") or {}).get("horizons") or {}
    out = {}
    for h in buys:
        c = research_confidence.buy_confidence(horizons[h], level_sets.get(h), len(buys), threshold, PARAMS[h]["max_risk_atr"], sections.get("universe_rank"), horizon_key=h, factor=sections.get("factor_rank"))
        if c:
            out[h] = c
    return out


def headline_confidence(sections: dict) -> dict | None:
    """Best buy-conviction across the BUY horizons, with its horizon."""
    assessment = sections.get("assessment") or {}
    horizons = assessment.get("horizons") or {}
    threshold = float((assessment.get("thresholds") or {}).get("buy", 65))
    buys = buy_horizons(sections)
    level_sets = (sections.get("levels") or {}).get("horizons") or {}
    best = None
    for h in buys:
        c = research_confidence.buy_confidence(horizons[h], level_sets.get(h), len(buys), threshold, PARAMS[h]["max_risk_atr"], sections.get("universe_rank"), horizon_key=h, factor=sections.get("factor_rank"))
        if c and (best is None or c["score"] > best["score"]):
            best = {**c, "horizon": h}
    return best


def notify_if_new_buy(db: Session, run: InstrumentResearchRun, brief: InstrumentResearchBrief) -> bool:
    """Create a bell event when this brief has a BUY horizon and the previous brief for the stock had none. Failure never affects the research run."""
    try:
        now_buys = buy_horizons(brief.sections or {})
        if not now_buys:
            return False
        previous = db.scalars(select(InstrumentResearchBrief).where(
            InstrumentResearchBrief.instrument_id == brief.instrument_id, InstrumentResearchBrief.id != brief.id,
            InstrumentResearchBrief.run_id.in_(select(InstrumentResearchRun.id).where(InstrumentResearchRun.user_id == run.user_id)),
        ).order_by(InstrumentResearchBrief.created_at.desc()).limit(1)).first()
        if previous is not None and buy_horizons(previous.sections or {}):
            return False
        instrument = db.get(Instrument, brief.instrument_id)
        rule = db.scalar(select(AlertRule).where(AlertRule.user_id == run.user_id, AlertRule.instrument_id == brief.instrument_id, AlertRule.rule_type == RULE_TYPE))
        now = datetime.now(timezone.utc)
        if rule is None:
            rule = AlertRule(id=uuid4(), user_id=run.user_id, scope="instrument", instrument_id=brief.instrument_id, rule_type=RULE_TYPE, params={},
                             severity="attention", cooldown_seconds=0, enabled=False, created_at=now, updated_at=now)
            db.add(rule)
            db.flush()
        conf = headline_confidence(brief.sections or {})
        evidence = {"symbol": instrument.symbol if instrument else None, "horizons": now_buys, "confidence": conf["score"] if conf else None,
                    "confidence_band": conf["band"] if conf else None, "message": "Ready for buying: levels are in the Research daily report."}
        db.add(AlertEvent(id=uuid4(), rule_id=rule.id, user_id=run.user_id, instrument_id=brief.instrument_id, fired_at=now,
                          dedupe_key=f"{RULE_TYPE}:{now.astimezone(IST).date().isoformat()}", evidence=evidence, status="new"))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()    # already notified today
        return False
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("research_buy_alert_failed error_class=%s", type(exc).__name__)
        return False




THESIS_RULE_TYPE = "thesis_break"      # a disabled, hidden carrier rule (like research_buy): its events appear in the bell


def notify_thesis_breaks(db: Session, user_id, now: datetime | None = None) -> list[dict]:
    """Stage 8: for the owner's Dhan long-term HOLDINGS, bell events when the multibagger engine sees the thesis break (revenue growth under 5% for two quarters, ROCE 5 points under its 3-year median, a pledge that
    appeared or rose, an auditor or regulatory event, PE above 2.5x EPS growth and 1.5x its own median (trim, not sell), or the class dropping to avoid). A price fall alone never alerts. One event per stock, kind and
    day. Runs only with ``multibagger_screen_enabled``; never raises; never an order."""
    from app.config import settings
    if not settings.multibagger_screen_enabled:
        return []
    from app.models import BrokerMirror
    from app.models.fundamental_snapshots import ShareholdingSnapshot
    from app.models.multibagger import MultibaggerAssessment
    from app.services import multibagger_engine, multibagger_loader
    now = now or datetime.now(timezone.utc)
    fired: list[dict] = []
    try:
        symbols = {m.symbol.strip().upper() for m in db.scalars(select(BrokerMirror).where(BrokerMirror.user_id == user_id, BrokerMirror.broker == "dhan", BrokerMirror.kind == "holding")) if m.quantity and m.quantity > 0}
        for inst in db.scalars(select(Instrument).where(Instrument.venue == "NSE", Instrument.symbol.in_(symbols))):
            rows = list(db.scalars(select(MultibaggerAssessment).where(MultibaggerAssessment.instrument_id == inst.id, MultibaggerAssessment.user_id == user_id).order_by(MultibaggerAssessment.as_of.desc()).limit(2)))
            if not rows:
                continue
            x = multibagger_loader.build_inputs(db, user_id, inst, now, None)
            snaps = list(db.scalars(select(ShareholdingSnapshot.pledged_pct_of_promoter).where(ShareholdingSnapshot.instrument_id == inst.id, ShareholdingSnapshot.pledged_pct_of_promoter.is_not(None)).order_by(ShareholdingSnapshot.captured_on.desc()).limit(2)))
            before = float(snaps[1]) if len(snaps) > 1 else None
            result = {"class": rows[0].klass, "scores": {"G": rows[0].g}}
            for a in multibagger_engine.thesis_alerts(x, result, previous_class=rows[1].klass if len(rows) > 1 else None, pledge_before=before):
                rule = db.scalar(select(AlertRule).where(AlertRule.user_id == user_id, AlertRule.instrument_id == inst.id, AlertRule.rule_type == THESIS_RULE_TYPE))
                if rule is None:
                    rule = AlertRule(id=uuid4(), user_id=user_id, scope="instrument", instrument_id=inst.id, rule_type=THESIS_RULE_TYPE, params={}, severity="attention", cooldown_seconds=0, enabled=False, created_at=now, updated_at=now)
                    db.add(rule)
                    db.flush()
                key = f"{THESIS_RULE_TYPE}:{a['kind']}:{now.astimezone(IST).date().isoformat()}"
                if db.scalar(select(AlertEvent.id).where(AlertEvent.rule_id == rule.id, AlertEvent.dedupe_key == key)) is not None:
                    continue
                db.add(AlertEvent(id=uuid4(), rule_id=rule.id, user_id=user_id, instrument_id=inst.id, fired_at=now, dedupe_key=key,
                                  evidence={"symbol": inst.symbol, "kind": a["kind"], "message": a["text"], "note": "Thesis check on a Dhan long-term holding. Decision support, not investment advice; never an order."}, status="new"))
                fired.append({"symbol": inst.symbol, **a})
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("thesis_break_alert_failed error_class=%s", type(exc).__name__)
        return []
    return fired
