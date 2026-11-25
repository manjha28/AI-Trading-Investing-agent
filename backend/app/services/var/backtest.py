"""Deterministic replay backtester over completed bars. Hypothetical results; never mixed with real P&L.

No look-ahead: at bar i only bars[:i+1] feed signals; orders fill from bar i+1; structural exits use bars <= i-1.
Missing prerequisites (ATR warm-up, prior-session profile, time-of-day volume history) suppress signals rather than
substituting present-day or default values."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.services import indian_charges as ic

from . import execution as ex
from .config import VarConfig
from .metrics import segment, summarize
from .risk import DayState, can_enter, size_position
from .strategy import EngineState, Signal, build_ctx, evaluate
from .structure import slot_key
from .vtypes import IST, Bar, Contract, D


@dataclass
class InstrumentData:
    contract: Contract
    sessions: dict[date, list[Bar]]          # completed 5m bars per session, chronological
    expiring_index_days: frozenset[date] = frozenset()
    excluded_days: dict[date, str] = field(default_factory=dict)   # e.g. scheduled results, F&O ban
    day_contract: dict[date, Contract] = field(default_factory=dict)            # contract actually traded each session (rollover)
    same_contract_sessions: dict[str, dict[date, list[Bar]]] = field(default_factory=dict)   # contract id -> its own sessions


@dataclass
class BacktestResult:
    config: dict
    window: dict
    trades: list[dict]
    signals: list[dict]
    rejections: list[dict]
    summary: dict
    by_setup: dict
    by_instrument: dict
    by_direction: dict
    by_data_level: dict
    assumptions: dict
    hypothetical: bool = True
    skipped_sessions: list[dict] = field(default_factory=list)
    stage_counts: dict = field(default_factory=dict)


def _rvol_hist(prior: list[list[Bar]], n: int) -> dict[time, list[Decimal]]:
    h: dict[time, list[Decimal]] = {}
    for s in prior[-n:]:
        for b in s:
            h.setdefault(slot_key(b.ts), []).append(b.volume)
    return h


def _struct_exit(pos: ex.Position, bars: list[Bar], cvd: list[float | None], k: int, cfg: VarConfig) -> bool:
    """Decided from bars <= k-1 only (acts at bar k's open)."""
    if k < 2 or pos.bars_held < 1:
        return False
    ref = pos.signal.reference_level
    a, b = bars[k - 2], bars[k - 1]
    d = pos.direction
    if pos.signal.setup == "ACCEPTANCE_BREAKOUT":
        thr = (a.close < ref and b.close < ref) if d > 0 else (a.close > ref and b.close > ref)
        if not thr:
            return False
        if cfg.uses_orderflow:
            c0, c1 = cvd[k - 2], cvd[k - 1]
            return c0 is not None and c1 is not None and ((c1 < c0) if d > 0 else (c1 > c0))
        return True
    return (a.close > ref and b.close > ref) if d < 0 else (a.close < ref and b.close < ref)


def run_backtest(data: list[InstrumentData], cfg: VarConfig, initial_capital: Decimal, *, start: date | None = None,
                 end: date | None = None, allow_unknown_spread: bool = True) -> BacktestResult:
    days = sorted({d for x in data for d in x.sessions})
    if start: days = [d for d in days if d >= start]
    if end: days = [d for d in days if d <= end]
    equity = initial_capital
    trades: list[dict] = []
    sigs_out: list[dict] = []
    rej_out: list[dict] = []
    skipped: list[dict] = []
    stage_counts: dict[tuple, int] = {}
    bar_count = 0
    for day in days:
        day_state = DayState(start_equity=equity)
        ctxs: dict[str, tuple] = {}
        for x in data:
            bars = x.sessions.get(day)
            if not bars:
                continue
            contract = x.day_contract.get(day, x.contract)
            all_days = sorted(x.sessions)
            traded_prior = [x.sessions[d] for d in all_days if d < day]          # traded series: used only for time-of-day volume baseline
            own = x.same_contract_sessions.get(contract.instrument_id, x.sessions)  # profiles NEVER mix expiries
            own_days = [d for d in sorted(own) if d < day]
            prior = [own[d] for d in own_days]
            if not prior:
                skipped.append({"instrument": contract.symbol, "date": day.isoformat(), "reason": "no prior session of the same contract (profile unavailable)"})
                continue
            if (day - own_days[-1]).days > 5:
                skipped.append({"instrument": contract.symbol, "date": day.isoformat(), "reason": "prior session of the same contract missing (gap > 5 days)"})
                continue
            warm = [b for s in prior[-2:] for b in s][-(cfg.market_structure.atr_period * 3):]
            ctx = build_ctx(contract, cfg, bars, warm, prior[-1], prior[-cfg.market_structure.composite_sessions:],
                            _rvol_hist(traded_prior, cfg.breakout.rvol_sessions), allow_unknown_spread=allow_unknown_spread,
                            excluded_reason=x.excluded_days.get(day), is_expiring_index=day in x.expiring_index_days)
            bar_count += len(bars)
            ctxs[contract.symbol] = (x, ctx, EngineState())
        stamps = sorted({b.ts for (_x, c, _s) in ctxs.values() for b in c.bars})
        open_pos: dict[str, ex.Position] = {}
        pending: dict[str, tuple[Signal, ex.Position | None, int]] = {}
        idx = {sym: {b.ts: k for k, b in enumerate(c.bars)} for sym, (_x, c, _s) in ctxs.items()}

        def close_out(sym: str, pos: ex.Position) -> None:
            nonlocal equity
            res = ex.finalize(pos, cfg)
            res.update({"instrument": sym, "setup": pos.signal.setup, "direction": pos.signal.direction, "date": day.isoformat(),
                        "regime": pos.signal.regime, "data_level": pos.signal.data_level, "delta_quality": pos.signal.delta_quality,
                        "variant": cfg.variant, "exit_model": cfg.exits.exit_model, "entry_variant": cfg.execution.entry_variant,
                        "reference": f"{pos.signal.reference_source} {pos.signal.reference_level}", "derivation": pos.signal.derivation,
                        "conditions": [(c.name, c.state.value, c.detail) for c in pos.signal.conditions], "strategy_version": cfg.version,
                        "hypothetical": True})
            trades.append(res)
            day_state.realized_net += res["net_pnl"]
            day_state.open_risk -= res["initial_risk"]
            day_state.open_positions -= 1
            if pos.contract.kind == "index":
                day_state.open_index_positions -= 1
            day_state.consecutive_losses = day_state.consecutive_losses + 1 if res["net_pnl"] <= 0 else 0
            if res["exit_reason"].startswith("stop"):
                day_state.last_stop_out[pos.contract.instrument_id] = pos.exit_ts  # type: ignore[assignment]
            del open_pos[sym]

        for ts in stamps:
            # 1) pending entries from the previous bar fill (or die) on this bar
            for sym in list(pending):
                x, c, st = ctxs[sym]
                k = idx[sym].get(ts)
                if k is None:
                    continue
                sig, _p, lots = pending.pop(sym)
                if day_state.locked:
                    rej_out.append({"instrument": sym, "setup": sig.setup, "direction": sig.direction, "bar_end": sig.trigger_bar_end.isoformat(), "stage": "execution",
                                    "reasons": ["day_locked_before_fill: daily loss limit reached while the order was pending"]})
                    continue
                pos = ex.simulate_entry(sig, c.contract, c.bars[k], lots, cfg)
                if pos is None:
                    rej_out.append({"instrument": sym, "setup": sig.setup, "direction": sig.direction, "bar_end": sig.trigger_bar_end.isoformat(), "stage": "execution",
                                    "reasons": ["entry_not_filled_or_gap_invalidated: stop-entry not touched on the next bar, or the gap broke the min R:R"]})
                    continue
                pos.expiring_index = c.is_expiring_index
                open_pos[sym] = pos
                day_state.entries += 1
                day_state.open_positions += 1
                day_state.open_risk += pos.risk_per_unit * pos.lots * pos.lot_size
                if c.contract.kind == "index":
                    day_state.open_index_positions += 1
                ex.manage(pos, c.bars, k, cfg, adverse_only=True)
                if pos.closed:
                    close_out(sym, pos)
            # 2) manage open positions
            for sym in list(open_pos):
                x, c, st = ctxs[sym]
                k = idx[sym].get(ts)
                pos = open_pos[sym]
                if k is None or pos.entry_ts >= ts:
                    continue
                ex.manage(pos, c.bars, k, cfg, struct_exit=_struct_exit(pos, c.bars, c.cvd, k, cfg))
                if pos.closed:
                    close_out(sym, pos)
            # 3) daily risk lock (realized + gross unrealized at bar close)
            unreal = Decimal(0)
            for sym, pos in open_pos.items():
                k = idx[sym].get(ts)
                if k is not None:
                    unreal += (ctxs[sym][1].bars[k].close - pos.entry) * pos.direction * pos.lots * pos.lot_size
            cap = day_state.start_equity * D(cfg.risk.max_daily_loss_pct) / 100
            if not day_state.locked and day_state.realized_net + unreal <= -cap:
                day_state.locked = "daily loss limit reached"
            # 4) new signals on completed bar
            for sym, (x, c, st) in ctxs.items():
                k = idx[sym].get(ts)
                if k is None:
                    continue
                sigs, rejs, _reg = evaluate(c, k, st)
                for r in rejs:
                    rej_out.append({"instrument": sym, "setup": r.setup, "direction": r.direction, "bar_end": r.bar_end.isoformat(), "stage": "strategy", "reasons": r.reasons,
                                    "failed": [(c.name, c.state.value) for c in r.conditions if not c.ok()], "conditions": [(c.name, c.state.value) for c in r.conditions]})
                for s in sigs:
                    entry_ts = c.bars[k].end
                    sigs_out.append({"instrument": sym, "setup": s.setup, "direction": s.direction, "bar_end": entry_ts.isoformat(), "entry": str(s.entry),
                                     "stop": str(s.stop), "targets": [str(t) for t in s.targets], "rr": round(s.risk_reward, 2)})
                    if sym in open_pos or sym in pending:
                        rej_out.append({"instrument": sym, "setup": s.setup, "direction": s.direction, "bar_end": entry_ts.isoformat(), "stage": "risk", "reasons": ["duplicate_position_or_order_for_instrument"]})
                        continue
                    sz = size_position(day_state.start_equity, s.entry, s.stop, s.targets[0], s.direction, c.contract, cfg, c.bars[k].ts.astimezone(IST).date())
                    why = can_enter(day_state, sz, c.contract, entry_ts, cfg, unreal)
                    pend_risk = sum(1 for _ in pending)
                    if day_state.open_positions + pend_risk >= cfg.risk.max_simultaneous_positions:
                        why.append("pending orders would exceed simultaneous positions")
                    if why:
                        rej_out.append({"instrument": sym, "setup": s.setup, "direction": s.direction, "bar_end": entry_ts.isoformat(), "stage": "risk", "reasons": why})
                        continue
                    pending[sym] = (s, None, sz.lots)
        for _x, _c, st in ctxs.values():
            for stg in st.stages:
                stage_counts[(stg[0], stg[1], stg[2])] = stage_counts.get((stg[0], stg[1], stg[2]), 0) + 1
        # forced flat at the end of data for the day (no overnight)
        for sym in list(open_pos):
            x, c, st = ctxs[sym]
            pos = open_pos[sym]
            last = c.bars[-1]
            ex._close(pos, last.close - pos.direction * c.contract.tick_size * cfg.execution.exit_slippage_ticks, pos.remaining_lots, "end_of_data_flat", last.end)
            close_out(sym, pos)
        equity = initial_capital + sum((t["net_pnl"] for t in trades), Decimal(0))
    levels = {t["data_level"] for t in trades}
    return BacktestResult(
        config=cfg.model_dump(mode="json"),
        window={"start": days[0].isoformat() if days else None, "end": days[-1].isoformat() if days else None, "sessions": len(days),
                "bars": bar_count, "source": "stored 5m bars supplied by the caller", "data_levels": sorted(levels)},
        trades=trades, signals=sigs_out, rejections=rej_out,
        summary=summarize(trades, initial_capital),
        by_setup=segment(trades, lambda t: t["setup"], initial_capital),
        by_instrument=segment(trades, lambda t: t["instrument"], initial_capital),
        by_direction=segment(trades, lambda t: t["direction"], initial_capital),
        by_data_level=segment(trades, lambda t: t["data_level"], initial_capital),
        assumptions={"entry_slippage_ticks": cfg.execution.entry_slippage_ticks, "exit_slippage_ticks": cfg.execution.exit_slippage_ticks,
                     "stop_slippage_ticks": cfg.execution.stop_slippage_ticks, "stop_target_conflict": "stop assumed first",
                     "candle_fill_model": "approximate", "fee_schedule_version": ic.SCHEDULE_VERSION, "broker_profile": cfg.charges.broker_profile, "exchange": cfg.charges.exchange,
                     "cost_rates_verified": all(t.get("costs_verified") for t in trades) if trades else None, "charge_rounding": ic.ROUNDING_METHOD,
                     "unknown_spread_allowed": allow_unknown_spread,
                     "label": "HYPOTHETICAL: decision support, not investment advice; never combine with real portfolio P&L"},
        skipped_sessions=skipped, stage_counts={f"{a}:{b}:{'long_or_up' if c > 0 else 'short_or_down'}": v for (a, b, c), v in sorted(stage_counts.items())})


def chronological_splits(days: list[date], train: float = 0.5, validation: float = 0.25) -> dict[str, tuple[date, date]]:
    """Chronological train / validation / out-of-sample windows (no shuffling). Freeze config after train."""
    days = sorted(days)
    n = len(days)
    a, b = int(n * train), int(n * (train + validation))
    if a < 1 or b <= a or b >= n:
        raise ValueError("not enough sessions for three non-empty windows")
    return {"train": (days[0], days[a - 1]), "validation": (days[a], days[b - 1]), "out_of_sample": (days[b], days[-1])}


def instrument_data_from_underlying(u, min_prior_sessions: int = 1) -> InstrumentData:
    """Adapt loader.UnderlyingData: each session trades the contract chosen causally by ``select_series_for_day``."""
    from .loader import select_series_for_day
    days = sorted({d for cs in u.series for d in cs.sessions})
    sessions: dict[date, list[Bar]] = {}
    day_contract: dict[date, Contract] = {}
    expiring: set[date] = set()
    prior_day: date | None = None
    for day in days:
        cs = select_series_for_day(u, day, prior_day)
        prior_day = day
        if cs is None:
            continue
        sessions[day] = cs.sessions[day]
        day_contract[day] = cs.contract
        if u.kind == "index" and cs.expiry == day:
            expiring.add(day)
    any_contract = next(iter(day_contract.values()), u.series[0].contract)
    return InstrumentData(any_contract, sessions, frozenset(expiring), {}, day_contract,
                          {cs.contract.instrument_id: cs.sessions for cs in u.series})
