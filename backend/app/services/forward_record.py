"""Weekly forward record (Phase 7b). The nightly shadow tables (``stock_verdicts``, ``multibagger_assessments``) are the only truly point-in-time record: each row was written on its own night from what was known then.
This job takes every shadow BUY (buy_now / buy_on_trigger) verdict old enough to have resolved (swing: 15 sessions, positional: 60) and plays it on the completed bars that came after with ``simulate_card`` and the same
delivery costs as the replays; and it follows each long-term class forward 3, 6 and 12 months against the NIFTY 500 price index. Each observation is one stock-horizon EPISODE (the first night of a run of consecutive
buy verdicts) so a stock that stays a buy for a month is not counted thirty times; the all-nights view is shown beside it. Stored weekly; hypothetical, never real P&L, never mixed with portfolio figures.
Decision support, not investment advice."""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select

from app.services import shadow_outcomes as so
from app.services.technical_backtest import CARD_HOLD, simulate_card

logger = logging.getLogger(__name__)
BUY = ("buy_now", "buy_on_trigger")
MIN_SESSIONS = {"swing": 15, "positional": 60}
HORIZON_MONTHS = (3, 6, 12)
INDEX_SYMBOL = "^CRSLDX"
EPISODE_GAP_SESSIONS = 5
MIN_DAYS_BETWEEN_RUNS = 6
LT_GROUPS = {"quality_compounder": "compounder", "multibagger_candidate": "compounder", "emerging": "emerging", "watch": "watch", "not_a_fit": "not_a_fit", "avoid": "avoid"}
DISCLAIMER = "Decision support, not investment advice. Hypothetical results from stored shadow verdicts; never real portfolio P&L."


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t["outcome"] == "t1")
    lo, hi = so.wilson(wins, n)
    m, mlo, mhi = so.mean_ci([t["net_r"] for t in trades])
    if n == 1:
        m = trades[0]["net_r"]                              # one trade has a mean but no interval
    nz = lambda x: None if x != x else round(x, 3)  # noqa: E731 - NaN (too few trades) is unknown, never zero
    return {"trades": n, "t1_hits": wins, "hit_rate": None if not n else round(wins / n, 4), "hit_ci95": [nz(lo), nz(hi)], "net_r": nz(m), "net_r_ci95": [nz(mlo), nz(mhi)],
            "stopped": sum(1 for t in trades if t["outcome"] == "stop"), "timed_out": sum(1 for t in trades if t["outcome"] == "time")}


def _blue_sky(levels: dict[str, Any]) -> bool:
    t = (levels.get("targets") or [{}])[0]
    return "Blue sky" in str((t or {}).get("basis") or "")


