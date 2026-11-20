"""The strategy registry: one place where code-defined strategies are listed."""
from __future__ import annotations

from app.strategies.base import StrategySpec

REGISTRY: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> StrategySpec:
    if spec.key in REGISTRY:
        raise ValueError(f"duplicate strategy key {spec.key!r}")
    REGISTRY[spec.key] = spec
    return spec


def all_specs() -> list[StrategySpec]:
    return sorted(REGISTRY.values(), key=lambda s: (s.family, s.key))


def get(key: str) -> StrategySpec:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"unknown registry strategy {key!r}") from None
