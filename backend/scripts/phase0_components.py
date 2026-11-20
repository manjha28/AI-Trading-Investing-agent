"""THE KEY TEST. At ONE barrier cell (stop 0.50%, r=1, H=240) on equity cash (12 symbols, the clean Jan-Jul 2026 window) measure the uplift delivered by every VAR signal component,
INDIVIDUALLY and UNFILTERED: no other component, no regime gate, no risk gate, no cost filter.

Each component is computed with the VAR engine's own functions and thresholds (nothing tuned) at every completed 5-minute bar from 09:30 to 14:45, causally. A component says
"go long", "go short", or (direction-neutral components) "either". Entry = open of the next 1-minute bar; outcome = the same triple-barrier label as Phase 0.
Reported per component: n, win rate, uplift vs the all-entries base rate, E_R_net with a DAY-CLUSTERED standard error, and the existing filter_value comparison (passed vs not passed).
Measurement only: change no thresholds. Writes diagnostics/phase0_components.json."""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from phase0_common import build_sessions, equity_rows, round_trip_cost_fraction  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session as Db  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import barrier_study as bs  # noqa: E402
from app.services import filter_value as fv  # noqa: E402
from app.services.var.backtest import _rvol_hist, instrument_data_from_underlying  # noqa: E402
from app.services.var.config import VarConfig  # noqa: E402
from app.services.var.loader import LOOKBACK_CALENDAR_DAYS, load_equity  # noqa: E402
from app.services.var.strategy import build_ctx, volume_beyond  # noqa: E402
from app.services.var.structure import classify_regime, relative_volume  # noqa: E402
from app.services.var.tpo import initial_balance, tpo_profile  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "diagnostics" / "phase0_components.json"
EQ = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "BHARTIARTL", "ITC", "SBIN", "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR"]
WIN = (date(2026, 1, 1), date(2026, 7, 31))
CELL = (0.005, 1.0, 240)
FIRST_BAR, LAST_BAR = 3, 65                  # completed 5m bars ending 09:30 .. 14:45 (the engine's entry window)
CFG = VarConfig()
DISTS = bs.distinct_distances((CELL[0],), (CELL[1],))
log = lambda *a: print(*a, flush=True)  # noqa: E731

EXCLUDED = {
    "delta / CVD / delta divergence / absorption": "order flow is not stored (no last-traded quantity, bid/ask or depth): cannot be measured",
    "first_target_rr, net_reward_after_costs, stop_size": "trade-geometry / cost feasibility checks, not predictors of direction (they need a stop and target; net_reward is a cost filter)",
    "entry_window, spread, feed_quality, bars_contiguous, not_duplicate": "hygiene / risk-control gates, not signals (time of day is covered by the Phase 0 time-of-day table)",
    "retest and trigger-candle structure": "need a reference level and a pending setup; the setup pieces that carry information (acceptance, sweep, failed auction, bar direction) are tested separately",
}


def f_of(price: float) -> float:
    return round_trip_cost_fraction("equity_cash", "cash", price, date(2026, 7, 15), notional=1_000_000.0)[0] / CELL[0]


