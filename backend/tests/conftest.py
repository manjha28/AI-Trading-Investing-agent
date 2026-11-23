"""Shared test setup: tests must not depend on the owner's local .env switches.

Time: tests run on the real clock by default. Tests written around a trading session ("the market is open", "yesterday's close") should request the
``trading_session_clock`` fixture, which pins wall-clock time to a regular NSE trading day, mid-session (Wed 2026-11-18 12:00 IST) and lets it tick.
time-machine is used instead of freezegun because it leaves ``time.monotonic`` alone (rate limiters and timeouts keep working).
"""
import datetime as _dt

import pytest
import time_machine

import sys
from types import SimpleNamespace

from app import db as _app_db
from app.config import settings

_REAL_ENGINE = _app_db.engine

# The owner's local .env (Dhan-only data, 60 s quote freshness, Delta research on, ...) is operating configuration, not test input. Reset every setting to
# its shipped default for the whole test session so results do not depend on whose machine or .env the suite runs on.
_defaults = type(settings)(_env_file=None)
for _name in type(settings).model_fields:
    try:
        setattr(settings, _name, getattr(_defaults, _name))
    except Exception:  # noqa: BLE001 - frozen or computed fields keep their loaded value
        pass

SESSION_NOW = _dt.datetime(2026, 11, 18, 6, 30, tzinfo=_dt.timezone.utc)      # Wed 12:00 IST


@pytest.fixture
def trading_session_clock():
    with time_machine.travel(SESSION_NOW, tick=True):
        yield SESSION_NOW


@pytest.fixture(autouse=True)
def _optional_global_pin():
    """Diagnostic only: PIN_TEST_CLOCK=1 pins every test to the trading-session instant, to find which tests depend on the wall clock."""
    import os
    if os.environ.get("PIN_TEST_CLOCK") == "1":
        with time_machine.travel(SESSION_NOW, tick=True):
            yield
    else:
        yield


@pytest.fixture(autouse=True)
def _owner_env_switches_off(monkeypatch):
    monkeypatch.setattr(settings, "market_data_dhan_only", False)    # production .env sets this to true (Dhan-only price data); provider-chain tests need the default chain
    monkeypatch.setattr(settings, "delta_research_enabled", False)   # the owner's .env enables Delta research; tests assert the shipped default and opt in explicitly


def _rebind_engines(target) -> None:
    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(module, "engine", None) is _REAL_ENGINE:
            module.engine = target
    _app_db.engine = target


def _restore_engines(patched) -> None:
    for name, module in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and getattr(module, "engine", None) is patched:
            module.engine = _REAL_ENGINE
    _app_db.engine = _REAL_ENGINE


@pytest.fixture(autouse=True)
def _tests_never_touch_the_real_database(request):
    """Hermetic database: code that opens its own sessions from the module-level ``engine`` (worker threads, schedulers, WebSocket handlers) must see the
    test database, never the owner's real one. Tests that use a ``test_engine`` fixture get it everywhere; tests without one get a guard that fails loudly."""
    if "test_engine" in request.fixturenames:
        target = request.getfixturevalue("test_engine")
    else:
        def refuse(*_a, **_k):
            raise RuntimeError("this test touched the real database; give it a test_engine fixture")
        target = SimpleNamespace(connect=refuse, begin=refuse, raw_connection=refuse, dispose=lambda: None)
    _rebind_engines(target)
    yield
    _restore_engines(target)
