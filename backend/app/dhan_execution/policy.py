"""Hard, code-level limits. Raising any of these requires a new owner approval recorded in AGENTS.md; configuration cannot raise them."""
from __future__ import annotations

import os
from datetime import time

from app.config import settings

ALLOWED_STRATEGY_KEYS: frozenset[str] = frozenset({"five_ema_alert", "mfi_dip_30m"})
MAX_ORDER_QUANTITY = 1
PRODUCT_TYPE = "INTRADAY"
EXCHANGE_SEGMENT = "NSE_EQ"
ENTRY_WINDOW_IST: tuple[time, time] = (time(9, 20), time(14, 30))     # no entries in the opening auction noise or late in the session
PRICE_MAX_AGE_SECONDS = 150
MARKETABLE_LIMIT_BUFFER = 0.003                                         # entry is a limit 0.3% through the last price: fills like a market order, never runs away
CONFIRMATION_PHRASE = "ENABLE LIVE ORDERS"
STRATEGY_PREFIX = "reg_"


def environment_block() -> str | None:
    """Why live order endpoints must not be reached from THIS process, or None. Tests and CI can never place an order."""
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.environ.get("_", ""):
        return "running under pytest"
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        return "running in CI"
    return None


def deployment_enabled() -> bool:
    return bool(getattr(settings, "dhan_live_execution_enabled", False))