def session_features(ctx, i_list) -> dict[str, np.ndarray]:
    """Direction code per component for bar indices in i_list: +1 long, -1 short, 0 none, 2 either direction."""
    n = len(ctx.bars)
    bars = ctx.bars
    cl = np.array([float(b.close) for b in bars]); op = np.array([float(b.open) for b in bars])
    hi = np.array([float(b.high) for b in bars]); lo = np.array([float(b.low) for b in bars])
    tick = float(ctx.tick)
    ps, pp, cs, cp = ctx.prev_stats, ctx.prev_prof, ctx.comp_stats, ctx.comp_prof
    pVAH = float(pp.price(ps.vah)) if ps and pp else None
    pVAL = float(pp.price(ps.val)) if ps and pp else None
    pPOC = float(pp.price(ps.poc)) if ps and pp else None
    cVAH = float(cp.price(cs.vah)) if cs and cp else None
    cVAL = float(cp.price(cs.val)) if cs and cp else None
    cPOC = float(cp.price(cs.poc)) if cs and cp else None
    ptraj = ctx.poc_traj()
    names = ["regime_trend", "regime_balanced_toward_prev_POC", "vwap_side", "vwap_slope", "vwap_deviation_momentum_terciles", "vwap_deviation_fade_terciles", "relative_volume_ge_1.2",
             "relative_volume_ge_1.0", "acceptance_2_closes", "acceptance_2_closes_plus_25pct_volume", "sweep_extension_window", "failed_auction", "prev_value_trend_above_VAH_below_VAL",
             "inside_prev_value_toward_prev_POC", "developing_POC_position", "developing_value_position", "initial_balance_breakout", "composite_POC_position", "composite_value_position",
             "tpo_poc_migration", "bar_direction"]
    out = {k: np.zeros(n, dtype=np.int8) for k in names}
    dev = np.full(n, np.nan)
    tpo_poc: dict[int, int | None] = {}
    sess_open = bars[0].ts.replace(hour=9, minute=15)
    for i in i_list:
        atr, vw = ctx.atr[i], ctx.vwap[i]
        reg = classify_regime(bars, i, ctx.vwap, ctx.atr, ps, pp, ptraj, CFG.regime, True)
        c = cl[i]
        if reg.name == "BULLISH_TREND":
            out["regime_trend"][i] = 1
        elif reg.name == "BEARISH_TREND":
            out["regime_trend"][i] = -1
        if reg.name == "BALANCED" and pPOC is not None and pPOC != c:
            out["regime_balanced_toward_prev_POC"][i] = 1 if pPOC > c else -1
        if vw is not None:
            out["vwap_side"][i] = 1 if c > vw else (-1 if c < vw else 0)
            if atr:
                dev[i] = (c - vw) / atr
        if reg.slope is not None:
            out["vwap_slope"][i] = 1 if reg.slope >= CFG.regime.slope_threshold else (-1 if reg.slope <= -CFG.regime.slope_threshold else 0)
        rv = relative_volume(bars[i], ctx.rvol_hist, CFG.breakout.rvol_sessions, None)
        if rv is not None:
            out["relative_volume_ge_1.2"][i] = 2 if rv >= CFG.breakout.relative_volume_minimum else 0
            out["relative_volume_ge_1.0"][i] = 2 if rv >= CFG.reversal.relative_volume_minimum else 0
        # acceptance: two consecutive closes beyond a reference level (previous VAH/VAL, completed Initial Balance, composite VAH/VAL)
        ib = initial_balance(bars[: i + 1], bars[i].end, minutes=CFG.market_structure.initial_balance_minutes)
        ib_ok = ib.complete and i >= 1 and bars[i - 1].end >= sess_open + timedelta(minutes=CFG.market_structure.initial_balance_minutes)
        up_refs = [x for x in (pVAH, float(ib.high) if ib_ok and ib.high is not None else None, cVAH) if x is not None]
        dn_refs = [x for x in (pVAL, float(ib.low) if ib_ok and ib.low is not None else None, cVAL) if x is not None]
        acc = 0; acc_vol = 0
        if i >= 1:
            for side, refs in ((1, up_refs), (-1, dn_refs)):
                for ref in refs:
                    ok2 = (cl[i] >= ref + tick and cl[i - 1] >= ref + tick) if side > 0 else (cl[i] <= ref - tick and cl[i - 1] <= ref - tick)
                    if ok2:
                        acc = side if acc in (0, side) else 99
                        from decimal import Decimal as Dc
                        tot = sum((b.volume for b in bars[i - 1: i + 1]), Dc(0))
                        outv = sum((volume_beyond(b, Dc(str(ref)), side, ctx.tick) for b in bars[i - 1: i + 1]), Dc(0))
                        if tot > 0 and float(outv / tot * 100) >= CFG.breakout.minimum_outside_volume_pct:
                            acc_vol = side if acc_vol in (0, side) else 99
        out["acceptance_2_closes"][i] = acc if acc != 99 else 0
        out["acceptance_2_closes_plus_25pct_volume"][i] = acc_vol if acc_vol != 99 else 0
        # sweep / failed auction at the previous value-area edges (probe size window from the engine: 0.10-0.75 ATR) over the last 4 bars
        if atr and pVAH is not None and pVAL is not None and i >= 3:
            e_up = float(hi[i - 3: i + 1].max()) - pVAH
            e_dn = pVAL - float(lo[i - 3: i + 1].min())
            up = CFG.reversal.minimum_sweep_atr * atr <= e_up <= CFG.reversal.maximum_sweep_atr * atr
            dn = CFG.reversal.minimum_sweep_atr * atr <= e_dn <= CFG.reversal.maximum_sweep_atr * atr
            if up != dn:
                out["sweep_extension_window"][i] = -1 if up else 1
                if (up and c < pVAH) or (dn and c > pVAL):
                    out["failed_auction"][i] = -1 if up else 1
        if pVAH is not None and pVAL is not None:
            out["prev_value_trend_above_VAH_below_VAL"][i] = 1 if c > pVAH else (-1 if c < pVAL else 0)
            if pVAL <= c <= pVAH and pPOC is not None and pPOC != c:
                out["inside_prev_value_toward_prev_POC"][i] = 1 if pPOC > c else -1
        ds = ctx.dev_stats[i]
        if ds is not None:
            dpoc, dvah, dval = float(ctx.tick) * ds.poc, float(ctx.tick) * ds.vah, float(ctx.tick) * ds.val
            out["developing_POC_position"][i] = 1 if c > dpoc else (-1 if c < dpoc else 0)
            out["developing_value_position"][i] = 1 if c > dvah else (-1 if c < dval else 0)
        if ib_ok and ib.high is not None and ib.low is not None:
            out["initial_balance_breakout"][i] = 1 if c > float(ib.high) else (-1 if c < float(ib.low) else 0)
        if cPOC is not None:
            out["composite_POC_position"][i] = 1 if c > cPOC else (-1 if c < cPOC else 0)
        if cVAH is not None and cVAL is not None:
            out["composite_value_position"][i] = 1 if c > cVAH else (-1 if c < cVAL else 0)
        # TPO: developing time-based POC migration over six bars (the engine's tpo_support rule)
        for j in (i, i - 6):
            if j >= 0 and j not in tpo_poc:
                tpo_poc[j] = tpo_profile(bars[: j + 1], ctx.tick, bracket_minutes=CFG.market_structure.tpo_bracket_minutes, pct=CFG.market_structure.value_area_pct).poc
        if i >= 6 and tpo_poc.get(i) is not None and tpo_poc.get(i - 6) is not None and tpo_poc[i] != tpo_poc[i - 6]:
            out["tpo_poc_migration"][i] = 1 if tpo_poc[i] > tpo_poc[i - 6] else -1
        out["bar_direction"][i] = 1 if c > op[i] else (-1 if c < op[i] else 0)
    out["_dev"] = dev
    return out