def _play(row: Any, frame: pd.DataFrame, now: datetime) -> dict[str, Any] | None:
    """One stored verdict -> an outcome, or None when it is not playable (no levels) / too recent. 'pending' while the window is still open."""
    lv = (row.reasons or {}).get("levels") or {}
    try:
        planned, stop, t1 = float(lv["planned_entry"]), float(lv["stop"]), float(lv["targets"][0]["price"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    after = frame.loc[frame.index > pd.Timestamp(row.as_of)]
    if len(after) < MIN_SESSIONS[row.horizon]:
        return {"state": "too_recent"}
    r = simulate_card(lv.get("entry_status") or "WAIT_FOR_ENTRY", planned, stop, t1, after["open"].values, after["high"].values, after["low"].values, after["close"].values, CARD_HOLD[row.horizon])
    if r["outcome"] in ("pending",):
        return {"state": "pending"}
    if r["outcome"] not in ("stop", "t1", "time"):
        return {"state": r["outcome"]}                      # unfilled / invalid: not a trade, counted, never 0R
    risk = r["entry"] - stop
    d_in, d_out = after.index[r["k_in"]].date(), after.index[r["k_out"]].date()
    cf = so._cost_fraction(r["entry"], r["exit"], d_in, d_out)
    return {"state": "trade", "outcome": r["outcome"], "net_r": (r["exit"] - r["entry"]) / risk - cf * r["entry"] / risk, "fill_date": str(d_in), "exit_date": str(d_out), "blue_sky": _blue_sky(lv),
            "predicted": ((row.reasons or {}).get("confidence") or {}).get("win_probability")}


def _reliability(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Predicted win probability (stored on the night, before the outcome) against the actual win rate, in up to ten equal-count buckets."""
    xs = sorted((t for t in trades if t.get("predicted") is not None), key=lambda t: t["predicted"])
    if len(xs) < 10:
        return []
    k = min(10, max(2, len(xs) // 10))
    out = []
    for i in range(k):
        part = xs[i * len(xs) // k:(i + 1) * len(xs) // k]
        out.append({"bucket": i + 1, "n": len(part), "predicted": round(sum(t["predicted"] for t in part) / len(part), 4), "actual": round(sum(1 for t in part if t["outcome"] == "t1") / len(part), 4)})
    return out


def short_horizon(db: Any, user_id: Any, now: datetime) -> dict[str, Any]:
    from app.models import Instrument
    from app.models.stock_verdict import StockVerdict
    from app.services import gates_v2, research_pipeline as rp
    today = now.astimezone(timezone.utc).date()
    rows = list(db.scalars(select(StockVerdict).where(StockVerdict.user_id == user_id, StockVerdict.horizon.in_(tuple(MIN_SESSIONS)), StockVerdict.verdict.in_(BUY), StockVerdict.as_of < today)
                           .order_by(StockVerdict.instrument_id, StockVerdict.horizon, StockVerdict.as_of)))
    frames: dict[Any, pd.DataFrame | None] = {}
    last_buy: dict[tuple[Any, str], date] = {}
    trades: list[dict[str, Any]] = []
    skipped: dict[str, int] = defaultdict(int)
    for r in rows:
        key = (r.instrument_id, r.horizon)
        episode = key not in last_buy or (r.as_of - last_buy[key]).days > EPISODE_GAP_SESSIONS * 7 / 5
        last_buy[key] = r.as_of
        if r.instrument_id not in frames:
            f = rp._daily_frame(db, r.instrument_id, None, limit=500)
            frames[r.instrument_id] = None if f is None else gates_v2.drop_forming_bar(f, now)
        f = frames[r.instrument_id]
        if f is None or not len(f):
            skipped["no_bars"] += 1
            continue
        res = _play(r, f, now)
        if res is None:
            skipped["no_levels"] += 1
        elif res["state"] != "trade":
            skipped[res["state"]] += 1
        else:
            sym = db.scalar(select(Instrument.symbol).where(Instrument.id == r.instrument_id))
            trades.append({**res, "horizon": r.horizon, "verdict": r.verdict, "as_of": str(r.as_of), "symbol": sym, "episode": episode})
    out: dict[str, Any] = {"observations": len(rows), "not_a_trade": dict(skipped), "by_horizon": {}}
    for h in MIN_SESSIONS:
        for scope, pick in (("episodes", lambda t: t["episode"]), ("all_nights", lambda t: True)):
            ht = [t for t in trades if t["horizon"] == h and pick(t)]
            block = {"all": _stats(ht), "by_verdict": {v: _stats([t for t in ht if t["verdict"] == v]) for v in BUY}, "blue_sky": _stats([t for t in ht if t["blue_sky"]]),
                     "not_blue_sky": _stats([t for t in ht if not t["blue_sky"]])}
            if scope == "episodes":
                block["reliability"] = _reliability(ht)
            out["by_horizon"].setdefault(h, {})[scope] = block
    out["first_signal"] = None if not rows else str(rows[0].as_of)
    return out


def _index_series(db: Any) -> pd.Series | None:
    from app.models import Instrument, MarketBar
    iid = db.scalar(select(Instrument.id).where(Instrument.symbol == INDEX_SYMBOL, Instrument.asset_class == "index"))
    if iid is None:
        return None
    rows = db.execute(select(MarketBar.timestamp, MarketBar.close).where(MarketBar.instrument_id == iid, MarketBar.interval == "1d", MarketBar.source == "dhan")).all()
    if not rows:
        return None
    return pd.Series({pd.Timestamp(ts.date()): float(c) for ts, c in rows}).sort_index()


def _px_on_or_after(s: pd.Series, d: pd.Timestamp, slack_days: int = 7) -> float | None:
    after = s.loc[d:]
    return None if not len(after) or (after.index[0] - d).days > slack_days else float(after.iloc[0])


def long_term(db: Any, user_id: Any, now: datetime) -> dict[str, Any]:
    from app.models.multibagger import MultibaggerAssessment
    from app.services import research_pipeline as rp
    ix = _index_series(db)
    rows = list(db.scalars(select(MultibaggerAssessment).where(MultibaggerAssessment.user_id == user_id).order_by(MultibaggerAssessment.as_of)))
    if ix is None or not rows:
        return {"available": False, "reason": "No NIFTY 500 index bars or no stored class history yet.", "by_months": {}}
    base = rows[0].as_of.toordinal() % 7                               # one observation per stock per week: the nightly rows overlap almost completely
    frames: dict[Any, pd.DataFrame | None] = {}
    last_day = ix.index[-1]
    out: dict[str, Any] = {"available": True, "first_class_date": str(rows[0].as_of), "benchmark": "NIFTY 500 price index (Dhan IDX_I 19); a price index, not total return", "by_months": {}}
    for months in HORIZON_MONTHS:
        per: dict[str, list[float]] = defaultdict(list)
        for a in rows:
            grp = LT_GROUPS.get(a.klass)
            if grp is None or a.as_of.toordinal() % 7 != base:
                continue
            target = pd.Timestamp(a.as_of) + pd.DateOffset(months=months)
            if target > last_day:
                continue
            if a.instrument_id not in frames:
                frames[a.instrument_id] = rp._daily_frame(db, a.instrument_id, None, limit=1500)
            f = frames[a.instrument_id]
            if f is None:
                continue
            p0, p1 = _px_on_or_after(f["close"], pd.Timestamp(a.as_of)), _px_on_or_after(f["close"], target)
            i0, i1 = _px_on_or_after(ix, pd.Timestamp(a.as_of)), _px_on_or_after(ix, target)
            if None in (p0, p1, i0, i1):
                continue
            per[grp].append((p1 / p0 - 1) - (i1 / i0 - 1))
        block = {}
        for grp, xs in per.items():
            m, lo, hi = so.mean_ci(xs)
            s = sorted(xs)
            med = s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2
            nz = lambda x: None if x != x else round(x, 4)  # noqa: E731
            block[grp] = {"n": len(xs), "median_excess": round(med, 4), "mean_excess": nz(m), "mean_ci95": [nz(lo), nz(hi)], "beat_index": round(sum(1 for x in xs if x > 0) / len(xs), 4)}
        out["by_months"][str(months)] = block or {"note": f"No class observation is {months} months old yet (first class date {rows[0].as_of}; first {months}-month reading on {(pd.Timestamp(rows[0].as_of) + pd.DateOffset(months=months)).date()})."}
    return out


def build(db: Any, user_id: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    return {"generated_at": now.isoformat(), "short_horizon": short_horizon(db, user_id, now), "long_term": long_term(db, user_id, now),
            "definitions": {"swing": "T1 reached before the stop within 15 sessions of the fill, after delivery costs", "positional": "the same within 60 sessions", "episode": "first night of a run of consecutive buy verdicts for one stock and horizon",
                            "long_term": "stock return minus NIFTY 500 price-index return over 3 / 6 / 12 months from the class date, one reading per stock per week"}, "disclaimer": DISCLAIMER}


def run_if_due(db: Any, user_id: Any, now: datetime | None = None) -> bool:
    """Weekly job: store a report unless one was stored in the last 6 days. Never raises."""
    from app.models.forward_record import ForwardRecordReport
    now = now or datetime.now(timezone.utc)
    try:
        last = db.scalar(select(ForwardRecordReport.as_of).where(ForwardRecordReport.user_id == user_id).order_by(ForwardRecordReport.as_of.desc()).limit(1))
        if last is not None and (now.date() - last).days < MIN_DAYS_BETWEEN_RUNS:
            return False
        payload = build(db, user_id, now)
        row = db.scalar(select(ForwardRecordReport).where(ForwardRecordReport.user_id == user_id, ForwardRecordReport.as_of == now.date()))
        if row is None:
            db.add(ForwardRecordReport(user_id=user_id, as_of=now.date(), computed_at=now, payload=_clean(payload)))
        else:
            row.computed_at, row.payload = now, _clean(payload)
        db.commit()
        logger.info("forward_record_stored as_of=%s", now.date())
        return True
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("forward_record_failed error_class=%s", type(exc).__name__)
        return False


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


def latest(db: Any, user_id: Any) -> dict[str, Any] | None:
    from app.models.forward_record import ForwardRecordReport
    row = db.scalar(select(ForwardRecordReport).where(ForwardRecordReport.user_id == user_id).order_by(ForwardRecordReport.as_of.desc()).limit(1))
    return None if row is None else {"as_of": str(row.as_of), "computed_at": row.computed_at.isoformat(), **row.payload}
