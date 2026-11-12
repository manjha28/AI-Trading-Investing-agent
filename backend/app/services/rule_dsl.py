"""Tiny helpers for writing strategy definitions as data (shared by the starter strategies)."""
from __future__ import annotations

from typing import Any

STARTER_VERSION = "1"
LABEL = "Built-in Starter Strategy"
TF = "5m"


def ind(alias: str, indicator: str, params: dict[str, Any] | None = None, tf: str = TF) -> dict[str, Any]:
    return {"alias": alias, "indicator": indicator, "params": params or {}, "timeframe": tf}


def I(ref: str, output: str | None = None, offset: int = 0) -> dict[str, Any]:  # noqa: E743
    return {"kind": "indicator", "ref": ref, "output": output, "offset": offset}


def P(field: str, tf: str = TF, offset: int = 0) -> dict[str, Any]:
    return {"kind": "price", "field": field, "timeframe": tf, "offset": offset}


def C(value: float) -> dict[str, Any]:
    return {"kind": "const", "value": value}


def A(op: str, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "arith", "op": op, "left": left, "right": right}


def cmp(left: dict[str, Any], op: str, right: dict[str, Any], within: int = 1) -> dict[str, Any]:
    return {"type": "compare", "left": left, "op": op, "right": right, "within": within}


def AND(*children: dict[str, Any]) -> dict[str, Any]:
    return {"type": "group", "op": "and", "children": list(children)}