def main() -> None:
    eng = create_engine(settings.database_url)
    rows_all: list[tuple] = []        # (symbol_idx, day_ordinal, bar_i, dir, win, loss, timeout, r_net, t_min)
    feats: dict[str, list] = defaultdict(list)       # component -> list of codes aligned with (symbol, day, bar) entries
    keys: list[tuple] = []
    dev_all: list[float] = []
    f_by_sym: dict[str, float] = {}
    with Db(eng) as db:
        for si, sym in enumerate(EQ):
            loaded = load_equity(db, sym, WIN[0] - timedelta(days=LOOKBACK_CALENDAR_DAYS), WIN[1])
            x = instrument_data_from_underlying(loaded)
            sessions, _ = build_sessions(equity_rows(db, sym, *WIN))
            by_day = {s.day: s for s in sessions}
            price = float(np.nanmedian(np.concatenate([s.c for s in sessions])))
            f = f_of(price)
            f_by_sym[sym] = f
            all_days = sorted(x.sessions)
            used = 0
            for day in all_days:
                if day < WIN[0] or day > WIN[1] or day not in by_day:
                    continue
                contract = x.day_contract.get(day, x.contract)
                traded_prior = [x.sessions[d] for d in all_days if d < day]
                own = x.same_contract_sessions.get(contract.instrument_id, x.sessions)
                own_days = [d for d in sorted(own) if d < day]
                prior = [own[d] for d in own_days]
                if not prior or (day - own_days[-1]).days > 5:
                    continue
                bars = x.sessions[day]
                if len(bars) < LAST_BAR + 2:
                    continue
                warm = [b for s in prior[-2:] for b in s][-(CFG.market_structure.atr_period * 3):]
                ctx = build_ctx(contract, CFG, bars, warm, prior[-1], prior[-CFG.market_structure.composite_sessions:], _rvol_hist(traded_prior, CFG.breakout.rvol_sessions), allow_unknown_spread=True)
                idx = list(range(FIRST_BAR, LAST_BAR + 1))
                comp = session_features(ctx, idx)
                cat = bs.concat_sessions([bs.SessionArrays(by_day[day], DISTS, horizons=(CELL[2],))], DISTS, (CELL[2],))
                lab = {d: bs.label_cell(cat, *CELL, d) for d in (1, -1)}
                for i in idx:
                    e = (i + 1) * 5
                    if e >= bs.SLOTS or not cat["entry_ok"][e]:
                        continue
                    keys.append((si, day.toordinal(), i, e))
                    dev_all.append(float(comp["_dev"][i]))
                    for name, arr in comp.items():
                        if name != "_dev":
                            feats[name].append(int(arr[i]))
                    for d in (1, -1):
                        w, l, t, rto = (lab[d][0][e], lab[d][1][e], lab[d][2][e], lab[d][3][e])
                        R = CELL[1] if w else (-1.0 if l else float(np.nan_to_num(rto)))
                        rows_all.append((si, day.toordinal(), i, d, 1 if w else 0, 1 if l else 0, 1 if t else 0, R - f, (day.toordinal() * 1440.0 + e)))
                used += 1
            log(f"  {sym}: {used} sessions, entries so far {len(keys):,}")
    # ---------------------------------------------------------------- assemble
    E = np.array(keys)
    nE = len(keys)
    rows = np.array(rows_all)               # two rows per entry: long then short
    R_net = rows[:, 7].reshape(nE, 2); W = rows[:, 4].reshape(nE, 2); Lo = rows[:, 5].reshape(nE, 2); T = rows[:, 6].reshape(nE, 2)
    day = E[:, 1]; sym_i = E[:, 0]; tmin = rows[:, 8].reshape(nE, 2)[:, 0]
    dev = np.array(dev_all)
    q33, q67 = np.nanpercentile(dev, [33.333, 66.667])
    codes = {k: np.array(v, dtype=np.int8) for k, v in feats.items()}
    codes["vwap_deviation_momentum_terciles"] = np.where(dev >= q67, 1, np.where(dev <= q33, -1, 0)).astype(np.int8)
    codes["vwap_deviation_fade_terciles"] = -codes["vwap_deviation_momentum_terciles"]
    f_mean = float(np.mean(list(f_by_sym.values())))

    def mask_dir(code: np.ndarray) -> np.ndarray:
        """(nE, 2) boolean: [long active, short active]"""
        return np.stack([(code == 1) | (code == 2), (code == -1) | (code == 2)], axis=1)

    def stats(sel: np.ndarray) -> dict:
        n = int(sel.sum())
        if n == 0:
            return {"n": 0}
        w, l, t, r = W[sel], Lo[sel], T[sel], R_net[sel]
        dd = np.repeat(day[:, None], 2, axis=1)[sel]
        n_long = int(sel[:, 0].sum()); n_short = int(sel[:, 1].sum())
        pw, pl, pt = w.mean(), l.mean(), t.mean()
        m = float(r.mean())
        by = defaultdict(float); cnt = defaultdict(int)
        for d_, x_ in zip(dd, r - m):
            by[d_] += x_; cnt[d_] += 1
        G = len(by)
        se = math.sqrt(sum(v * v for v in by.values()) * G / (G - 1)) / n if G > 1 else float("nan")
        r_to = float(((r + f_mean)[t == 1]).mean()) if t.sum() else 0.0
        return {"n": n, "n_long": n_long, "n_short": n_short, "days": G, "p_win": float(pw), "p_loss": float(pl), "p_timeout": float(pt), "p_win_resolved": float(pw / (pw + pl)) if pw + pl else None,
                "r_timeout_mean": r_to, "E_R_net": m, "E_R_net_se_day_clustered": se, "p_breakeven": bs.p_breakeven(CELL[1], f_mean, float(pt), r_to)}

    allsel = np.ones((nE, 2), bool)
    base = stats(allsel)
    base_long = stats(np.stack([np.ones(nE, bool), np.zeros(nE, bool)], axis=1))
    base_short = stats(np.stack([np.zeros(nE, bool), np.ones(nE, bool)], axis=1))
    log(f"\nENTRIES: {nE:,} five-minute bar ends x 2 directions ({int(base['n']):,} labelled samples) over {len(set(day.tolist()))} days, {len(EQ)} symbols; cell stop {CELL[0] * 100:.2f}% r={CELL[1]:g} H={CELL[2]}; cost f={f_mean:.4f} R")
    log(f"BASE (all entries, both directions): p_win {base['p_win']:.4f}  p_loss {base['p_loss']:.4f}  p_timeout {base['p_timeout']:.4f}  resolved win rate {base['p_win_resolved']:.4f}  E_R_net {base['E_R_net']:+.4f} ± {base['E_R_net_se_day_clustered']:.4f}")
    log(f"      long-only: p_win {base_long['p_win']:.4f} (resolved {base_long['p_win_resolved']:.4f}) E_R_net {base_long['E_R_net']:+.4f}   short-only: p_win {base_short['p_win']:.4f} (resolved {base_short['p_win_resolved']:.4f}) E_R_net {base_short['E_R_net']:+.4f}")
    log(f"      break-even: p_win {base['p_breakeven']:.4f} unconditional = resolved {base['p_breakeven'] / (1 - base['p_timeout']):.4f} (timeouts still pay cost); '54.0%' = (1+f)/2 = {(1 + f_mean) / 2:.4f} if timeouts were free")
    results = []
    thin_records_cache = None
    for name, code in codes.items():
        sel = mask_dir(code)
        s = stats(sel)
        if s["n"] < 50:
            results.append({"component": name, **s})
            continue
        # base matched to the component's own long/short mix
        mix_p = (s["n_long"] * base_long["p_win"] + s["n_short"] * base_short["p_win"]) / (s["n_long"] + s["n_short"])
        mix_e = (s["n_long"] * base_long["E_R_net"] + s["n_short"] * base_short["E_R_net"]) / (s["n_long"] + s["n_short"])
        row = {"component": name, **s, "uplift_vs_base": s["p_win"] / base["p_win"], "uplift_vs_direction_matched_base": s["p_win"] / mix_p,
               "uplift_resolved_vs_base": (s["p_win_resolved"] / base["p_win_resolved"]) if s["p_win_resolved"] else None,
               "p_win_minus_breakeven_pp": (s["p_win"] - s["p_breakeven"]) * 100,
               "E_R_net_minus_matched_base": s["E_R_net"] - mix_e, "t_vs_matched_base": ((s["E_R_net"] - mix_e) / s["E_R_net_se_day_clustered"]) if s["E_R_net_se_day_clustered"] else None,
               "t_E_R_net_vs_zero": (s["E_R_net"] / s["E_R_net_se_day_clustered"]) if s["E_R_net_se_day_clustered"] else None}
        # the existing filter_value service: entries where the component fires (passed) vs entries where it does not (rejected)
        recs = []
        for k in range(nE):
            for dcol in (0, 1):
                active = bool(sel[k, dcol])
                recs.append({"states": {name: "pass" if active else "fail"}, "instrument": EQ[sym_i[k]], "t_min": float(tmin[k]), "r_net": float(R_net[k, dcol]), "win": float(W[k, dcol])})
        fvr = fv.filter_value(recs, [name], min_gap_min=float(CELL[2]))[0]
        row["filter_value"] = {k: fvr[k] for k in ("n_passed", "n_rejected", "E_R_net_passed", "E_R_net_rejected", "E_R_net_diff", "E_R_net_se", "E_R_net_t", "p_win_passed", "p_win_rejected", "verdict")}
        results.append(row)
    _ = thin_records_cache
    ranked = sorted([r for r in results if r.get("n", 0) >= 50], key=lambda r: -r["uplift_vs_base"])
    log(f"\nCOMPONENTS RANKED BY UPLIFT (p_win / base p_win {base['p_win']:.4f}); break-even uplift for the base mix = {base['p_breakeven'] / base['p_win']:.3f}")
    log(f"{'component':<42}{'n':>9}{'days':>5}{'p_win':>7}{'resolved':>9}{'p_to':>6}{'uplift':>8}{'matched':>8}{'p_BE':>7}{'gap pp':>8}{'E_R_net':>9}{'± day-SE':>9}{'t vs 0':>7}{'t vs base':>10}  filter_value (thinned pass/rej: diff, t)")
    for r in ranked:
        fvx = r["filter_value"]
        log(f"{r['component']:<42}{r['n']:>9,}{r['days']:>5}{r['p_win']:>7.3f}{r['p_win_resolved']:>9.3f}{r['p_timeout']:>6.2f}{r['uplift_vs_base']:>8.3f}{r['uplift_vs_direction_matched_base']:>8.3f}{r['p_breakeven']:>7.3f}"
            f"{r['p_win_minus_breakeven_pp']:>+8.2f}{r['E_R_net']:>+9.4f}{r['E_R_net_se_day_clustered']:>9.4f}{r['t_E_R_net_vs_zero']:>+7.1f}{r['t_vs_matched_base']:>+10.2f}  "
            f"{fvx['n_passed']}/{fvx['n_rejected']}: {('—' if fvx['E_R_net_diff'] is None else f'{fvx['E_R_net_diff']:+.3f}')}, t={('—' if fvx['E_R_net_t'] is None else f'{fvx['E_R_net_t']:+.2f}')} {fvx['verdict']}")
    best = ranked[0]
    bestE = max(ranked, key=lambda r: r["E_R_net"])
    log(f"\nBEST BY UPLIFT: {best['component']}  uplift {best['uplift_vs_base']:.3f} (needs {best['p_breakeven'] / base['p_win']:.3f} to break even)  p_win {best['p_win']:.3f} vs its own break-even {best['p_breakeven']:.3f}  E_R_net {best['E_R_net']:+.4f}")
    log(f"BEST BY E_R_net: {bestE['component']}  E_R_net {bestE['E_R_net']:+.4f} ± {bestE['E_R_net_se_day_clustered']:.4f} (t vs zero {bestE['t_E_R_net_vs_zero']:+.1f})")
    log(f"components with E_R_net > 0: {sum(1 for r in ranked if r['E_R_net'] > 0)} of {len(ranked)}; components with p_win >= own break-even: {sum(1 for r in ranked if r['p_win'] >= r['p_breakeven'])}")
    log("\nNOT MEASURABLE (and why):")
    for k, v in EXCLUDED.items():
        log(f"  - {k}: {v}")
    OUT.write_text(json.dumps({"cell": {"s_pct": CELL[0] * 100, "r": CELL[1], "H": CELL[2]}, "f_cost_R": f_mean, "base": base, "base_long": base_long, "base_short": base_short, "terciles": [float(q33), float(q67)],
                               "results": ranked, "too_few": [r for r in results if r.get("n", 0) < 50], "not_measurable": EXCLUDED, "window": [str(WIN[0]), str(WIN[1])], "symbols": EQ}, indent=1, default=str))
    log(f"saved {OUT}")


if __name__ == "__main__":
    main()
