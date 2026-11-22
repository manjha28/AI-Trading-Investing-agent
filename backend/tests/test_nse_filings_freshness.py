"""NSE filings feed freshness warning (24 h) and the library pin that fixed the 17-day outage. Test data only: hand-made poll rows in an in-memory database."""
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import news_ingestion as ni
from app.db import Base
from app.models import NewsSourcePoll

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    Base.metadata.drop_all(e)
    e.dispose()


def _poll(db, last_ok, err=None):
    db.add(NewsSourcePoll(source="nse", last_attempted_at=NOW, last_successful_at=last_ok, last_error_category=err, rate_limited=False, articles_discovered=0))
    db.commit()


def test_warns_when_the_feed_has_not_succeeded_for_a_day(db, caplog):
    _poll(db, NOW - timedelta(days=17), "upstream_failure")
    with caplog.at_level(logging.WARNING, logger="app.news_ingestion"):
        out = ni.check_filings_freshness(db, NOW)
    assert out["stale"] is True and round(out["age_hours"]) == 17 * 24 and out["last_error"] == "upstream_failure"
    assert any("nse_filings_stale" in r.message and "upstream_failure" in r.message for r in caplog.records)


def test_a_feed_that_never_succeeded_warns_and_a_fresh_one_does_not(db, caplog):
    with caplog.at_level(logging.WARNING, logger="app.news_ingestion"):
        assert ni.check_filings_freshness(db, NOW)["stale"] is True                      # never polled
    assert any("nse_filings_stale" in r.message for r in caplog.records)
    caplog.clear()
    _poll(db, NOW - timedelta(hours=23))
    with caplog.at_level(logging.WARNING, logger="app.news_ingestion"):
        assert ni.check_filings_freshness(db, NOW)["stale"] is False
    assert not caplog.records
    db.query(NewsSourcePoll).one().last_successful_at = NOW - timedelta(hours=25)
    db.commit()
    assert ni.check_filings_freshness(db, NOW)["stale"] is True


def test_the_nse_library_pin_is_the_release_that_uses_the_new_domain():
    """The outage: nse 5.1.1 fetched cookies from www.nseindia.com/option-chain, which NSE now redirects (301) to nseindia.in; the library does not follow redirects. 5.2.0 uses nseindia.in."""
    pin = re.search(r"^nse==(\S+)", (Path(__file__).parents[1] / "requirements.txt").read_text(), re.M).group(1)
    assert pin == "5.2.0"
    import importlib.metadata as md
    import inspect

    from nse import transport
    assert tuple(int(x) for x in md.version("nse").split(".")[:2]) >= (5, 2)
    assert "nseindia.in/option-chain" in inspect.getsource(transport.Transport._fetch_cookies)
