"""Options-buying simulation on top of the existing intraday engine.

Reused from the existing engine: ``evaluate()`` (the SAME condition trees produce the underlying signal in live scans and backtests), completed-candle
aggregation, the central transaction-cost engine, and ``intraday_metrics.summarize``. New here: contract selection per signal, quote/candle execution on the
OPTION series, lot sizing against a risk budget, premium/underlying exits and the options report. The single-instrument bar loop of the equity engine cannot
express "select a contract at the signal time and trade a different series", so this module owns only that part.

Execution rules (echoed in every result):
- A signal exists at the CLOSE of the underlying 1-minute candle. Contract selection uses only contracts/quotes with timestamps <= that instant.
- Entry: first executable option quote strictly after signal + latency, bought at the ASK (+ slippage ticks). Exit: SOLD at the BID (- slippage ticks).
  ``indicative_ohlc`` mode (explicitly labelled, never "executable"): next option candle open plus an assumed half-spread each way.
- Premium stop and underlying invalidation both apply; whichever fires first closes the trade. Stop and target in one candle -> stop first, flagged.
- Quantity is whole lots sized from the risk budget (stop distance x lot size + estimated round-trip charges); a single lot over budget is rejected.
- No data, no result: with no quotes (quote mode) or candles (indicative mode) a signal is a NO TRADE with the reason recorded. Nothing is substituted.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from app.config import NSE_CALENDAR_YEARS
from app.services import intraday_indicators as ti
from app.services.intraday_metrics import summarize
from app.services.intraday_strategy import ENGINE_VERSION, EvalResult, StrategyDefinition, explain
from app.services.option_selector import SelectionConfig, select_contract
from app.services.options_data import IST, Candle, Contract, OptionDataSource, Quote, aware, dataset_fingerprint
from app.services.timeframes import MINUTE, MarketProfile
from app.services.transaction_costs import compute_charges, get_schedule, trade_executions

D = Decimal
OPTIONS_ENGINE_VERSION = "options-engine/1.0.0"
CENT = D("0.01")


def selection_config(defn: StrategyDefinition) -> SelectionConfig:
    s = defn.options.selection
    return SelectionConfig(allow_ce=s.allow_ce and defn.constraints.allow_long, allow_pe=s.allow_pe and defn.constraints.allow_short, strike_choices=tuple(s.strike_choices),
                           delta_min=s.delta_min, delta_max=s.delta_max, delta_preferred=s.delta_preferred, expiry_rank=s.expiry_rank, allow_expiry_day=s.allow_expiry_day,
                           max_spread_pct=s.max_spread_pct, quote_max_age_seconds=s.quote_max_age_seconds, min_volume=s.min_volume, min_depth_lots=s.min_depth_lots,
                           allow_calculated_greeks=s.allow_calculated_greeks, risk_free_rate_pct=s.risk_free_rate_pct)


def _hhmm(v: str) -> int:
    return int(v[:2]) * 60 + int(v[3:])


def _q(x: Decimal, tick: Decimal) -> Decimal:
    return (x / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def _plain(x: Decimal, places: str = "0.0001") -> str:
    s = format(x.quantize(D(places), ROUND_HALF_UP), "f")
    while s.endswith("0") and len(s.split(".")[1]) > 2:
        s = s[:-1]
    return s


class _Trade:
    pass


def _blackout(spec, ts: datetime) -> str | None:
    for b in spec.event_blackouts:
        if aware(datetime.fromisoformat(b.start)) <= ts < aware(datetime.fromisoformat(b.end)):
            return b.label or "event_blackout"
    return None


def run_options_backtest(defn: StrategyDefinition, frame: pd.DataFrame, res: EvalResult, profile: MarketProfile, source: OptionDataSource, *, initial_capital: Decimal,
                         spot_frame: pd.DataFrame | None = None, symbol: str = "NIFTY signal", instrument_id: str = "signal", direction: str = "both",
                         overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = defn.options
    if spec is None:
        raise ValueError("not an options strategy")
    ex = spec.execution.model_copy(update=overrides or {})
    rk, u = spec.risk, spec.underlying
    schedule = get_schedule(ex.fee_schedule)
    sel_cfg = selection_config(defn)
    tick = D(str(ex.tick_size))
    slip = tick * ex.slippage_ticks
    half = D(str(ex.ohlc_half_spread_pct)) / 100
    stop_frac = D(str(ex.premium_stop_pct)) / 100
    t_arr = frame["time"].reset_index(drop=True)
    close = frame["close"].to_numpy(float)
    ctx = ti.Ctx(profile, "1m")
    sw = ti.compute("swing", frame.reset_index(drop=True), {"left": u.swing_left, "right": u.swing_right}, ctx)
    spot_close = None
    if spot_frame is not None and len(spot_frame):
        spot_close = dict(zip(spot_frame["time"], spot_frame["close"]))
    ref_label = "spot index" if spot_close is not None else "futures price (proxy: includes basis)"
    sig = res.signals
    cap = initial_capital
    cash = initial_capital
    trades: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    day_pnl: dict[Any, Decimal] = defaultdict(lambda: D(0))
    busy_until: datetime | None = None
    warnings: list[str] = []
    years: set[int] = set()
    all_quotes: list[Quote] = []
    all_contracts: dict[str, Contract] = {}
    start_min, last_min = _hhmm(u.start_time), _hhmm(u.last_entry_time)
    n = len(frame)

    def skip(reason: str, i: int, extra: dict[str, Any] | None = None) -> None:
        skipped[reason] += 1
        log.append({"signal_time": (t_arr.iloc[i] + MINUTE).isoformat(), "result": "no_trade", "reason": reason, **(extra or {})})

    for i in np.flatnonzero(sig["long_entry"] | sig["short_entry"]):
        i = int(i)
        sig_time = aware((t_arr.iloc[i] + MINUTE).to_pydatetime())
        local = sig_time.astimezone(IST)
        years.add(local.year)
        if sig["long_entry"][i] and sig["short_entry"][i]:
            skip("conflicting_long_and_short_signal", i)
            continue
        side = "long" if sig["long_entry"][i] else "short"
        if direction == "long" and side == "short" or direction == "short" and side == "long":
            skip("direction_filtered", i)
            continue
        mod = local.hour * 60 + local.minute
        if not (start_min <= mod <= last_min):
            skip("outside_entry_window", i)
            continue
        if busy_until is not None and sig_time < busy_until:
            skip("position_already_open", i)
            continue
        why = _blackout(spec, sig_time)
        if why:
            skip(f"event_blackout:{why}", i)
            continue
        if day_pnl[local.date()] <= -(initial_capital * D(str(rk.daily_loss_limit_pct)) / 100):
            skip("daily_loss_limit_reached", i)
            continue
        ref = D(repr(float(spot_close[t_arr.iloc[i]]))) if spot_close is not None and t_arr.iloc[i] in spot_close else (D(repr(float(close[i]))) if spot_close is None else None)
        selection = select_contract(source, sel_cfg, underlying=u.underlying, direction=side, signal_time=sig_time, reference_price=ref, reference_label=ref_label)
        if not selection.ok:
            skip("no_contract:" + (selection.no_trade_reasons[0] if selection.no_trade_reasons else "unknown"), i, {"selection": selection.as_dict()})
            continue
        c, q0 = selection.contract, selection.quote
        lot = c.lot_size
        all_contracts[c.id] = c
        # ───── entry on the OPTION series, strictly after the signal could exist ─────
        earliest = sig_time + timedelta(seconds=ex.entry_latency_seconds)
        wait_end = sig_time + timedelta(seconds=ex.max_entry_wait_seconds)
        entry_time = entry_raw = None
        e_bid = e_ask = None
        if ex.price_mode == "quote_accurate":
            for q in source.quotes_between(c.id, earliest, wait_end):
                if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask >= q.bid:
                    spr = float((q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100)
                    if spr > sel_cfg.max_spread_pct:
                        skip("spread_widened_at_execution", i, {"selection": selection.as_dict()})
                        entry_time = False
                        break
                    entry_time, entry_raw, e_bid, e_ask = aware(q.time), q.ask, q.bid, q.ask
                    all_quotes.append(q)
                    break
            if entry_time is None:
                skip("no_executable_quote_after_signal", i, {"selection": selection.as_dict()})
                continue
            if entry_time is False:
                continue
        else:
            cs = [x for x in source.candles_between(c.id, sig_time, wait_end) if aware(x.time) >= sig_time]
            if not cs:
                skip("no_option_candle_after_signal", i, {"selection": selection.as_dict()})
                continue
            entry_time, entry_raw = aware(cs[0].time), cs[0].open * (1 + half)
            e_bid, e_ask = cs[0].open * (1 - half), entry_raw
        entry_fill = entry_raw + slip
        stop_price = _q(entry_fill * (1 - stop_frac), tick)
        target_price = _q(entry_fill * (1 + stop_frac * D(str(ex.target_r))), tick)
        # ───── sizing: whole lots from the risk budget ─────
        per_unit = entry_fill - stop_price
        est = compute_charges(trade_executions("long", D(lot), entry_fill, stop_price), schedule).total
        per_lot_risk = per_unit * lot + est
        if rk.sizing_method == "fixed_lots":
            lots = rk.fixed_lots
        else:
            lots = int((cash * D(str(rk.risk_per_trade_pct)) / 100 / per_lot_risk).to_integral_value(rounding=ROUND_FLOOR)) if per_lot_risk > 0 else 0
        if lots < 1:
            skip("single_lot_exceeds_risk_budget", i, {"per_lot_risk": str(per_lot_risk.quantize(CENT)), "budget": str((cash * D(str(rk.risk_per_trade_pct)) / 100).quantize(CENT)), "selection": selection.as_dict()})
            continue
        afford = int((cash / (entry_fill * lot)).to_integral_value(rounding=ROUND_FLOOR))
        if afford < 1:
            skip("insufficient_capital", i)
            continue
        lots = min(lots, afford)
        qty = D(lots * lot)
        # ───── underlying-driven exit triggers (completed 1-minute candles after the signal) ─────
        deadline = entry_time + timedelta(minutes=ex.max_hold_minutes)
        inv = sw["low" if side == "long" else "high"][i]
        triggers: list[tuple[datetime, str]] = []
        opp_rule, thesis_rule = ("short_entry", "long_exit") if side == "long" else ("long_entry", "short_exit")
        for k in range(i + 1, n):
            ct = aware((t_arr.iloc[k] + MINUTE).to_pydatetime())
            if ct > deadline + timedelta(seconds=ex.max_entry_wait_seconds):
                break
            if ex.underlying_invalidation and not math.isnan(inv) and ((side == "long" and close[k] < inv) or (side == "short" and close[k] > inv)):
                triggers.append((ct, "underlying_invalidation"))
                break
            if ex.opposite_signal_exit and sig[opp_rule][k]:
                triggers.append((ct, "opposite_signal"))
                break
            if ex.thesis_exit and sig[thesis_rule][k]:
                triggers.append((ct, "thesis_invalidated"))
                break
        trig = triggers[0] if triggers else None
        # ───── walk the option series ─────
        exit_time = exit_raw = None
        reason = ""
        ambiguous = gapped = unresolved = False
        hwm = entry_fill
        mae = mfe = D(0)
        x_bid = x_ask = None
        trail_on = False
        path_quotes: list[Quote] = []
        if ex.price_mode == "quote_accurate":
            path_quotes = [q for q in source.quotes_between(c.id, entry_time, deadline + timedelta(seconds=ex.max_entry_wait_seconds)) if aware(q.time) > entry_time and q.bid is not None and q.bid > 0]
            all_quotes.extend(path_quotes)
            for q in path_quotes:
                qt = aware(q.time)
                bid = q.bid
                rel = (bid - entry_fill) / entry_fill * 100
                mae, mfe = min(mae, rel), max(mfe, rel)
                fired = None
                if trig and qt >= trig[0] + timedelta(seconds=ex.entry_latency_seconds):
                    fired = trig[1]
                elif bid <= stop_price:
                    fired, gapped = "premium_stop", bid < stop_price
                elif ex.trailing_distance_pct and ex.trailing_activation_r is not None and hwm >= entry_fill * (1 + stop_frac * D(str(ex.trailing_activation_r))):
                    trail_on = True
                    if bid <= hwm * (1 - D(str(ex.trailing_distance_pct)) / 100):
                        fired = "trailing_stop"
                elif ex.trailing_distance_pct and ex.trailing_activation_r is None and bid <= hwm * (1 - D(str(ex.trailing_distance_pct)) / 100) and hwm > entry_fill:
                    fired = "trailing_stop"
                if fired is None and bid >= target_price:
                    fired = "target"
                if fired is None and qt >= deadline:
                    fired = "time_exit"
                if fired:
                    exit_time, exit_raw, reason, x_bid, x_ask = qt, bid, fired, q.bid, q.ask
                    break
                hwm = max(hwm, bid)
            if exit_time is None:     # quotes ran out before any exit: close on the last known bid, flagged, never on a made-up price
                if path_quotes:
                    q = path_quotes[-1]
                    exit_time, exit_raw, reason, x_bid, x_ask = aware(q.time), q.bid, "exit_quote_unavailable_closed_at_last_bid", q.bid, q.ask
                    unresolved = True
                else:
                    skip("no_quotes_after_entry", i)
                    continue
        else:
            cands = [x for x in source.candles_between(c.id, entry_time, deadline + timedelta(seconds=ex.max_entry_wait_seconds)) if aware(x.time) >= entry_time]
            for idx, cd in enumerate(cands):
                ct0 = aware(cd.time)
                bid_open = cd.open * (1 - half)
                rel_lo, rel_hi = (cd.low * (1 - half) - entry_fill) / entry_fill * 100, (cd.high * (1 - half) - entry_fill) / entry_fill * 100
                mae, mfe = min(mae, rel_lo), max(mfe, rel_hi)
                fired = None
                px = bid_open
                if idx > 0 and trig and ct0 >= trig[0]:
                    fired = trig[1]
                elif idx > 0 and bid_open <= stop_price:
                    fired, gapped = "premium_stop", True
                elif idx > 0 and bid_open >= target_price:
                    fired = "target"
                elif ct0 >= deadline:
                    fired = "time_exit"
                else:
                    stop_hit = cd.low <= stop_price
                    tgt_hit = cd.high * (1 - half) >= target_price
                    if stop_hit:
                        fired, px, ambiguous = "premium_stop", stop_price * (1 - half), tgt_hit
                    elif tgt_hit:
                        fired, px = "target", target_price
                if fired:
                    exit_time, exit_raw, reason, x_bid, x_ask = ct0, px, fired, px, px / (1 - half) * (1 + half)
                    break
            if exit_time is None:
                if cands:
                    last = cands[-1]
                    exit_time, exit_raw, reason = aware(last.time) + MINUTE, last.close * (1 - half), "exit_candle_unavailable_closed_at_last_close"
                    x_bid, x_ask, unresolved = exit_raw, last.close * (1 + half), True
                else:
                    skip("no_option_candles_after_entry", i)
                    continue
        exit_fill = max(exit_raw - slip, D("0.05"))
        costs = compute_charges(trade_executions("long", qty, entry_fill, exit_fill), schedule)
        gross = (exit_raw - entry_raw) * qty                       # raw executable prices: ask in, bid out
        slippage_cost = (slip + (exit_raw - exit_fill)) * qty
        net = (exit_fill - entry_fill) * qty - costs.total
        cash += net
        busy_until = exit_time
        day_pnl[local.date()] += net
        risk_amt = per_unit * qty
        e_spread = float((e_ask - e_bid) / ((e_ask + e_bid) / 2) * 100) if e_bid and e_ask and e_bid + e_ask else None
        x_spread = float((x_ask - x_bid) / ((x_ask + x_bid) / 2) * 100) if x_bid and x_ask and x_bid + x_ask else None
        exit_local = exit_time.astimezone(IST)
        trades.append({
            "instrument_id": instrument_id, "symbol": c.symbol, "side": "long", "qty": str(int(qty)), "signal_time": sig_time.isoformat(), "entry_time": entry_time.isoformat(),
            "exit_time": exit_time.isoformat(), "entry_raw": _plain(entry_raw), "entry_fill": _plain(entry_fill), "exit_raw": _plain(exit_raw), "exit_fill": _plain(exit_fill),
            "exit_reason": reason, "stop_initial": _plain(stop_price), "target": _plain(target_price), "gross_pnl": str(gross.quantize(CENT, ROUND_HALF_UP)),
            "slippage_cost": str(slippage_cost.quantize(CENT, ROUND_HALF_UP)), "charges": str(costs.total.quantize(CENT, ROUND_HALF_UP)), "cost_breakdown": costs.as_dict(),
            "net_pnl": str(net.quantize(CENT, ROUND_HALF_UP)), "r_multiple": str((net / risk_amt).quantize(CENT, ROUND_HALF_UP)) if risk_amt else None,
            "holding_minutes": round((exit_time - entry_time).total_seconds() / 60, 2), "ambiguous_stop_and_target_same_bar": ambiguous,
            "entry_minute_local": entry_time.astimezone(IST).hour * 60 + entry_time.astimezone(IST).minute, "exit_day": exit_local.strftime("%Y-%m-%d"),
            "explain": explain(res, "long_entry" if side == "long" else "short_entry", i),
            "option": {"option_type": c.option_type, "underlying_direction": side, "strike": str(c.strike), "expiry": c.expiry_date.isoformat(), "lot_size": lot, "lots": lots, "delta": selection.delta,
                       "delta_source": selection.delta_source, "entry_bid": None if e_bid is None else _plain(e_bid), "entry_ask": None if e_ask is None else _plain(e_ask),
                       "entry_spread_pct": None if e_spread is None else round(e_spread, 4), "exit_bid": None if x_bid is None else _plain(x_bid), "exit_ask": None if x_ask is None else _plain(x_ask),
                       "exit_spread_pct": None if x_spread is None else round(x_spread, 4), "price_source": "quote" if ex.price_mode == "quote_accurate" else "ohlc_indicative",
                       "selection_why": selection.why, "contract_id": c.id, "master_version": c.master_version, "underlying_close_at_signal": str(close[i]),
                       "invalidation_level": None if math.isnan(inv) else round(float(inv), 4), "mae_pct": round(float(mae), 3), "mfe_pct": round(float(mfe), 3),
                       "stop_pct": ex.premium_stop_pct, "target_r": ex.target_r, "gap_through_stop": gapped, "exit_unresolved": unresolved, "reference": ref_label,
                       "config": f"stop {ex.premium_stop_pct:g}% / {ex.target_r:g}R"},
        })
        log.append({"signal_time": sig_time.isoformat(), "result": "traded", "selection": selection.as_dict()})

    for y in sorted(years):
        if y not in NSE_CALENDAR_YEARS:
            warnings.append(f"NSE holiday calendar is not configured for {y}: trading days are inferred from the presence of data, not from a verified calendar")
    if ex.price_mode == "indicative_ohlc":
        warnings.append("INDICATIVE OHLC backtest: option prices are candle prices plus an assumed half-spread each way. This is not quote-accurate or executable evidence.")
    if not get_schedule(ex.fee_schedule).verification.startswith("Rates as listed"):
        warnings.append("Options charges use an UNVERIFIED fee schedule; net P&L must not be trusted until the rates are confirmed against a contract note")
    if unresolved := sum(1 for t in trades if t["option"]["exit_unresolved"]):
        warnings.append(f"{unresolved} trade(s) had no option quote/candle at the planned exit and were closed on the last known price")
    # equity series (realised) for the shared metrics
    et, ev, eq = [], [], initial_capital
    for t in sorted(trades, key=lambda t: t["exit_time"]):
        eq += D(t["net_pnl"])
        et.append(int(pd.Timestamp(t["exit_time"]).as_unit("ns").value))
        ev.append(float(eq))
    fake = SimpleNamespace(trades=trades, cfg=SimpleNamespace(initial_capital=initial_capital), market="equity_in", warnings=warnings, skipped=dict(skipped), costs=schedule,
                           runs=[SimpleNamespace(profile=profile, quality={}, symbol=symbol, frame=pd.DataFrame())])
    out = summarize(fake, et, ev)
    out["benchmark"] = {"label": "Not applicable to options trades", "per_instrument": [], "average_return_pct": None}
    out["options_report"] = options_report(trades, log, skipped, ex)
    out["assumptions"] = options_assumptions(defn, schedule, ref_label, sel_cfg)
    out["versions"] = {"engine": ENGINE_VERSION, "options_engine": OPTIONS_ENGINE_VERSION, "indicator_library": ti.INDICATOR_LIBRARY_VERSION}
    out["selection_log"] = log[:500]
    out["option_dataset"] = {"fingerprint_sha256": dataset_fingerprint(list(all_contracts.values()), all_quotes), "contracts_selected": len(all_contracts), "quotes_used": len(all_quotes),
                             "master_versions": sorted({c.master_version for c in all_contracts.values()}), "price_source": "quote" if ex.price_mode == "quote_accurate" else "ohlc_indicative"}
    return out


def _bucket_delta(d: float | None) -> str:
    if d is None:
        return "unknown"
    a = abs(d)
    lo = math.floor(a * 20) / 20
    return f"{lo:.2f}-{lo + 0.05:.2f}"


def _grp(trades, key) -> dict[str, dict[str, Any]]:
    g: dict[str, list[Decimal]] = defaultdict(list)
    for t in trades:
        g[key(t)].append(D(t["net_pnl"]))
    return {k: {"trades": len(v), "net_pnl": str(sum(v, D(0)).quantize(CENT)), "win_rate": round(sum(1 for x in v if x > 0) / len(v), 4), "expectancy": str((sum(v, D(0)) / len(v)).quantize(CENT))}
            for k, v in sorted(g.items())}


def options_report(trades: list[dict[str, Any]], log: list[dict[str, Any]], skipped: dict[str, int], ex) -> dict[str, Any]:
    avg = lambda vals: round(sum(vals) / len(vals), 4) if vals else None  # noqa: E731
    o = [t["option"] for t in trades]
    return {
        "ce_trades": sum(1 for x in o if x["option_type"] == "CE"), "pe_trades": sum(1 for x in o if x["option_type"] == "PE"),
        "avg_entry_spread_pct": avg([x["entry_spread_pct"] for x in o if x["entry_spread_pct"] is not None]), "avg_exit_spread_pct": avg([x["exit_spread_pct"] for x in o if x["exit_spread_pct"] is not None]),
        "avg_delta_at_entry": avg([abs(x["delta"]) for x in o if x["delta"] is not None]), "avg_mae_pct": avg([x["mae_pct"] for x in o]), "avg_mfe_pct": avg([x["mfe_pct"] for x in o]),
        "max_holding_minutes": max((t["holding_minutes"] for t in trades), default=None),
        "by_option_type": _grp(trades, lambda t: t["option"]["option_type"]), "by_delta_bucket": _grp(trades, lambda t: _bucket_delta(t["option"]["delta"])),
        "by_expiry": _grp(trades, lambda t: t["option"]["expiry"]), "by_hour_ist": _grp(trades, lambda t: f"{t['entry_minute_local'] // 60:02d}:00"),
        "by_config": _grp(trades, lambda t: t["option"]["config"]), "by_price_source": _grp(trades, lambda t: t["option"]["price_source"]),
        "greeks_sources": {k: sum(1 for x in o if x["delta_source"] == k) for k in ("provider", "calculated")},
        "signals": {"total": len(log), "traded": sum(1 for r in log if r["result"] == "traded"), "no_trade_reasons": dict(sorted(skipped.items(), key=lambda kv: -kv[1]))},
        "price_basis": "actual recorded option quotes (ask in / bid out)" if ex.price_mode == "quote_accurate" else "INDICATIVE: option candle prices with an assumed half-spread; not executable quotes",
    }


def options_assumptions(defn: StrategyDefinition, schedule, ref_label: str, sel_cfg: SelectionConfig) -> dict[str, Any]:
    ex = defn.options.execution
    return {
        "strategy_type": "options_buying", "underlying": defn.options.underlying.underlying, "signal_source": defn.options.underlying.signal_source, "atm_reference": ref_label,
        "price_mode": ex.price_mode, "entry": "first executable option quote strictly after signal + latency, bought at the ASK" if ex.price_mode == "quote_accurate" else "next option candle open + assumed half-spread (INDICATIVE)",
        "exit": "sold at the BID" if ex.price_mode == "quote_accurate" else "candle prices minus assumed half-spread (INDICATIVE)", "entry_latency_seconds": ex.entry_latency_seconds,
        "premium_stop_pct": ex.premium_stop_pct, "target_r": ex.target_r, "target_pct": round(ex.premium_stop_pct * ex.target_r, 4), "max_hold_minutes": ex.max_hold_minutes,
        "underlying_invalidation": ex.underlying_invalidation, "opposite_signal_exit": ex.opposite_signal_exit, "thesis_exit": ex.thesis_exit,
        "same_bar_stop_and_target": "stop assumed first; flagged", "slippage_ticks": ex.slippage_ticks, "tick_size": ex.tick_size,
        "selection": {k: (list(v) if isinstance(v, tuple) else v) for k, v in sel_cfg.__dict__.items()}, "risk": defn.options.risk.model_dump(),
        "cost_profile": schedule.describe(), "costs_note": "Charges follow the configured fee schedule; options rates are UNVERIFIED defaults. Income tax is never included.",
        "greeks": "provider delta at the quote timestamp; calculated only from a contemporaneous implied volatility and labelled; never invented",
        "lot_size": "from the effective-dated contract master row used at the signal time", "sizing_basis": "realised equity; whole lots; a single lot above the risk budget is rejected",
    }


# ───────────────────────── validation: in/out-of-sample, sweep, walk-forward ─────────────────────────
STOP_GRID = (8.0, 10.0, 12.0, 15.0)
TARGET_GRID = (1.25, 1.5, 2.0)


def _headline(res: dict[str, Any]) -> dict[str, Any]:
    m = res["metrics"]
    return {k: m.get(k) for k in ("total_trades", "win_rate", "net_profit", "return_pct", "profit_factor_net", "max_drawdown_pct", "expectancy_per_trade")}


def run_validation(kind: str, defn: StrategyDefinition, frame: pd.DataFrame, res_full: EvalResult, profile: MarketProfile, source: OptionDataSource, *, initial_capital: Decimal,
                   spot_frame: pd.DataFrame | None = None, split: float = 0.7, train_days: int = 10, test_days: int = 3) -> dict[str, Any]:
    """Premium stop x target research. Parameter choices are made on TRAIN/IN-SAMPLE data only; the grid is never presented as validation."""
    t = frame["time"]
    lo, hi = t.iloc[0], t.iloc[-1] + MINUTE

    def window(a, b) -> dict[str, Any]:
        mask = (t >= a) & (t < b)
        idx = np.flatnonzero(mask.to_numpy())
        sub_sig = {k: v & mask.to_numpy() for k, v in res_full.signals.items()}
        sub = EvalResult(res_full.base_times, res_full.decision_idx, sub_sig, res_full.trees, res_full.atr, res_full.frames, [])
        return {"idx": idx, "res": sub}

    def run(a, b, **ov):
        w = window(a, b)
        return run_options_backtest(defn, frame, w["res"], profile, source, initial_capital=initial_capital, spot_frame=spot_frame, overrides=ov or None) if len(w["idx"]) else None

    def combos():
        return [{"premium_stop_pct": s, "target_r": r} for s in STOP_GRID for r in TARGET_GRID]

    cut = (lo + (hi - lo) * split).floor("D")
    if kind == "is_oos":
        a, b = run(lo, cut), run(cut, hi)
        return {"mode": "in_out_of_sample", "split_at": cut.isoformat(), "in_sample": _headline(a) if a else None, "out_of_sample": _headline(b) if b else None,
                "note": "The stop/target were fixed before this split; both windows use the same definition."}
    if kind == "sensitivity":
        rows = []
        for cmb in combos():
            r = run(lo, cut, **cmb)
            rows.append({"params": cmb, **(_headline(r) if r else {"total_trades": 0})})
        return {"mode": "sensitivity", "window": "in-sample only", "in_sample_end": cut.isoformat(), "parameters": ["premium_stop_pct", "target_r"], "grid": rows,
                "note": "Computed on the in-sample window only. Do not adopt the best cell without out-of-sample confirmation."}
    folds, oos_net, oos_trades = [], D(0), 0
    t0 = lo.floor("D")
    while t0 + pd.Timedelta(days=train_days + test_days) <= hi + pd.Timedelta(days=1):
        t1, t2 = t0 + pd.Timedelta(days=train_days), t0 + pd.Timedelta(days=train_days + test_days)
        best, best_score, best_head = None, None, None
        for cmb in combos():
            r = run(t0, t1, **cmb)
            score = D(str(r["metrics"]["net_profit"])) if r else None
            if score is not None and (best_score is None or score > best_score):
                best, best_score, best_head = cmb, score, _headline(r)
        if best is not None:
            test = run(t1, t2, **best)
            head = _headline(test) if test else {"total_trades": 0, "net_profit": "0.00"}
            oos_trades += int(head["total_trades"] or 0)
            oos_net += D(str(head["net_profit"] or "0"))
            folds.append({"train": [t0.isoformat(), t1.isoformat()], "test": [t1.isoformat(), t2.isoformat()], "chosen_params": best, "train_result": best_head, "test_result": head})
        t0 += pd.Timedelta(days=test_days)
    return {"mode": "walk_forward", "train_days": train_days, "test_days": test_days, "optimised_on": "net_profit (train windows only)", "parameters": ["premium_stop_pct", "target_r"], "folds": folds,
            "oos_total_trades": oos_trades, "oos_net_profit": float(oos_net), "note": "Each test window uses the stop/target chosen only from the preceding train window."}
