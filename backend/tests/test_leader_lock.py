"""Leader election against the real Postgres advisory lock (skipped if Postgres is unreachable)."""
import asyncio

import pytest
from sqlalchemy import create_engine, text

from app.config import settings
from app.leader_lock import LeaderElection, run_as_leader

TEST_KEY = 7_201_126_999  # not the production key, so a running API is unaffected


@pytest.fixture
def pg_engine():
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("advisory locks need PostgreSQL")
    eng = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:  # connection failure, not "no data"
        pytest.skip(f"PostgreSQL unreachable: {exc.__class__.__name__}")
    yield eng
    eng.dispose()


def test_only_one_election_wins_and_takeover_after_release(pg_engine):
    a, b = LeaderElection(pg_engine, TEST_KEY), LeaderElection(pg_engine, TEST_KEY)
    try:
        assert a.try_acquire() is True
        assert a.try_acquire() is True  # idempotent for the holder
        assert b.try_acquire() is False
        assert b.is_leader() is False
        a.release()
        assert b.try_acquire() is True
        assert a.is_leader() is False
    finally:
        a.release()
        b.release()


def test_lost_connection_drops_leadership_and_frees_lock(pg_engine):
    a, b = LeaderElection(pg_engine, TEST_KEY), LeaderElection(pg_engine, TEST_KEY)
    try:
        assert a.try_acquire()
        pid = a._conn.execute(text("SELECT pg_backend_pid()")).scalar()
        with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as killer:
            killer.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})
        assert a.heartbeat() is False
        assert a.is_leader() is False
        assert b.try_acquire() is True
    finally:
        a.release()
        b.release()


def test_non_postgres_engine_is_always_leader():
    eng = create_engine("sqlite://")
    election = LeaderElection(eng)
    assert election.try_acquire() and election.heartbeat() and election.is_leader()


def test_run_as_leader_runs_only_on_leader_and_fails_over(pg_engine):
    runs: dict[str, int] = {"a": 0, "b": 0}

    def make(name):
        async def loop():
            runs[name] += 1
            await asyncio.sleep(3600)
        return loop

    async def scenario():
        ea, eb = LeaderElection(pg_engine, TEST_KEY), LeaderElection(pg_engine, TEST_KEY)
        ta = asyncio.create_task(run_as_leader(ea, make("a"), "a", poll_seconds=0.1, heartbeat_seconds=0.1))
        await asyncio.sleep(0.5)
        tb = asyncio.create_task(run_as_leader(eb, make("b"), "b", poll_seconds=0.1, heartbeat_seconds=0.1))
        await asyncio.sleep(0.5)
        assert runs == {"a": 1, "b": 0}  # follower stays idle
        ta.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ta
        ea.release()  # leader shutdown frees the lock
        await asyncio.sleep(0.6)
        assert runs["b"] == 1  # follower took over
        tb.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tb
        eb.release()

    asyncio.run(scenario())
