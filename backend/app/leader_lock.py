"""Single-leader election for in-process periodic loops.

Several API replicas each start the same schedulers (snapshots, alerts, news, market data).
A Postgres session-level advisory lock, held on one dedicated connection, makes exactly one
replica the leader. If the leader's connection dies the lock is released and another replica
takes over; the old leader notices through its heartbeat and stops its loops.

On non-Postgres databases (SQLite in tests) there is nothing to coordinate and every process
is its own leader.
"""
import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

SCHEDULER_LOCK_KEY = 7_201_126_001  # arbitrary application-wide constant (bigint)


class LeaderElection:
    def __init__(self, engine: Engine, key: int = SCHEDULER_LOCK_KEY) -> None:
        self._engine = engine
        self._key = key
        self._conn: Connection | None = None
        self._mutex = threading.Lock()

    @property
    def _coordinated(self) -> bool:
        return self._engine.dialect.name == "postgresql"

    def is_leader(self) -> bool:
        with self._mutex:
            return self._conn is not None or not self._coordinated

    def try_acquire(self) -> bool:
        """Return True if this process is (or has just become) the leader."""
        with self._mutex:
            if not self._coordinated or self._conn is not None:
                return True
            conn = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            try:
                got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self._key}).scalar())
            except Exception:
                conn.close()
                raise
            if not got:
                conn.close()
                return False
            self._conn = conn
            return True

    def heartbeat(self) -> bool:
        """Confirm the lock connection is alive; drop leadership if it is not."""
        with self._mutex:
            if not self._coordinated:
                return True
            if self._conn is None:
                return False
            try:
                self._conn.execute(text("SELECT 1"))
                return True
            except Exception:
                logger.warning("Scheduler leader connection lost; leadership dropped")
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None
                return False

    def release(self) -> None:
        with self._mutex:
            if self._conn is None:
                return
            with contextlib.suppress(Exception):
                self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self._key})
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None


async def run_as_leader(
    election: LeaderElection,
    loop_factory: Callable[[], Awaitable[object]],
    name: str,
    *,
    poll_seconds: float = 30.0,
    heartbeat_seconds: float = 10.0,
) -> None:
    """Run ``loop_factory()`` only while this process holds leadership.

    Waits (polling) until elected, runs the loop, and stops it again if leadership is lost.
    """
    while True:
        try:
            elected = await asyncio.to_thread(election.try_acquire)
        except Exception:
            logger.exception("Leader election failed for %s; retrying", name)
            elected = False
        if not elected:
            await asyncio.sleep(poll_seconds)
            continue
        logger.info("Scheduler leader: starting %s", name)
        task = asyncio.ensure_future(loop_factory())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=heartbeat_seconds)
                if done:
                    task.result()  # re-raise loop errors; a clean return ends the wrapper
                    return
                if not await asyncio.to_thread(election.heartbeat):
                    logger.warning("Scheduler leadership lost: stopping %s", name)
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    break
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise
