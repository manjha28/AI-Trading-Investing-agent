"""Code-defined strategies: a typed spec that BUILDS an ordinary ``intraday-strategy/1`` definition.

A registry strategy is not a second engine. ``StrategySpec.definition()`` returns the same compiled condition tree the
Builder saves, so the scanner, the backtester and live monitoring consume it unchanged (one compiled tree, one indicator
library). Every number is a named parameter with bounds; every level is computed deterministically from market data.
Nothing here places or implies an order: a strategy only produces signals and hypothetical backtests.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.services.rule_dsl import LABEL, STARTER_VERSION

REGISTRY_VERSION = "1"


@dataclass(frozen=True)
class ParamSpec:
    name: str
    default: float | int
    low: float | int
    high: float | int
    description: str

    @property
    def is_int(self) -> bool:
        return isinstance(self.default, int) and not isinstance(self.default, bool)


@dataclass
class Rules:
    """What a builder returns: indicators and conditions (as rule_dsl dicts) plus risk/session settings."""

    indicators: list[dict[str, Any]]
    long_entry: dict[str, Any] | None
    short_entry: dict[str, Any] | None = None
    long_exit: dict[str, Any] | None = None
    short_exit: dict[str, Any] | None = None
    stop: dict[str, Any] = field(default_factory=lambda: {"mode": "atr", "value": 1.5, "atr_length": 14})
    target: dict[str, Any] = field(default_factory=lambda: {"mode": "rr", "value": 2.0, "atr_length": 14})
    trail: dict[str, Any] = field(default_factory=lambda: {"mode": "none", "value": 0, "atr_length": 14})
    # Intraday (MIS) equity: no new entries late in the session and a forced square-off before the close.
    session: dict[str, Any] = field(default_factory=lambda: {"no_entry_after": "14:30", "square_off": "15:15"})
    max_trades_per_day: int | None = None
    timeframe: str = "5m"


@dataclass(frozen=True)
class StrategySpec:
    key: str
    name: str
    family: str          # trend | momentum | mean_reversion | breakout | volatility | volume
    summary: str
    rationale: str       # why it should work: the edge being claimed, and when it should fail
    params: tuple[ParamSpec, ...]
    build: Callable[[dict[str, float | int]], Rules]
    evidence: dict[str, Any] | None = None   # recorded research result (hypothetical backtest); None = not tested
    version: str = "1"

    def resolve(self, overrides: dict[str, float | int] | None = None) -> dict[str, float | int]:
        values = {p.name: p.default for p in self.params}
        known = {p.name: p for p in self.params}
        for name, value in (overrides or {}).items():
            if name not in known:
                raise ValueError(f"{self.key}: unknown parameter {name!r}")
            spec = known[name]
            if not spec.low <= value <= spec.high:
                raise ValueError(f"{self.key}.{name} must be between {spec.low} and {spec.high}")
            values[name] = int(value) if spec.is_int else float(value)
        return values

    def definition(self, overrides: dict[str, float | int] | None = None) -> dict[str, Any]:
        rules = self.build(self.resolve(overrides))
        provenance = {
            "starter_key": f"reg_{self.key}", "starter_version": STARTER_VERSION, "label": LABEL, "family": self.family,
            "registry_version": REGISTRY_VERSION, "strategy_version": self.version, "rationale": self.rationale,
            "performance": "hypothetical backtest recorded" if self.evidence else "not tested", "evidence": self.evidence,
        }
        return {
            "schema_version": "intraday-strategy/1", "name": f"Registry: {self.name}", "description": self.summary[:500], "market": "equity_in",
            "universe": [], "base_timeframe": "1m", "decision_timeframe": rules.timeframe, "indicators": rules.indicators,
            "long_entry": rules.long_entry, "short_entry": rules.short_entry, "long_exit": rules.long_exit, "short_exit": rules.short_exit,
            "stop": rules.stop, "target": rules.target, "trail": rules.trail, "sizing": {"method": "percent_equity", "value": 10},
            "session": rules.session,
            "constraints": {"allow_long": rules.long_entry is not None, "allow_short": rules.short_entry is not None,
                            "max_trades_per_day": rules.max_trades_per_day},
            "python": None, "source": {"type": "starter", "artifact": None, "provenance": provenance},
        }

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "family": self.family, "summary": self.summary, "rationale": self.rationale,
                "version": self.version, "evidence": self.evidence,
                "params": [{"name": p.name, "default": p.default, "min": p.low, "max": p.high, "description": p.description} for p in self.params]}
