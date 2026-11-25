"""Performance metrics: thin wrapper over the shared statistical block. Results are HYPOTHETICAL and never combined with real P&L."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Callable

from app.services.trade_stats import net_gross_block


def summarize(trades: list[dict], initial_capital: Decimal, n_configs: int = 1) -> dict:
    b = net_gross_block(trades, initial_capital, n_configs)
    b["hypothetical"] = True
    b["initial_capital"] = str(initial_capital)
    b["costs_verified"] = all(t.get("costs_verified") for t in trades) if trades else None
    return b


def segment(trades: list[dict], key: Callable[[dict], str], initial_capital: Decimal) -> dict[str, dict]:
    g: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: summarize(v, initial_capital) for k, v in sorted(g.items())}
