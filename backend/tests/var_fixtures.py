"""TEST FIXTURE DATA ONLY. Deterministic synthetic sessions for VAR unit tests; never imported by runtime code."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from app.services.var.config import VarConfig
from app.services.var.profile import profile_from_bars
from app.services.var.strategy import EngineState, build_ctx, evaluate
from app.services.var.vtypes import IST, Bar, BarFlow, Contract, DeltaQuality

TICK = D("0.05")
CONTRACT = Contract("fx-nifty", "NIFTY-FUT-TEST", "NIFTY", "index", None, 75, TICK)


def day_open(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST)


def mkbar(d, k, o, h, l, c, v=1500, flow=None):
    return Bar(day_open(d) + timedelta(minutes=5 * k), D(str(o)), D(str(h)), D(str(l)), D(str(c)), D(str(v)), flow)


def prev_session(d, base=24000, n=75, vol=1000):
    """Heavy cluster low (POC), then a drift up to a distinct VAH well above the POC."""
    bars = []
    for k in range(n):
        centre = base - 100 + (k % 4) * 2 if k < 45 else base - 100 + (k - 44) * 3.5
        bars.append(mkbar(d, k, centre, centre + 6, centre - 6, centre + 1, vol))
    return bars


def prior_sessions(count=21, start=date(2026, 1, 1)):
    out, d = [], start
    while len(out) < count:
        if d.weekday() < 5:
            out.append((d, prev_session(d)))
        d += timedelta(days=1)
    return out


def rvol_hist(sessions, vol=1000):
    from app.services.var.structure import slot_key
    h = {}
    for _d, bars in sessions[-20:]:
        for b in bars:
            h.setdefault(slot_key(b.ts), []).append(D(vol))
    return h


def flow(buy, sell, unc=0, q=DeltaQuality.TICK_ESTIMATED):
    return BarFlow(D(buy), D(sell), D(unc), q)


def make_ctx(cur_bars, cfg, *, hist_vol=1000, composite=False, **kw):
    sess = prior_sessions()
    prev_d, prev = sess[-1]
    comp = [b for _d, b in sess[-5:]] if composite else []
    return build_ctx(CONTRACT, cfg, cur_bars, prev[-60:], prev, comp, rvol_hist(sess, hist_vol), allow_unknown_spread=True, **kw)


def run_all(ctx, cfg=None):
    """Evaluate every bar causally; returns (signals, rejections)."""
    st = EngineState()
    sigs, rejs = [], []
    for i in range(len(ctx.bars)):
        s, r, _ = evaluate(ctx, i, st)
        sigs += [(i, x) for x in s]
        rejs += [(i, x) for x in r]
    return sigs, rejs, st


def ref_levels(sess=None):
    from app.services.var.profile import profile_from_bars
    sess = sess or prior_sessions()
    p = profile_from_bars(sess[-1][1], TICK)
    s = p.stats(70)
    return p.price(s.vah), p.price(s.val), p.price(s.poc)


def breakout_long_bars(d, flows=False, acceptance2=True, retest_ok=True):
    R, _val, _poc = ref_levels()
    r = lambda x: R + D(str(x))
    rows = []
    prev_c = -32
    for k in range(6):
        c = -30 + 5 * k
        rows.append((prev_c, max(prev_c, c) + 4, min(prev_c, c) - 4, c))
        prev_c = c
    rows += [(-5, 10, -6, 6), (6, 16, 4, 12 if acceptance2 else -3), (10, 11, 0.5, 4 if retest_ok else -4), (3, 14, 2, 11)]
    bars = []
    for k, (o, h, l, c) in enumerate(rows):
        fl = flow(700, 300) if flows else None
        bars.append(mkbar(d, k, r(o), r(h), r(l), r(c), 1500, fl))
    return bars


def reversal_short_bars(d, flows=False, sweep=5.5, trend_acceptance=False):
    """Balanced session hovering far below prior VAH with high early volume, then a failed probe above VAH."""
    R, _val, _poc = ref_levels()
    r = lambda x: R + D(str(x))
    rows = [(-34 + (k % 2) * 2, -28, -40, -33 + (k % 2) * 2, 40000) for k in range(12)]
    # gradual approach, probe above VAH, failure, retest-and-reject
    rows += [(-33, -20, -35, -23, 1500), (-23, -12, -25, -14, 1500), (-14, -4, -16, -6, 1500)]
    if trend_acceptance:
        rows += [(-6, sweep, -8, 4, 1500), (4, sweep, 2, 6, 1500), (6, 7, -8, -3, 1500)]
    else:
        rows += [(-6, sweep, -8, 4, 1500), (4, 5, -8, -3, 1500)]
    rows += [(-3, -0.4, -3.3, -2.5, 1500)]
    bars = []
    for k, (o, h, l, c, v) in enumerate(rows):
        fl = None
        if flows:
            if k == len(rows) - 1:
                fl = flow(300, 700)             # rejection bar: strongly negative delta
            elif k == len(rows) - 3 or k == len(rows) - 4:
                fl = flow(800, 200)             # buying into the probe...
            elif k == len(rows) - 2:
                fl = flow(250, 750)             # ...then a bearish delta reversal
            else:
                fl = flow(520, 480)
        bars.append(mkbar(d, k, r(o), r(h), r(l), r(c), v, fl))
    return bars
