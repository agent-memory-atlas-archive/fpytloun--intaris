"""Queue sampling stays off-loop, single-flight and cancellation-safe."""

import asyncio
import threading
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from intaris.background import BackgroundWorker, TaskQueue
from intaris.config import AnalysisConfig, DBConfig
from intaris.db import Database
from intaris.queue_wakeup import AnalysisWakeup
from tests.test_async_evaluation import wait_for_event


@pytest.mark.asyncio
async def test_stats_cursor_and_connection_owned_by_worker(tmp_path, monkeypatch):
    db = Database(DBConfig(path=str(tmp_path / "queue.db")))
    queue = TaskQueue(db)
    worker = BackgroundWorker(db, AnalysisConfig(), task_queue=queue)
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    owners = []
    cursor = db.cursor

    @contextmanager
    def measured():
        owners.append(threading.get_ident())
        with cursor() as cur:
            entered.set()
            assert release.wait(3)
            yield cur
        owners.append(threading.get_ident())

    monkeypatch.setattr(db, "cursor", measured)
    worker._running = True
    task = asyncio.create_task(worker._queue_metrics_loop())
    try:
        await wait_for_event(entered)
        assert owners[0] != loop_thread
        # A loop callback and another executor operation run while DB is gated.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 17), 0.5) == 17
        release.set()
        await asyncio.wait_for(asyncio.shield(worker._queue_stats_task), 3)
        await asyncio.sleep(0)
        assert owners == [owners[0], owners[0]]
        assert worker.metrics.task_queue_depth == 0
    finally:
        release.set()
        worker._tasks = [task]
        await worker.stop()
        db.close()


@pytest.mark.asyncio
async def test_canceled_sampler_restart_joins_query_and_stop_does_not_publish():
    entered, release = threading.Event(), threading.Event()
    queue = Mock()
    calls = 0

    def stats():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(3)
        return {"pending": 29}

    queue.get_queue_stats.side_effect = stats
    db = Mock()
    db.analysis_wakeup.return_value = AnalysisWakeup(db)
    worker = BackgroundWorker(db, AnalysisConfig(), task_queue=queue)
    worker._running = True
    first = asyncio.create_task(worker._queue_metrics_loop())
    await wait_for_event(entered)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    pending = worker._queue_stats_task
    assert not pending.done()
    replacement = asyncio.create_task(worker._queue_metrics_loop())
    await asyncio.sleep(0)
    assert worker._queue_stats_task is pending
    assert calls == 1
    worker._tasks = [replacement]
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.01)
    assert not stopping.done()
    release.set()
    await stopping
    assert worker.metrics.task_queue_depth == 0
    assert worker._queue_stats_task is None
    assert calls == 1
    assert worker._tasks == []


@pytest.mark.asyncio
async def test_stats_failure_uses_resilient_restart_without_duplicate_query(
    monkeypatch,
):
    queue = Mock()
    queue.get_queue_stats.side_effect = [RuntimeError("DB busy"), {"pending": 9}]
    db = Mock()
    db.analysis_wakeup.return_value = AnalysisWakeup(db)
    worker = BackgroundWorker(db, AnalysisConfig(), task_queue=queue)
    worker._running = True
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        if delay == 10:
            worker._running = False

    monkeypatch.setattr("intaris.background.asyncio.sleep", sleep)
    await worker._resilient_loop("queue_metrics", worker._queue_metrics_loop)
    assert sleeps == [5, 10]
    assert queue.get_queue_stats.call_count == 2
    assert worker.metrics.task_queue_depth == 9
    assert worker._queue_stats_task is None
