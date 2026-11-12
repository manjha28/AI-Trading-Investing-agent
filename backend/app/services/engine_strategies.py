"""Library strategies backed by the standalone ``scalp_hold`` engine (VWAPX-TB).

They are stored as ordinary Python-path strategies (so they appear in the Strategy Library with editable parameters and versioning), but their
backtests run through the engine's own simulator (triple-barrier geometry, venue cost functions, walk-forward evaluation), not the rule evaluator.
They carry no live signal logic: scanning skips them, and they are created suspended.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar
from app.models.strategies import Strategy, StrategyVersion
from app.services import intraday_python as ipy
from app.services.intraday_strategy import PythonSpec, Source, StrategyDefinition, definition_hash, validate_definition
from scalp_hold.app_adapter import DEFAULT_PARAMS

ENGINE_ID = "scalp_hold"
FILENAME = "scalp_hold.py"

SOURCE_TEXT = '''"""scalp_hold (VWAPX-TB): VWAP/volume-profile intraday strategy evaluated by the scalp_hold engine.

This file declares the strategy and its parameters. Its backtests run through the engine's own simulator (backend/scalp_hold): causal VWAP features,
triple-barrier stop/target geometry, Indian and crypto venue cost functions, anchored walk-forward folds and null controls. There is no live signal
logic here: the strategy is research-only and is never scanned.

Signals (booleans, no composite score): T trend pullback, R band reversion, B value-area breakout.
"""


class Strategy:
    NAME = "scalp_hold"
    DESCRIPTION = "VWAPX-TB: VWAP bands + fixed-grid volume profile, triple-barrier exits, venue-accurate costs. Backtest-only."
    DECISION_TIMEFRAME = "5m"
    INDICATORS = []
    ALLOW_SHORT = True
    PARAMS = %PARAMS%

    def __init__(self, params):
        self.params = params

    def on_bar(self, ctx):
        return None
'''


def source_text() -> str:
    return SOURCE_TEXT.replace("%PARAMS%", repr(DEFAULT_PARAMS))


def is_engine(defn: StrategyDefinition) -> bool:
    return defn.python is not None and (defn.source.provenance or {}).get("engine") == ENGINE_ID


def build_definition(universe: list[str], market: str = "equity_in") -> tuple[StrategyDefinition, str]:
    text = source_text()
    sha = hashlib.sha256(text.encode()).hexdigest()
    defn = StrategyDefinition(
        name=ENGINE_ID, description="VWAPX-TB: VWAP bands + fixed-grid volume profile, triple-barrier exits, venue-accurate costs. Backtest-only.", market=market,  # type: ignore[arg-type]
        universe=universe, decision_timeframe="5m", python=PythonSpec(source_sha256=sha, entrypoint="Strategy"), constraints={"allow_long": True, "allow_short": True},
        source=Source(type="python", artifact={"filename": FILENAME, "sha256": sha, "size": len(text.encode())},
                      provenance={"engine": ENGINE_ID, "params": DEFAULT_PARAMS, "isolation": "backtests run in the scalp_hold engine; no live signal logic"}))
    return defn, text


def ensure_scalp_hold(db: Session, user_id: Any) -> Strategy:
    """Create the library entry for this owner if it does not exist (idempotent). Anchor instrument: the stored equity series with the most 1-minute bars."""
    existing = db.scalar(select(Strategy).where(Strategy.user_id == user_id, Strategy.name == ENGINE_ID))
    if existing is not None:
        return existing
    best = db.execute(select(MarketBar.instrument_id, func.count(MarketBar.id)).where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False))
                      .group_by(MarketBar.instrument_id).order_by(func.count(MarketBar.id).desc()).limit(50)).all()
    anchor = None
    for instrument_id, _n in best:
        inst = db.get(Instrument, instrument_id)
        if inst is not None and inst.country == "IN" and inst.asset_class == "equity":
            anchor = inst
            break
    if anchor is None:
        raise ValueError("no stored equity instrument with 1-minute data to anchor the strategy to")
    defn, text = build_definition([str(anchor.id)])
    issues, _req = validate_definition(defn)
    if any(i["level"] == "error" for i in issues):
        raise ValueError("; ".join(i["message"] for i in issues if i["level"] == "error"))
    if ipy.screen_source(text):
        raise ValueError("engine source failed the Python screen")
    now = datetime.now(timezone.utc)
    s = Strategy(user_id=user_id, instrument_id=anchor.id, name=ENGINE_ID, description=defn.description, status="suspended", current_version=1, created_at=now, updated_at=now)
    db.add(s)
    db.flush()
    dumped = defn.model_dump(mode="json")
    db.add(StrategyVersion(strategy_id=s.id, version=1, params=(defn.source.provenance or {}).get("params", {}), definition=dumped, definition_hash=definition_hash(dumped),
                           source_artifact={**(defn.source.artifact or {}), "text": text}, source_type="python", created_at=now))
    db.commit()
    return s
