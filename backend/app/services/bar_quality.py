"""Validate and clean stored 1-minute OHLCV before any indicator, signal or backtest uses it.

Never fills gaps: invalid rows and duplicates are removed and counted, missing minutes are counted and located.
Equity gaps are measured against the 09:15-15:30 IST session on every day the NSE calendar classifies as a trading day
(services.trading_calendar over the single calendar in app.config). Three distinct day states are reported:
``non_trading_days`` (weekend/holiday: zero expected candles), ``sessions_no_data`` (a trading day with no stored rows:
counted as a fully missing session) and ``partial_days`` (a session with under half of its candles). Days in years the
calendar does not cover are ``calendar_unknown_days``: they contribute zero expected candles and are never called holidays
or missing. Muhurat/special sessions fall outside the regular session and are reported as ``out_of_session``.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Any

import numpy as np
import pandas as pd

from app.services.timeframes import MINUTE, MarketProfile, in_session_mask, local_minutes
from app.services.trading_calendar import NON_TRADING, TRADING, UNKNOWN, classify_day

OK_MISSING_PCT = 0.5
DEFAULT_REFUSE_MISSING_PCT = 5.0
MAX_LISTED = 30


def clean_and_validate(frame: pd.DataFrame, profile: MarketProfile, *, now: pd.Timestamp | None = None,
                       max_missing_pct: float = DEFAULT_REFUSE_MISSING_PCT,
                       unserved: list[tuple[int, int]] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """``unserved`` lists exchange-local minute-of-day ranges [start, end) the provider structurally does not serve for this instrument.
    They are removed from the expected minutes (and so from the share compared with ``max_missing_pct``) and disclosed in the report
    as ``provider_unserved``; the threshold itself is never changed. Genuine fetch failures in served minutes still count in full."""
    report: dict[str, Any] = {"market": profile.key, "rows_in": int(len(frame))}
    unserved_rel = [] if profile.continuous else [(max(0, a - profile.open_minute), min(profile.session_minutes, b - profile.open_minute))
                                                  for a, b in (unserved or []) if b > a]
    unserved_rel = [(a, b) for a, b in unserved_rel if b > a]

    def served(span: int) -> int:
        """Minutes of the first ``span`` session minutes that the provider is expected to serve."""
        return span - sum(max(0, min(b, span) - a) for a, b in unserved_rel)
    unserved_total = 0
    if frame.empty:
        report.update(status="refuse", bars=0, issues=["no 1-minute data"], missing_minutes=0, expected_minutes=0, missing_pct=None)
        return frame, report
    f = frame.copy()
    report["out_of_order"] = int((f["time"].diff().dropna() < pd.Timedelta(0)).sum())
    f = f.sort_values("time", kind="stable")
    dup = f["time"].duplicated(keep="first")
    report["duplicates"] = int(dup.sum())
    f = f.loc[~dup]
    prices = f[["open", "high", "low", "close"]]
    bad = (prices.isna().any(axis=1) | (prices <= 0).any(axis=1) | (f["high"] < prices[["open", "close", "low"]].max(axis=1))
           | (f["low"] > prices[["open", "close", "high"]].min(axis=1)) | f["volume"].isna() | (f["volume"] < 0))
    report["invalid_ohlc_or_volume"] = int(bad.sum())
    f = f.loc[~bad]
    report["zero_volume_bars"] = int((f["volume"] == 0).sum())
    ins = in_session_mask(f["time"], profile)
    report["out_of_session"] = int((~ins).sum())
    f = f.loc[ins].reset_index(drop=True)
    issues: list[str] = []
    if f.empty:
        report.update(status="refuse", bars=0, issues=["no valid in-session 1-minute bars"], missing_minutes=0, expected_minutes=0, missing_pct=None)
        return f, report
    now = now or pd.Timestamp.now(tz="UTC")
    missing = 0
    expected = 0
    gaps: list[dict[str, Any]] = []
    no_data_sessions: list[str] = []
    non_trading: list[str] = []
    unknown_days: list[str] = []
    if profile.continuous:
        t = f["time"]
        expected = int((t.iloc[-1] - t.iloc[0]) // MINUTE) + 1
        steps = (t.diff().dropna() // MINUTE).to_numpy()
        for i in np.flatnonzero(steps > 1)[:MAX_LISTED]:
            gaps.append({"from": t.iloc[i].isoformat(), "missing_minutes": int(steps[i] - 1)})
        missing = int(expected - len(f))
        empty_days: list[str] = []
        partial_days: list[str] = []
    else:
        days, minute_of_day = local_minutes(f["time"], profile)
        rel = (minute_of_day - profile.open_minute).to_numpy()
        per_day = f.assign(_day=days.to_numpy(), _rel=rel).groupby("_day")
        partial_days = []
        last_day = days.iloc[-1]
        local_now = now.tz_convert(profile.tz)
        for day, g in per_day:
            span = profile.session_minutes
            if day == last_day and local_now.strftime("%Y-%m-%d") == day:  # session may still be running: only count elapsed minutes
                elapsed = (local_now.hour * 60 + local_now.minute) - profile.open_minute
                span = int(max(1, min(span, elapsed)))
            exp = served(span)
            unserved_total += span - exp
            in_gap = np.zeros(len(g), dtype=bool)
            for a, b in unserved_rel:
                in_gap |= ((g["_rel"] >= a) & (g["_rel"] < b)).to_numpy()
            present = int(len(g) - in_gap.sum())
            expected += exp
            absent = max(0, exp - present)
            missing += absent
            if absent:
                gaps.append({"day": day, "missing_minutes": int(absent), "present": present})
            if present < exp * 0.5:
                partial_days.append(day)
        firsts = f.groupby(days.to_numpy(), sort=True).agg(open=("open", "first"), close=("close", "last"))
        jump = (firsts["open"] / firsts["close"].shift(1) - 1).abs().dropna()
        report["max_overnight_gap_pct"] = round(float(jump.max() * 100), 2) if len(jump) else 0.0
        if len(jump) and jump.max() > 0.15:
            issues.append(f"overnight gap of {report['max_overnight_gap_pct']}% on {jump.idxmax()}: prices are NOT adjusted for splits/bonus issues; results around it may be wrong")
        # Classify every DATALESS day in the span against the NSE trading calendar. A trading day with no
        # rows is a fully missing session (counted); holidays/weekends expect zero candles; years without
        # calendar coverage are reported as unknown, never counted as missing and never called holidays.
        have = set(days)
        today_local = local_now.strftime("%Y-%m-%d")
        for d in pd.date_range(days.iloc[0], days.iloc[-1], freq="D").strftime("%Y-%m-%d"):
            if d in have:
                continue
            kind = classify_day(date_type.fromisoformat(d), profile.key)
            if kind == TRADING and d != today_local:  # today-with-no-rows-yet is freshness, not a missing session
                no_data_sessions.append(d)
                day_served = served(profile.session_minutes)
                unserved_total += profile.session_minutes - day_served
                expected += day_served
                missing += day_served
                gaps.append({"day": d, "missing_minutes": int(day_served), "present": 0})
            elif kind == NON_TRADING and date_type.fromisoformat(d).weekday() < 5:
                non_trading.append(d)
            elif kind == UNKNOWN:
                unknown_days.append(d)
        gaps = gaps[:MAX_LISTED]
        empty_days = no_data_sessions  # kept under the historical report key; weekends/holidays are no longer in it
    pct = round(100.0 * missing / expected, 3) if expected else 0.0
    last_close = f["time"].iloc[-1] + MINUTE
    report.update(
        bars=int(len(f)), window_start=f["time"].iloc[0].isoformat(), window_end=f["time"].iloc[-1].isoformat(),
        expected_minutes=int(expected), missing_minutes=int(missing), missing_pct=pct, gaps=gaps,
        partial_days=partial_days[:MAX_LISTED], empty_weekdays=empty_days[:MAX_LISTED], empty_weekday_count=len(empty_days),
        sessions_no_data=no_data_sessions[:MAX_LISTED], sessions_no_data_count=len(no_data_sessions),
        non_trading_days=non_trading[:MAX_LISTED], non_trading_day_count=len(non_trading),
        calendar_unknown_days=unknown_days[:MAX_LISTED], calendar_unknown_day_count=len(unknown_days),
        last_bar_age_seconds=max(0, int((now - last_close).total_seconds())))
    if unserved_rel:
        report["provider_unserved"] = {"ranges_local": [{"start": f"{a // 60:02d}:{a % 60:02d}", "end": f"{(b - 1) // 60:02d}:{(b - 1) % 60:02d}"} for a, b in (unserved or [])],
                                       "excluded_minutes": int(unserved_total),
                                       "note": "Provider does not serve these minutes; excluded from the missing share, which is not padded."}
    if report["duplicates"]:
        issues.append(f"{report['duplicates']} duplicate candle(s) removed (first kept)")
    if report["invalid_ohlc_or_volume"]:
        issues.append(f"{report['invalid_ohlc_or_volume']} invalid OHLC/volume row(s) removed")
    if report["out_of_session"]:
        issues.append(f"{report['out_of_session']} out-of-session row(s) excluded")
    if report["out_of_order"]:
        issues.append(f"{report['out_of_order']} out-of-order row(s) re-sorted")
    if pct > OK_MISSING_PCT:
        issues.append(f"{missing} missing 1-minute candle(s) ({pct}% of expected) - not filled")
    if partial_days:
        issues.append(f"{len(partial_days)} session(s) have data but under half of the expected candles (partial capture)")
    if non_trading:
        issues.append(f"{len(non_trading)} weekday(s) are NSE non-trading days (holiday/special session): zero candles expected")
    if no_data_sessions:
        issues.append(f"{len(no_data_sessions)} trading session(s) have no data at all (counted as missing; e.g. {no_data_sessions[0]})")
    if unknown_days:
        years = sorted({d[:4] for d in unknown_days})
        issues.append(f"{len(unknown_days)} weekday(s) fall in year(s) without NSE calendar coverage ({', '.join(years)}): "
                      "not counted as missing and not confirmed as holidays")
    status = "refuse" if pct > max_missing_pct else ("warn" if issues else "ok")
    if status == "refuse":
        issues.append(f"missing share {pct}% exceeds the {max_missing_pct}% limit: backtest refused for this window")
    report.update(status=status, issues=issues)
    return f, report
