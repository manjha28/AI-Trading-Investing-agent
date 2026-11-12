"""Performance analytics for the intraday backtester. Money stays Decimal (serialised as strings); ratios are floats."""
from __future__ import annotations

import math
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import pandas as pd

from app.services.intraday_backtest import MAX_EQUITY_POINTS, MIN_DAYS_RATIOS, MIN_TRADES_RELIABLE

D = Decimal
C = D("0.01")


def _s(x: Decimal) -> str:
    return str(x.quantize(C, ROUND_HALF_UP))


def _r(x: float | None, places: int = 4) -> float | None:
    return None if x is None or not math.isfinite(x) else round(float(x), places)


def _streaks(flags: list[bool]) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def _bucket(minute: int, market: str) -> str:
    if market == "crypto":
        hour = minute // 60
        return "asia 00-08 UTC" if hour < 8 else "europe 08-16 UTC" if hour < 16 else "us 16-24 UTC"
    return "open 09:15-10:30" if minute < 630 else "midday 10:30-13:30" if minute < 810 else "close 13:30-15:30"


def _group(trades: list[dict[str, Any]], key) -> dict[str, dict[str, Any]]:
    out: dict[str, list[Decimal]] = defaultdict(list)
    for t in trades:
        out[key(t)].append(D(t["net_pnl"]))
    return {k: {"trades": len(v), "net_pnl": _s(sum(v, D(0))), "win_rate": _r(sum(1 for x in v if x > 0) / len(v)),
                "expectancy": _s(sum(v, D(0)) / len(v))} for k, v in sorted(out.items())}


