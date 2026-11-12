"""Run a library strategy backed by the scalp_hold engine against stored 1-minute bars."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.models import Instrument
from app.services.intraday_backtest import BacktestConfig
from app.services.intraday_strategy import StrategyDefinition
from scalp_hold.app_adapter import run_scalp_hold


def execute_scalp_hold(db: Session, defn: StrategyDefinition, kind: str, instruments: list[Instrument], cfg: BacktestConfig, window: tuple[datetime, datetime],
                       options: dict[str, Any]) -> dict[str, Any]:
    from app.services.intraday_runner import RunnerError, load_bars

    start, end = window
    frames: dict[str, tuple[pd.DataFrame, str]] = {}
    refused: list[dict[str, Any]] = []
    for inst in instruments:
        raw = load_bars(db, inst.id, start, end)
        if raw.empty:
            refused.append({"instrument_id": str(inst.id), "symbol": inst.symbol, "reason": "no stored 1-minute bars in this window"})
            continue
        df = raw.copy()
        df.index = pd.DatetimeIndex(df.pop("time"), name="ts_utc")
        frames[inst.symbol] = (df[["open", "high", "low", "close", "volume"]].astype(float), inst.asset_class)
    if not frames:
        raise RunnerError("no instrument has stored 1-minute bars in this window: " + "; ".join(f"{r['symbol']}: {r['reason']}" for r in refused))
    try:
        result = run_scalp_hold(frames, defn.market, kind, dict((defn.source.provenance or {}).get("params", {})), equity=float(cfg.initial_capital),
                                max_lev=float(cfg.leverage) if cfg.leverage and cfg.leverage > 1 else None, slip_bps=float(cfg.slippage_bps), max_missing_pct=float(cfg.max_missing_pct),
                                quick=bool(options.get("quick")), overrides=dict(options.get("params") or {}))
    except ValueError as error:
        raise RunnerError(str(error)) from None
    by_symbol = {i.symbol: i for i in instruments}
    result["refused_instruments"] = refused + [{**r, "instrument_id": str(by_symbol[r["symbol"]].id) if r["symbol"] in by_symbol else None} for r in result["refused_instruments"]]
    if not result["instruments"]:
        raise RunnerError("no instrument could be evaluated: " + "; ".join(f"{r['symbol']}: {r['reason']}" for r in result["refused_instruments"]))
    result["data_window"] = {"start": start.isoformat(), "end": end.isoformat()}
    return result
