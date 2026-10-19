"""Bounded analysis queue hints. SQL remains the sole work/ownership authority."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def _fd_ready(connection, *, write=False):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = connection.fileno()

    def ready():
        if not future.done():
            future.set_result(None)

    register = loop.add_writer if write else loop.add_reader
    remove = loop.remove_writer if write else loop.remove_reader
    register(fd, ready)
    try:
        await future
    finally:
        remove(fd)


async def _pg_ready(connection):
    from psycopg2.extensions import POLL_OK, POLL_READ, POLL_WRITE

    while True:
        state = connection.poll()
        if state == POLL_OK:
            return
        if state not in (POLL_READ, POLL_WRITE):
            raise RuntimeError("Unexpected PostgreSQL listener state")
        await _fd_ready(connection, write=state == POLL_WRITE)


class AnalysisWakeup:
    """One coordinator/listener shared by all workers of the application DB.

    No queue items are kept here. Cached deadlines suppress duplicate empty
    claims, but every claim and ownership transition still happens in SQL.
    """

    fallback_seconds = 10.0
    sqlite_check_seconds = 1.0

    def __init__(self, db):
        self._db = db
        self._loop = None
        self._listener = None
        self._connection = None
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()
        self._generation = 0
        self._checked_generation = -1
        self._check_at = 0.0
        self._recovery_check_at = 0.0
        self._draining = False
        self._users = 0
        self._notify_lock = threading.Lock()
        self._notify_pending = False
        self.recovered_total = 0
        self.last_check_at = None
        self.listener_ready = asyncio.Event()

    async def start(self):
        self._users += 1
        if self._users != 1:
            return
        self._loop = asyncio.get_running_loop()
        self._invalidate()
        self._listener = asyncio.create_task(self._listen(), name="analysis-hints")

    async def stop(self):
        if not self._users:
            return
        self._users -= 1
        if self._users:
            return
        self._invalidate()
        task, self._listener = self._listener, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._loop = None

    def notify(self):
        """Thread-safe, coalesced post-commit local hint."""
        with self._notify_lock:
            loop = self._loop
            if loop is None or loop.is_closed() or self._notify_pending:
                return
            self._notify_pending = True
            try:
                loop.call_soon_threadsafe(self._deliver)
            except RuntimeError:
                self._notify_pending = False

    def _deliver(self):
        with self._notify_lock:
            self._notify_pending = False
        self._invalidate()

    def _invalidate(self):
        self._generation += 1
        event, self._event = self._event, asyncio.Event()
        event.set()

    def _recover(self, queue, expiry):
        wall_now = datetime.now(timezone.utc).timestamp()
        expired = expiry is not None and expiry <= wall_now
        if expired:
            self.recovered_total += queue.reset_stale_running()
        delay = self.fallback_seconds
        if expiry is not None and expiry > wall_now:
            delay = min(delay, expiry - wall_now)
        self._recovery_check_at = time.monotonic() + delay
        return expired

    async def claim(self, queue):
        """Wait for eligible work, without one empty SQL poll per worker."""
        while self._users:
            async with self._lock:
                # Capture BEFORE checking SQL. A subsequent commit changes the
                # generation or sets this exact event, including during sleep.
                event = self._event
                generation = self._generation
                now = time.monotonic()
                if self._draining:
                    # A continuously nonempty backlog must not starve lease
                    # recovery (including summaries blocking another scope).
                    if now >= self._recovery_check_at:
                        _, expiry = queue.next_wakeup()
                        self._recover(queue, expiry)
                    task = queue.claim_next()
                    if task is not None:
                        return task
                    self._draining = False
                    self._checked_generation = -1
                if generation != self._checked_generation or now >= self._check_at:
                    self.last_check_at = datetime.now(timezone.utc).isoformat()
                    due, expiry = queue.next_wakeup()
                    wall_now = datetime.now(timezone.utc).timestamp()
                    if self._recover(queue, expiry):
                        due = wall_now
                    if due is not None and due <= wall_now:
                        task = queue.claim_next()
                        if task is not None:
                            self._draining = True
                            return task
                    # A due row may be locked/claimed by a different process.
                    # Do not spin, nor let an expired cached deadline do so.
                    delays = [self.fallback_seconds]
                    for deadline in (due, expiry):
                        if deadline is not None:
                            delay = deadline - wall_now
                            delays.append(delay if delay > 0 else 1.0)
                    self._check_at = time.monotonic() + min(delays)
                    self._checked_generation = generation
                timeout = max(0.001, self._check_at - time.monotonic())
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        raise asyncio.CancelledError

    async def _listen(self):
        while self._users:
            connection = None
            try:
                connection = self._db.open_analysis_listener()
                self._connection = connection
                if self._db.backend == "postgresql":
                    # Bounded coalescing is sufficient: all messages mean
                    # "re-read the queue", never contain task data.
                    connection.notifies = deque(maxlen=1)
                    await asyncio.wait_for(_pg_ready(connection), 5)
                    cursor = connection.cursor()
                    try:
                        cursor.execute("LISTEN intaris_analysis")
                        await asyncio.wait_for(_pg_ready(connection), 5)
                    finally:
                        cursor.close()
                    self.listener_ready.set()
                    self._invalidate()  # scan after LISTEN is established
                    while self._users:
                        await _fd_ready(connection)
                        connection.poll()
                        if connection.notifies:
                            connection.notifies.clear()
                            self._invalidate()
                else:
                    version = connection.execute("PRAGMA data_version").fetchone()[0]
                    self.listener_ready.set()
                    self._invalidate()
                    while self._users:
                        await asyncio.sleep(self.sqlite_check_seconds)
                        current = connection.execute("PRAGMA data_version").fetchone()[
                            0
                        ]
                        if current != version:
                            version = current
                            self._invalidate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Analysis hints disconnected; using SQL fallback", exc_info=True
                )
                self._invalidate()
            finally:
                self.listener_ready.clear()
                self._connection = None
                if connection is not None:
                    connection.close()
            await asyncio.sleep(1)