def summarize(eng: Any, et: list[int], ev: list[float]) -> dict[str, Any]:
    trades = eng.trades
    cap = eng.cfg.initial_capital
    market = eng.market
    net = [D(t["net_pnl"]) for t in trades]
    gross = [D(t["gross_pnl"]) for t in trades]
    wins, losses = [x for x in net if x > 0], [x for x in net if x < 0]
    warnings = list(eng.warnings)
    n = len(trades)
    total_net = sum(net, D(0))
    m: dict[str, Any] = {
        "total_trades": n, "winning_trades": len(wins), "losing_trades": len(losses), "breakeven_trades": n - len(wins) - len(losses),
        "win_rate": _r(len(wins) / n) if n else None,
        "gross_pnl_before_costs": _s(sum(gross, D(0))), "gross_profit": _s(sum((g for g in gross if g > 0), D(0))),
        "gross_loss": _s(sum((g for g in gross if g < 0), D(0))),
        "total_slippage": _s(sum((D(t["slippage_cost"]) for t in trades), D(0))), "total_charges": _s(sum((D(t["charges"]) for t in trades), D(0))),
        "net_profit": _s(total_net), "return_pct": _r(float(total_net / cap * 100), 3),
        "avg_win": _s(sum(wins, D(0)) / len(wins)) if wins else None, "avg_loss": _s(sum(losses, D(0)) / len(losses)) if losses else None,
        "expectancy_per_trade": _s(total_net / n) if n else None,
        "largest_win": _s(max(net)) if n and max(net) > 0 else None, "largest_loss": _s(min(net)) if n and min(net) < 0 else None,
        "avg_holding_minutes": _r(sum(t["holding_minutes"] for t in trades) / n, 1) if n else None,
        "max_consecutive_wins": _streaks([x > 0 for x in net]), "max_consecutive_losses": _streaks([x < 0 for x in net]),
        "profit_factor_net": _r(float(sum(wins, D(0)) / abs(sum(losses, D(0))))) if losses else None,
        "avg_risk_reward": _r(float((sum(wins, D(0)) / len(wins)) / abs(sum(losses, D(0)) / len(losses)))) if wins and losses else None,
        "avg_r_multiple": _r(float(sum(D(t["r_multiple"]) for t in trades if t["r_multiple"] is not None) / max(1, sum(1 for t in trades if t["r_multiple"] is not None)))) if any(t["r_multiple"] is not None for t in trades) else None,
    }
    bds = [t["cost_breakdown"] for t in trades if t.get("cost_breakdown")]
    tot = lambda k: sum((D(b[k]) for b in bds), D(0))  # noqa: E731
    turnover = tot("turnover")
    m.update({"total_buy_turnover": _s(tot("buy_turnover")), "total_sell_turnover": _s(tot("sell_turnover")), "total_turnover": _s(turnover),
              "charges_pct_of_turnover": _r(float(D(m["total_charges"]) / turnover * 100), 4) if turnover else None,
              "cost_components": {k: _s(tot(k)) for k in ("brokerage", "stt", "exchange_txn", "sebi", "ipft", "stamp_duty", "gst", "dp_charges", "other")},
              "slippage_is_separate_from_charges": True})
    gp, gl = sum((g for g in gross if g > 0), D(0)), sum((g for g in gross if g < 0), D(0))
    m["profit_factor_gross"] = _r(float(gp / abs(gl))) if gl else None
    # equity & drawdown (mark-to-market on every base timestamp)
    eq = np.array(ev, dtype=float)
    times = pd.to_datetime(np.array(et, dtype="int64"), utc=True) if et else pd.DatetimeIndex([], tz="UTC")
    start_eq = float(cap)
    peak = np.maximum.accumulate(np.r_[start_eq, eq])[1:] if len(eq) else eq
    dd = (eq - peak) / peak if len(eq) else np.array([])
    m["max_drawdown_pct"] = _r(float(dd.min() * 100), 3) if len(dd) else 0.0
    m["max_drawdown_amount"] = _s(D(repr(float((eq - peak).min())))) if len(dd) else "0.00"
    idx = np.arange(len(eq))
    if len(eq) > MAX_EQUITY_POINTS:
        keep = set(np.linspace(0, len(eq) - 1, MAX_EQUITY_POINTS).astype(int)) | {int(dd.argmin())}
        idx = np.array(sorted(keep))
    m_curve = [{"t": times[i].isoformat(), "equity": round(float(eq[i]), 2), "drawdown_pct": round(float(dd[i] * 100), 4)} for i in idx]
    # daily-return based ratios
    tz = eng.runs[0].profile.tz if eng.runs else "UTC"
    ratios_reason = None
    sharpe = sortino = None
    if len(eq):
        s = pd.Series(eq, index=times.tz_convert(tz).strftime("%Y-%m-%d"))
        daily = s.groupby(level=0).last()
        ret = daily.pct_change().dropna()
        first = daily.iloc[0] / start_eq - 1
        ret = pd.concat([pd.Series([first]), ret.reset_index(drop=True)]) if len(daily) else ret
        if len(ret) < MIN_DAYS_RATIOS or n < MIN_TRADES_RELIABLE:
            ratios_reason = f"needs >= {MIN_DAYS_RATIOS} trading days and >= {MIN_TRADES_RELIABLE} trades (have {len(ret)} days, {n} trades)"
        elif ret.std(ddof=1) > 0:
            ann = math.sqrt(365 if market == "crypto" else 252)
            sharpe = _r(float(ret.mean() / ret.std(ddof=1) * ann), 3)
            downside = np.sqrt(np.mean(np.minimum(ret.to_numpy(), 0) ** 2))
            sortino = _r(float(ret.mean() / downside * ann), 3) if downside > 0 else None
    m["sharpe_ratio"], m["sortino_ratio"], m["ratios_note"] = sharpe, sortino, ratios_reason or "daily returns, risk-free rate 0, annualised"
    monthly = defaultdict(lambda: D(0))
    for t in trades:
        monthly[t["exit_day"][:7]] += D(t["net_pnl"])
    by_exit = [{"month": k, "net_pnl": _s(v), "return_pct_of_initial": _r(float(v / cap * 100), 3)} for k, v in sorted(monthly.items())]
    if n < MIN_TRADES_RELIABLE:
        warnings.append(f"only {n} trade(s): fewer than {MIN_TRADES_RELIABLE} is too small a sample for the statistics to be reliable")
    elif n < 100:
        warnings.append(f"{n} trades: a moderate sample; treat the result as indicative only")
    for r in eng.runs:
        if r.quality.get("status") == "warn":
            warnings.append(f"{r.symbol}: data-quality warnings - " + "; ".join(r.quality.get("issues", [])))
    eod = sum(1 for t in trades if t["exit_reason"] == "end_of_data")
    if eod:
        warnings.append(f"{eod} trade(s) were closed only because the data window ended")
    amb = sum(1 for t in trades if t["ambiguous_stop_and_target_same_bar"])
    if amb:
        warnings.append(f"{amb} trade(s) touched both stop and target inside one 1-minute bar; the stop was assumed first")
    reasons: dict[str, int] = defaultdict(int)
    for t in trades:
        reasons[t["exit_reason"]] += 1
    bench = []
    for r in eng.runs:
        f = r.frame
        if len(f) > 1:
            bench.append({"symbol": r.symbol, "buy_and_hold_return_pct": _r(float((f["close"].iloc[-1] / f["open"].iloc[0] - 1) * 100), 3)})
    if bench:
        bench_avg = _r(sum(b["buy_and_hold_return_pct"] for b in bench) / len(bench), 3)
    else:
        bench_avg = None
    edges = None
    hist = None
    if n >= 5:
        vals = np.array([float(x) for x in net])
        counts, edges_ = np.histogram(vals, bins=min(10, max(3, n // 3)))
        hist = [{"from": round(float(edges_[i]), 2), "to": round(float(edges_[i + 1]), 2), "count": int(counts[i])} for i in range(len(counts))]
    return {
        "metrics": m, "equity_curve": m_curve, "monthly": by_exit,
        "by_side": _group(trades, lambda t: t["side"]), "by_instrument": _group(trades, lambda t: t["symbol"]),
        "by_session": _group(trades, lambda t: _bucket(t["entry_minute_local"], market)),
        "exit_reasons": dict(reasons), "skipped_signals": dict(eng.skipped), "trade_distribution": hist,
        "benchmark": {"label": "Buy & hold, first open to last close, before costs (reference only)", "per_instrument": bench, "average_return_pct": bench_avg},
        "trades": trades, "warnings": warnings,
        "sample": {"trades": n, "reliable": n >= MIN_TRADES_RELIABLE},
    }


def assumptions(eng: Any) -> dict[str, Any]:
    d, c = eng.defn, eng.cfg
    return {
        "market": eng.market, "base_timeframe": "1m", "decision_timeframe": d.decision_timeframe,
        "entry_fill": "next base bar open after the signal bar closes; skipped if that bar is not contiguous (same session, within max_fill_delay_minutes)",
        "max_fill_delay_minutes": c.max_fill_delay_minutes,
        "slippage_bps": str(c.slippage_bps), "slippage_model": c.slippage_model, "slippage_ticks": c.slippage_ticks,
        "slippage_rule": "adverse to the trader on market and stop fills (bps of price, unrounded; or N ticks); none on limit (target) fills. Reported separately from statutory charges.",
        "pnl_identity": "net = gross (raw prices) - slippage - charges; charges are computed once per trade by the central cost engine on executed prices",
        "same_bar_stop_and_target": "stop assumed first (1-minute is the finest data held); flagged on the trade",
        "gap_through_stop": "fills at the bar open", "gap_through_target": "fills at the bar open (limit fill, no slippage)",
        "trailing": "highest/lowest price through the previous bar; a bar never trails on information from inside itself",
        "cost_profile": eng.costs.describe(), "cost_overrides": {k: str(v) for k, v in c.cost_overrides.items()},
        "costs_note": "Charges follow the configured fee schedule (assumptions to verify against a contract note), not broker-confirmed values. Income tax is never included.",
        "tick_size": str(eng.tick), "qty_step": str(eng.step), "leverage": str(c.leverage), "initial_capital": str(c.initial_capital),
        "sizing": d.sizing.model_dump(), "sizing_basis": "realised equity (cash after closed trades and costs); open P&L is not compounded into new entries",
        "stop": d.stop.model_dump(), "target": d.target.model_dump(), "trail": d.trail.model_dump(),
        "max_concurrent": c.max_concurrent, "direction": {"long": eng.allow_long, "short": eng.allow_short},
        "square_off_minute_local": eng.sq, "no_entry_after_minute_local": eng.no_entry,
        "session_rules": "Indian equity: forced flat at square-off, no overnight positions. Crypto: continuous, no session rules.",
        "max_trades_per_day_scope": "per instrument per local day", "window": {"start": c.start.isoformat() if c.start else None, "end": c.end.isoformat() if c.end else None},
        "ratio_basis": "Sharpe/Sortino from daily mark-to-market returns, risk-free 0, only with enough days and trades",
    }
