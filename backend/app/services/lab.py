"""Backtest lab: symbol sets, batch aggregation and ranking. Everything here is a HYPOTHETICAL backtest summary.

Results are read back from stored backtest runs (which carry their data window, bar counts, source and cost profile);
nothing is computed from model output, and none of it ever mixes with real portfolio P&L.
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Instrument, MarketBar, WatchlistInstrument
from app.strategies.universes import NIFTY_50

MIN_BARS_1M = 20_000          # ~ 53 sessions of 375 one-minute bars
MAX_LAB_INSTRUMENTS = 50
MAX_LAB_STRATEGIES = 12
PRESETS = ("master", "watchlist", "nifty50", "has_1m")
MASTER_TOKEN = "@master"          # a universe entry meaning "every instrument on the user's Market watch master list"
MAX_MASTER_INSTRUMENTS = 400


def _counts(db: Session) -> dict[UUID, int]:
    return {r[0]: r[1] for r in db.execute(
        select(MarketBar.instrument_id, func.count()).where(MarketBar.interval == "1m", MarketBar.is_synthetic.is_(False)).group_by(MarketBar.instrument_id))}


def master_instruments(db: Session, user_id: UUID, *, min_bars: int = 0) -> list[Instrument]:
    """The caller's whole Market watch master list: the Dhan-mapped NSE universe, plus what they added, plus their watchlist.

    ``min_bars`` > 0 keeps only instruments with at least that many stored real 1-minute bars (a scanner needs history to evaluate indicators).
    """
    from app.api.market_data import _universe_instrument_ids
    from app.models.market_watch import MarketWatchExtra

    ids = set(_universe_instrument_ids(db))
    ids |= set(db.scalars(select(MarketWatchExtra.instrument_id).where(MarketWatchExtra.user_id == user_id)))
    ids |= set(db.scalars(select(WatchlistInstrument.instrument_id).where(WatchlistInstrument.user_id == user_id)))
    if not ids:
        return []
    found = [i for i in db.scalars(select(Instrument).where(Instrument.id.in_(list(ids)), Instrument.is_synthetic.is_(False))) if i.venue in ("NSE", "XNSE", "BSE")]
    if min_bars > 0:
        counts = _counts(db)
        found = [i for i in found if counts.get(i.id, 0) >= min_bars]
    return sorted(found, key=lambda i: i.symbol)[:MAX_MASTER_INSTRUMENTS]


def resolve_universe(db: Session, user_id: UUID, preset: str) -> dict[str, Any]:
    """Instruments of a preset that have enough stored 1-minute data, plus a transparent list of what was left out and why."""
    if preset not in PRESETS:
        raise ValueError(f"unknown universe {preset!r}")
    counts = _counts(db)
    equities = list(db.scalars(select(Instrument).where(
        Instrument.venue.in_(("NSE", "XNSE")), Instrument.asset_class.in_(("equity", "etf")), Instrument.is_synthetic.is_(False))))
    by_symbol: dict[str, list[Instrument]] = {}
    for inst in equities:
        by_symbol.setdefault(inst.symbol, []).append(inst)

    def richest(symbol: str) -> Instrument | None:   # one instrument per symbol: the one with the most stored 1-minute bars
        options = by_symbol.get(symbol, [])
        return max(options, key=lambda i: counts.get(i.id, 0)) if options else None

    included: list[Instrument] = []
    excluded: list[dict[str, str]] = []
    if preset == "master":
        # the whole list, with no history requirement: a forward test fills history as it runs
        listed = master_instruments(db, user_id)
        return {"preset": preset, "instruments": listed, "excluded": [], "bars": {str(i.id): counts.get(i.id, 0) for i in listed}}
    if preset == "nifty50":
        wanted = list(NIFTY_50)
    elif preset == "watchlist":
        ids = set(db.scalars(select(WatchlistInstrument.instrument_id).where(WatchlistInstrument.user_id == user_id)))
        wanted = sorted({i.symbol for i in equities if i.id in ids})
    else:
        wanted = sorted(symbol for symbol, options in by_symbol.items() if any(counts.get(i.id, 0) >= MIN_BARS_1M for i in options))
    for symbol in wanted:
        inst = richest(symbol)
        if inst is None:
            excluded.append({"symbol": symbol, "reason": "not in the instrument master"})
        elif counts.get(inst.id, 0) < MIN_BARS_1M:
            excluded.append({"symbol": symbol, "reason": f"only {counts.get(inst.id, 0)} stored 1-minute bars (need {MIN_BARS_1M})"})
        else:
            included.append(inst)
    included = included[:MAX_LAB_INSTRUMENTS]
    stored = sorted((counts.get(inst.id, 0), inst.symbol) for inst in (richest(sym) for sym in wanted) if inst is not None)
    coverage = {"needed_bars": MIN_BARS_1M, "needed_sessions": MIN_BARS_1M // 375, "symbols_checked": len(stored), "qualifying": len(included),
                "best_bars": stored[-1][0] if stored else 0, "best_symbol": stored[-1][1] if stored else None, "best_sessions": (stored[-1][0] // 375) if stored else 0,
                "median_bars": stored[len(stored) // 2][0] if stored else 0}
    return {"preset": preset, "instruments": included, "excluded": excluded, "coverage": coverage, "bars": {str(i.id): counts.get(i.id, 0) for i in included}}


def describe_presets(db: Session, user_id: UUID) -> list[dict[str, Any]]:
    labels = {"master": "Entire Market watch list (live)", "watchlist": "My watchlist", "nifty50": "Nifty 50 (constituents with stored 1-minute data)", "has_1m": "Everything with ≥ 1 year of 1-minute data"}
    out = []
    for preset in PRESETS:
        u = resolve_universe(db, user_id, preset)
        out.append({"key": preset, "label": labels[preset], "available": len(u["instruments"]), "excluded": u["excluded"],
                    "symbols": [i.symbol for i in u["instruments"]], "min_bars_1m": MIN_BARS_1M, "coverage": u.get("coverage")})
    return out


# ───────────────────────── aggregation ─────────────────────────
def _num(value: Any) -> Decimal | None:
    try:
        return None if value is None or value == "" else Decimal(str(value))
    except InvalidOperation:
        return None


def _row_metrics(m: dict[str, Any] | None) -> dict[str, Any]:
    m = m or {}
    keys = ("total_trades", "win_rate", "net_profit", "gross_pnl_before_costs", "total_charges", "total_slippage", "profit_factor_net",
            "expectancy_per_trade", "max_drawdown_pct", "sharpe_ratio", "return_pct", "avg_holding_minutes", "total_turnover")
    return {k: m.get(k) for k in keys}


def summarise_run(run: Any, strategy_name: str) -> dict[str, Any]:
    """One ranked-table row from a stored backtest run (single or in/out-of-sample)."""
    res = run.result or {}
    row: dict[str, Any] = {"run_id": str(run.id), "strategy_id": str(run.strategy_id), "strategy": strategy_name, "kind": run.kind,
                           "status": run.status, "error": run.error, "progress": run.progress}
    if run.status != "completed":
        return row
    if run.kind == "walk_forward":
        folds = res.get("folds") or []
        nets = [n for n in (_num((f.get("test_result") or {}).get("net_profit")) for f in folds) if n is not None]
        row["metrics"] = {"total_trades": res.get("oos_total_trades"), "net_profit": res.get("oos_net_profit")}
        row["walk_forward"] = {"folds": len(folds), "folds_profitable": sum(1 for n in nets if n > 0), "train_days": res.get("train_days"), "test_days": res.get("test_days"),
                               "parameters": res.get("parameters") or [], "optimised_on": res.get("optimised_on"),
                               "fold_results": [{"test": f.get("test"), "chosen_params": f.get("chosen_params"), "net_profit": (f.get("test_result") or {}).get("net_profit"),
                                                 "trades": (f.get("test_result") or {}).get("total_trades")} for f in folds[:60]]}
        row["by_instrument"] = None
    elif run.kind == "sensitivity":
        grid = res.get("grid") or []
        nets = sorted(n for n in (_num(g.get("net_profit")) for g in grid) if n is not None)
        median = nets[len(nets) // 2] if nets else None
        row["metrics"] = {"total_trades": sum(int(g.get("total_trades") or 0) for g in grid) // max(len(grid), 1), "net_profit": str(median) if median is not None else None}
        row["sweep"] = {"parameters": res.get("parameters") or [], "points": len(grid), "points_profitable": sum(1 for n in nets if n > 0),
                        "best": str(nets[-1]) if nets else None, "worst": str(nets[0]) if nets else None, "median": str(median) if median is not None else None,
                        "window": res.get("window"), "grid": grid[:80], "note": res.get("note")}
        row["by_instrument"] = None
    elif run.kind == "is_oos":
        row.update(in_sample=res.get("in_sample"), out_of_sample=res.get("out_of_sample"), split_at=res.get("split_at"))
        primary = res.get("out_of_sample") or {}
        row["metrics"] = {k: primary.get(k) for k in ("total_trades", "win_rate", "net_profit", "return_pct", "profit_factor_net", "max_drawdown_pct", "expectancy_per_trade")}
        row["by_instrument"] = None   # per-symbol detail exists for single runs only
    else:
        row["metrics"] = _row_metrics(res.get("metrics"))
        by = res.get("by_instrument") or {}
        row["by_instrument"] = {sym: {"trades": v.get("trades"), "net_pnl": v.get("net_pnl"), "win_rate": v.get("win_rate"), "expectancy": v.get("expectancy")} for sym, v in by.items()}
        nets = [_num(v.get("net_pnl")) for v in by.values()]
        nets = [n for n in nets if n is not None]
        row["symbols_profitable"] = sum(1 for n in nets if n > 0)
        row["symbols_tested"] = len(nets)
        months = [_num(x.get("net_pnl")) for x in (res.get("monthly") or [])]
        months = [x for x in months if x is not None]
        row["months_profitable"] = sum(1 for x in months if x > 0)
        row["months_tested"] = len(months)
    row["dataset"] = {"instruments": [{"symbol": i["symbol"], "bars": i["bars"], "start": i["start"], "end": i["end"]} for i in (res.get("dataset") or {}).get("instruments", [])]}
    row["cost_profile"] = ((res.get("assumptions") or {}).get("cost_profile") or {}).get("label")
    row["refused_instruments"] = [{"symbol": r.get("symbol"), "reason": r.get("reason")} for r in res.get("refused_instruments", [])]
    return row


def rank(rows: list[dict[str, Any]], *, min_trades: int = 30) -> list[dict[str, Any]]:
    """Order completed rows; rows below ``min_trades`` or unfinished sort last and are flagged.

    single / in-sample-vs-out-of-sample: net profit factor after charges (out-of-sample for the latter).
    walk_forward: net profit over test windows only (parameters were chosen on the preceding train window).
    sensitivity: the MEDIAN net across the parameter grid, so one lucky setting cannot win; fragile strategies sink.
    The rank is a convenience for scanning a table, not a recommendation: check per-symbol and per-month breadth too.
    """
    def score(row: dict[str, Any]) -> Decimal | None:
        m = row.get("metrics") or {}
        return _num(m.get("net_profit")) if row.get("kind") in ("walk_forward", "sensitivity") else _num(m.get("profit_factor_net"))

    def key(row: dict[str, Any]) -> tuple[int, Decimal]:
        value = score(row)
        trades = int((row.get("metrics") or {}).get("total_trades") or 0)
        usable = row.get("status") == "completed" and value is not None and trades >= min_trades
        return (0 if usable else 1, -(value if value is not None else Decimal(0)))

    ordered = sorted(rows, key=key)
    for position, row in enumerate(ordered, 1):
        usable = key(row)[0] == 0
        row["rank"] = position if usable else None
        row["low_sample"] = row.get("status") == "completed" and not usable
    return ordered


# ───────────────────────── comparison ─────────────────────────
def comparison(runs: list[Any], names: dict[Any, str]) -> dict[str, Any]:
    """Side-by-side view of 2-6 completed single runs: headline metrics, monthly net and per-symbol net on the symbols they share."""
    cols = []
    for run in runs:
        res = run.result or {}
        by = res.get("by_instrument") or {}
        cols.append({"run_id": str(run.id), "strategy": names.get(run.strategy_id, "(deleted strategy)"), "kind": run.kind, "status": run.status,
                     "metrics": _row_metrics(res.get("metrics")) if run.kind == "single" else None,
                     "monthly": {m["month"]: m.get("net_pnl") for m in (res.get("monthly") or [])},
                     "by_instrument": {sym: v.get("net_pnl") for sym, v in by.items()}})
    shared = set.intersection(*(set(c["by_instrument"]) for c in cols)) if cols else set()
    months = sorted(set().union(*(set(c["monthly"]) for c in cols))) if cols else []
    metric_keys = ["total_trades", "win_rate", "gross_pnl_before_costs", "total_charges", "total_slippage", "net_profit", "profit_factor_net",
                   "expectancy_per_trade", "max_drawdown_pct", "sharpe_ratio"]
    best: dict[str, str | None] = {}
    for k in ("net_profit", "profit_factor_net", "expectancy_per_trade", "sharpe_ratio"):
        scored = [(c["run_id"], _num((c["metrics"] or {}).get(k))) for c in cols]
        scored = [(rid, v) for rid, v in scored if v is not None]
        best[k] = max(scored, key=lambda t: t[1])[0] if scored else None
    return {"columns": cols, "shared_symbols": sorted(shared), "months": months, "metric_keys": metric_keys, "best": best,
            "note": "Hypothetical backtests. Compare runs only when their window, symbols and charge schedule match; differences otherwise are not like-for-like."}


def batch_csv(rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["rank", "strategy", "status", "trades", "win_rate", "gross_before_costs", "charges", "slippage", "net_profit", "profit_factor_net",
                     "expectancy_per_trade", "max_drawdown_pct", "sharpe", "symbols_profitable", "symbols_tested", "label"])
    for r in rows:
        m = r.get("metrics") or {}
        name = str(r.get("strategy", ""))
        if name.lstrip().startswith(("=", "+", "-", "@")):
            name = "'" + name   # spreadsheet formula injection guard
        writer.writerow([r.get("rank"), name, r.get("status"), m.get("total_trades"), m.get("win_rate"), m.get("gross_pnl_before_costs"), m.get("total_charges"),
                         m.get("total_slippage"), m.get("net_profit"), m.get("profit_factor_net"), m.get("expectancy_per_trade"), m.get("max_drawdown_pct"),
                         m.get("sharpe_ratio"), r.get("symbols_profitable"), r.get("symbols_tested"), "hypothetical backtest, not real P&L"])
    return out.getvalue()
