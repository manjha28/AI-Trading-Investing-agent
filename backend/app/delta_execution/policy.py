"""Hard, code-level limits. Raising any of these needs a new owner approval recorded in AGENTS.md; configuration cannot raise them."""
from __future__ import annotations

import os
from decimal import Decimal

from app.config import settings

ALLOWED_STRATEGY_KEYS: frozenset[str] = frozenset({"five_ema_alert"})
MAX_CONTRACTS = 1                       # one lot, whatever the lot size is
LEVERAGE_CAP = 25                       # the lower of this and the contract's own maximum is used
MARKETABLE_LIMIT_BUFFER = Decimal("0.003")   # entry is a limit 0.3% through the last price: fills like a market order, never runs away
CONFIRMATION_PHRASE = "ENABLE DELTA LIVE ORDERS"
BASE_URL = "https://api.india.delta.exchange"


def effective_leverage_cap() -> Decimal:
    """Order value / wallet: the leverage actually carried. Default 25x, configurable DOWNWARD only; it can never exceed LEVERAGE_CAP."""
    return Decimal(max(1, min(LEVERAGE_CAP, int(settings.delta_effective_leverage_cap))))


def environment_block() -> str | None:
    """Why live order endpoints must not be reached from THIS process, or None. Tests and CI can never place an order."""
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in os.environ.get("_", ""):
        return "running under pytest"
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        return "running in CI"
    return None


def deployment_enabled() -> bool:
    return bool(settings.delta_live_execution_enabled)


def credentials_present() -> bool:
    return bool(settings.delta_api_key and settings.delta_api_key.get_secret_value() and settings.delta_api_secret and settings.delta_api_secret.get_secret_value())


def effective_leverage(initial_margin_pct: Decimal | None) -> int | None:
    """min(25x, the exchange maximum = 100 / initial margin %). None when the contract's margin is unknown: the order is then refused."""
    if initial_margin_pct is None or initial_margin_pct <= 0:
        return None
    maximum = int(Decimal(100) / initial_margin_pct)
    return max(1, min(LEVERAGE_CAP, maximum))
