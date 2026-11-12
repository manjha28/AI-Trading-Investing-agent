"""Day classification against the single NSE holiday calendar in app.config (do not duplicate those dates).

``classify_day`` answers "was/is this a trading day?" for a market profile:
  - "trading":      a regular session is expected; a day with no stored bars is MISSING data.
  - "non_trading":  weekend, verified NSE holiday, unverified-holiday date or special-session date
                    (special sessions such as Muhurat fall outside the regular session, which is the
                    only session the 1-minute store measures); zero candles are expected.
  - "unknown":      the calendar has no coverage for that year (NSE_CALENDAR_YEARS). Callers must not
                    count such days as missing OR as confirmed holidays - say so explicitly.

Continuous markets (crypto) have no closed days: every day is "trading".
The verified dates in app.config were cross-checked against the NSE trading-holiday API
(nse library, segment CM) on 2026-11-11; the API serves the current year only, so past years
stay "unknown" until their circulars are added to the config calendar.
"""
from __future__ import annotations

from datetime import date

from app.config import NSE_CALENDAR_YEARS, NSE_NON_TRADING_DATES, NSE_SPECIAL_SESSION_DATES

TRADING = "trading"
NON_TRADING = "non_trading"
UNKNOWN = "unknown"


def classify_day(day: date, profile_key: str) -> str:
    if profile_key != "equity_in":
        return TRADING  # continuous market: no closed days
    if day.weekday() >= 5:
        return NON_TRADING
    if day.year not in NSE_CALENDAR_YEARS:
        return UNKNOWN
    if day in NSE_NON_TRADING_DATES or day in NSE_SPECIAL_SESSION_DATES:
        return NON_TRADING
    return TRADING
