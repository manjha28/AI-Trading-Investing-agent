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


