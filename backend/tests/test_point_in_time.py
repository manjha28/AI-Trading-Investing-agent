"""Phase 6 step 1: extra Yahoo statement rows with provenance, and the dated, append-only snapshot tables. Test data only: hand-built frames and an in-memory database; no network."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Instrument, InstrumentFundamental
from app.models.fundamental_snapshots import FundamentalSnapshot, ShareholdingSnapshot
from app.services import fundamentals as F
from app.services import point_in_time as pit

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def eng():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


def _inst(db):
    i = Instrument(id=uuid4(), symbol="AAA", display_name="AAA", asset_class="equity", venue="NSE", currency="INR", country="IN", timezone="Asia/Kolkata", is_synthetic=False, relink_required=False, created_at=NOW)
    db.add(i)
    db.commit()
    return i


def _frames():
    p = pd.Timestamp("2026-03-31")
    income = pd.DataFrame({p: [1000.0, 100.0, 40.0, 150.0, 190.0, 5.0]}, index=["Total Revenue", "Net Income", "Interest Expense", "Pretax Income", "EBIT", "Basic EPS"])
    balance = pd.DataFrame({p: [900.0, 300.0, 55.0, 80.0]}, index=["Total Assets", "Net PPE", "Cash And Cash Equivalents", "Total Debt"])
    return income, balance, pd.DataFrame({p: [120.0]}, index=["Operating Cash Flow"])


def test_period_rows_capture_the_extra_yahoo_rows_under_screener_keys():
    income, balance, cash = _frames()
    (row,) = F._period_rows(income, balance, cash, "annual", "INR")
    assert row["extras"] == {"interest_expense": Decimal("40"), "profit_before_tax": Decimal("150"), "ebit": Decimal("190"), "fixed_assets": Decimal("300"), "cash": Decimal("55")}
    assert row["revenue"] == Decimal("1000")                                         # the canonical columns are untouched
    income2 = income.drop(index=["Interest Expense", "EBIT"])
    (row2,) = F._period_rows(income2, balance, cash, "annual", "INR")
    assert row2["extras"]["interest_expense"] is None and row2["extras"]["ebit"] is None      # missing stays missing, never zero


def test_extras_never_overwrite_another_sources_value_but_refresh_yahoos_own(eng):
    with Session(eng) as db:
        i = _inst(db)
        row = InstrumentFundamental(id=uuid4(), instrument_id=i.id, period_type="annual", period_end=date(2026, 3, 31), currency="INR", source="yfinance", fetched_at=NOW,
                                    extra_metrics={"interest_expense": {"value": "999", "source": "screener", "fetched_at": "2026-11-01", "basis": "consolidated"},
                                                   "cash": {"value": "1", "source": "yfinance", "fetched_at": "2026-10-01", "basis": None}})
        F._merge_yahoo_extras(row, {"interest_expense": Decimal("40"), "cash": Decimal("55"), "ebit": Decimal("190"), "fixed_assets": None}, NOW)
        em = row.extra_metrics
        assert em["interest_expense"]["value"] == "999" and em["interest_expense"]["source"] == "screener"      # Screener's value is kept
        assert em["cash"]["value"] == "55" and em["cash"]["source"] == "yfinance"                               # Yahoo's own value refreshed
        assert em["ebit"]["source"] == "yfinance" and "fixed_assets" not in em                                  # a missing value is not written


def _fake_fetch(monkeypatch):
    income, balance, cash = _frames()
    rows = F._period_rows(income, balance, cash, "annual", "INR")
    monkeypatch.setattr(F, "fetch_yfinance_fundamentals", lambda sym: {"annual": [dict(r, extras=dict(r["extras"])) for r in rows], "quarterly": [], "snapshot": {"market_cap": Decimal("5"), "pe": None, "pb": None, "dividend_yield": None, "sector": "S", "industry": "I"},
                                                                         "valuation_inputs": {"pe": {"value": Decimal("20"), "unit": "x"}}})


def test_each_sync_appends_a_dated_snapshot_and_never_edits_an_earlier_one(eng, monkeypatch):
    _fake_fetch(monkeypatch)
    with Session(eng) as db:
        i = _inst(db)
        rows, _ = F.sync_instrument_fundamentals(db, i, "AAA.NS", fetched_at=NOW)
        assert rows[0].extra_metrics["interest_expense"]["source"] == "yfinance"
        first = db.scalar(select(FundamentalSnapshot))
        payload_before = dict(first.payload)
        F.sync_instrument_fundamentals(db, i, "AAA.NS", fetched_at=NOW + timedelta(days=1))
        assert db.scalar(select(func.count()).select_from(FundamentalSnapshot)) == 2
        db.refresh(first)
        assert first.payload == payload_before and first.captured_on == date(2026, 11, 22)
        p = first.payload["periods"][0]
        assert p["values"]["revenue"].startswith("1000") and Decimal(p["extra_metrics"]["ebit"]["value"]) == 190 and p["period_end"] == "2026-03-31"
        assert first.payload["valuation_inputs"]["pe"]["value"] == "20"


def test_snapshots_are_append_only(eng, monkeypatch):
    _fake_fetch(monkeypatch)
    with Session(eng) as db:
        i = _inst(db)
        F.sync_instrument_fundamentals(db, i, "AAA.NS", fetched_at=NOW)
        snap = db.scalar(select(FundamentalSnapshot))
        snap.source = "tampered"
        with pytest.raises(RuntimeError, match="append-only"):
            db.commit()
        db.rollback()
        db.delete(db.scalar(select(FundamentalSnapshot)))
        with pytest.raises(RuntimeError, match="append-only"):
            db.commit()


def _dhan(status="available"):
    return {"shareholding": {"status": status, "holdings": [{"key": "promoter", "percent": "54.1"}, {"key": "fii", "percent": "12.5"}, {"key": "dii", "percent": "9.2"}, {"key": "public", "percent": "24.2"}]}}


def test_shareholding_snapshot_is_one_per_day_with_pledge_unknown(eng):
    with Session(eng) as db:
        i = _inst(db)
        assert pit.record_shareholding_snapshot(db, i.id, _dhan(), now=NOW) is True
        assert pit.record_shareholding_snapshot(db, i.id, _dhan(), now=NOW + timedelta(hours=3)) is False        # same IST day: not duplicated
        assert pit.record_shareholding_snapshot(db, i.id, _dhan(), now=NOW + timedelta(days=1)) is True
        rows = list(db.scalars(select(ShareholdingSnapshot).order_by(ShareholdingSnapshot.captured_on)))
        assert [r.captured_on for r in rows] == [date(2026, 11, 22), date(2026, 11, 23)]
        r = rows[0]
        assert (r.promoter_pct, r.fii_pct, r.dii_pct, r.public_pct) == (Decimal("54.1"), Decimal("12.5"), Decimal("9.2"), Decimal("24.2")) and r.pledged_pct_of_promoter is None and r.source == "dhan"
        assert pit.record_shareholding_snapshot(db, i.id, _dhan("unavailable"), now=NOW + timedelta(days=2)) is False   # an unavailable shareholding section writes nothing


def test_nse_filings_adapter_is_disabled_and_has_no_network_client(monkeypatch):
    import importlib
    import inspect

    from app.config import settings
    from app.services import nse_filings_adapter as nse
    assert settings.nse_filings_enabled is False and nse.enabled() is False
    with pytest.raises(nse.AdapterDisabled):
        nse.fetch_governance_events("AAA")
    src = inspect.getsource(importlib.import_module("app.services.nse_filings_adapter"))
    assert not any(w in src for w in ("import requests", "import httpx", "urllib", "curl", "aiohttp"))          # no HTTP client at all in this build
