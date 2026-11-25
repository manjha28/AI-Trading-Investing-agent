"""Instrument universe: NIFTY, BANKNIFTY and top-N liquid stock futures by median traded value (causal, per session).

Spread filters need stored bid/ask, which does not exist: that criterion is reported UNAVAILABLE (not passed). The F&O ban
list and scheduled-results calendar are also not stored; they are never assumed clear."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .config import VarConfig
from .loader import UnderlyingData, median_daily_turnover, select_series_for_day

CRORE = Decimal(10_000_000)


def rank_stock_universe(candidates: list[UnderlyingData], day: date, prior_day: date | None, cfg: VarConfig) -> tuple[list[str], list[dict]]:
    u = cfg.universe
    rows, picked = [], []
    for d in candidates:
        cs = select_series_for_day(d, day, prior_day)
        if cs is None:
            rows.append({"underlying": d.underlying, "eligible": False, "reason": "no selectable contract for the session"})
            continue
        t = median_daily_turnover(cs, u.liquidity_lookback_sessions, day)
        if t is None:
            rows.append({"underlying": d.underlying, "eligible": False, "reason": f"fewer than {u.liquidity_lookback_sessions} prior sessions"})
            continue
        ok = t >= Decimal(u.min_median_daily_turnover_inr)
        rows.append({"underlying": d.underlying, "eligible": ok, "median_turnover_inr": str(t.quantize(Decimal(1))),
                     "median_turnover_crore": str((t / CRORE).quantize(Decimal("0.1"))),
                     "spread_filter": "unavailable (no stored bid/ask)", "ban_list": "unavailable (not stored)", "results_calendar": "unavailable (not stored)",
                     "reason": "" if ok else "below minimum median turnover"})
    elig = sorted((r for r in rows if r["eligible"]), key=lambda r: -Decimal(r["median_turnover_inr"]))
    picked = [r["underlying"] for r in elig[: u.max_stock_contracts]]
    for r in rows:
        r["selected"] = r["underlying"] in picked
    return picked, rows
