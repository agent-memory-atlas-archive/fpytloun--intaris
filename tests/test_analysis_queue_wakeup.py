"""Cross-process and lifecycle tests using real SQLite/PostgreSQL connections."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from datetime import datetime, timedelta, timezone

import pytest

from intaris.background import TaskQueue
from intaris.config import DBConfig
from intaris.db import Database


def _producer(config, pipe):
    db = Database(config)
    queue = TaskQueue(db)
    pipe.send("ready")
    try:
        while True:
            command = pipe.recv()
            if command == "stop":
                break
            task_id = queue.enqueue("analysis", "tenant", agent_id="")
            pipe.send(task_id)
    finally:
        db.close()
        pipe.close()


def _claimer(config, pipe):
    db = Database(config)
    queue = TaskQueue(db)
    pipe.send("ready")
    pipe.recv()
    task = queue.claim_next()
    pipe.send(task)
    if task:
        pipe.recv()
        pipe.send(queue.complete(task["id"], claim_token=task["claim_token"]))
    db.close()
    pipe.close()


def _config(db):
    config = DBConfig()
    config.backend = db.backend
    config.path = db._path
    config.database_url = db._database_url
    return config


async def _receive(pipe):
    async with asyncio.timeout(30):
        while not pipe.poll():
            await asyncio.sleep(0.01)
        return pipe.recv()


async def _ready(wakeup):
    await asyncio.wait_for(wakeup.listener_ready.wait(), 30)


@pytest.mark.asyncio
async def test_cross_process_commit_and_lost_hint_recovery(queue_db):
    queue = TaskQueue(queue_db)
    wakeup = queue_db.analysis_wakeup()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_producer, args=(_config(queue_db), child))
    process.start()
    child.close()
    await wakeup.start()
    try:
        assert await _receive(parent) == "ready"
        await _ready(wakeup)
        for lose_hint in (False, True):
            if lose_hint:
                wakeup.fallback_seconds = 0.5
                wakeup._listener.cancel()
                await asyncio.gather(wakeup._listener, return_exceptions=True)
                assert wakeup._connection is None
                wakeup._invalidate()
            pending = asyncio.create_task(wakeup.claim(queue))
            await asyncio.sleep(0.05)
            parent.send("enqueue")
            task_id = await _receive(parent)
            # Healthy hints must beat the ten-second SQL fallback. Lost hints
            # separately exercise the accelerated fallback recovery.
            task = await asyncio.wait_for(pending, 3)
            assert task["id"] == task_id
            assert queue.complete(task_id, claim_token=task["claim_token"])
        parent.send("stop")
    finally:
        await wakeup.stop()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert process.exitcode == 0
    assert wakeup._connection is None
    assert wakeup._listener is None


@pytest.mark.asyncio
async def test_continuous_backlog_cannot_starve_expired_summary(queue_db):
    queue = TaskQueue(queue_db)
    stale_id = queue.enqueue("summary", "blocked")
    queue.claim_next()
    for i in range(30):
        queue.enqueue("analysis", "unrelated", agent_id=str(i), priority=10)
    wakeup = queue_db.analysis_wakeup()
    wakeup.fallback_seconds = 0.05
    await wakeup.start()
    try:
        first = await wakeup.claim(queue)
        queue.complete(first["id"], claim_token=first["claim_token"])
        assert wakeup._draining
        with queue_db.cursor() as cur:
            cur.execute(
                "UPDATE analysis_tasks SET heartbeat_at = ? WHERE id = ?",
                (
                    (
                        datetime.now(timezone.utc) - timedelta(seconds=599.85)
                    ).isoformat(),
                    stale_id,
                ),
            )
        for _ in range(20):
            task = await wakeup.claim(queue)
            assert task["id"] != stale_id  # higher-priority backlog stays nonempty
            queue.complete(task["id"], claim_token=task["claim_token"])
            await asyncio.sleep(0.02)
        assert wakeup.recovered_total == 1
        with queue_db.cursor() as cur:
            cur.execute(
                "SELECT status, claim_token FROM analysis_tasks WHERE id = ?",
                (stale_id,),
            )
            row = cur.fetchone()
        assert row[0] == "pending"
        assert row[1] is None
    finally:
        await wakeup.stop()


@pytest.mark.asyncio
async def test_two_process_claim_and_revoked_completion(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_claimer, args=(_config(queue_db), child))
    process.start()
    child.close()
    try:
        assert await _receive(parent) == "ready"
        parent.send("claim")
        first = await _receive(parent)
        assert first["id"] == task_id
        assert queue.claim_next() is None
        assert queue.reset_stale_running() == 0
        with queue_db.cursor() as cur:
            cur.execute(
                "UPDATE analysis_tasks SET heartbeat_at = ? WHERE id = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),
                    task_id,
                ),
            )
        assert queue.reset_stale_running() == 1
        second = queue.claim_next()
        parent.send("complete")
        assert await _receive(parent) is False
        assert queue.complete(task_id, claim_token=second["claim_token"])
    finally:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert process.exitcode == 0


@pytest.mark.asyncio
async def test_rollback_has_no_hint_or_work(queue_db):
    wakeup = queue_db.analysis_wakeup()
    await wakeup.start()
    try:
        await _ready(wakeup)
        generation = wakeup._generation
        with pytest.raises(RuntimeError):
            with queue_db.cursor(notify_analysis=True) as cur:
                cur.execute(
                    "INSERT INTO analysis_tasks "
                    "(id, task_type, user_id, status, next_attempt_at, created_at) "
                    "VALUES (?, 'analysis', 'tenant', 'pending', ?, ?)",
                    (
                        "rolled-back-task",
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                raise RuntimeError("abort")
        await asyncio.sleep(1.1)
        assert wakeup._generation == generation
        assert TaskQueue(queue_db).claim_next() is None
    finally:
        await wakeup.stop()


@pytest.mark.asyncio
async def test_deadline_dependency_and_shared_waiters(queue_db):
    queue = TaskQueue(queue_db)
    wakeup = queue_db.analysis_wakeup()
    await wakeup.start()
    tasks = []
    try:
        # Two waiting workers share the empty scan/deadline cache.
        await _ready(wakeup)
        before = queue_db.metrics()["transaction_latency"]["count"]
        tasks = [asyncio.create_task(wakeup.claim(queue)) for _ in range(2)]
        await asyncio.sleep(0.1)
        assert queue_db.metrics()["transaction_latency"]["count"] - before == 1
        deadline = datetime.now(timezone.utc) + timedelta(seconds=0.3)
        summary = queue.enqueue(
            "summary", "tenant", next_attempt_at=deadline.isoformat()
        )
        done, waiting = await asyncio.wait(
            tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(done) == 1
        first = done.pop().result()
        remaining = waiting.pop()
        assert first["id"] == summary
        assert datetime.now(timezone.utc) >= deadline
        analysis = queue.enqueue("analysis", "tenant")
        await asyncio.sleep(0.1)
        assert not remaining.done()  # blocked by running same-scope summary
        assert queue.complete(summary, claim_token=first["claim_token"])
        second = await asyncio.wait_for(remaining, 5)
        assert second["id"] == analysis
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await wakeup.stop()


@pytest.mark.asyncio
async def test_lease_expiry_restart_cancel_and_reconnect(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    first = queue.claim_next()
    # A crashed worker's last heartbeat expires shortly; no new enqueue.
    with queue_db.cursor() as cur:
        cur.execute(
            "UPDATE analysis_tasks SET heartbeat_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=599.7)).isoformat(),
                task_id,
            ),
        )
    wakeup = queue_db.analysis_wakeup()
    await wakeup.start()
    try:
        second = await asyncio.wait_for(wakeup.claim(queue), 5)
        assert second["claim_token"] != first["claim_token"]
        assert wakeup.recovered_total == 1
        assert queue.complete(task_id, claim_token=second["claim_token"])
        waiter = asyncio.create_task(wakeup.claim(queue))
        await asyncio.sleep(0.05)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        if queue_db.backend == "postgresql":
            await _ready(wakeup)
            connection = wakeup._connection
            pid = connection.get_backend_pid()
            with queue_db.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(?)", (pid,))
            async with asyncio.timeout(10):
                while wakeup._connection is connection or wakeup._connection is None:
                    await asyncio.sleep(0.02)
        await wakeup.stop()
        assert wakeup._connection is None
        await wakeup.start()
        task_id = queue.enqueue("analysis", "tenant")
        assert (await asyncio.wait_for(wakeup.claim(queue), 5))["id"] == task_id
    finally:
        await wakeup.stop()


@pytest.mark.asyncio
async def test_generation_during_check_does_not_sleep(queue_db):
    queue = TaskQueue(queue_db)
    wakeup = queue_db.analysis_wakeup()
    await wakeup.start()
    original = queue.next_wakeup

    def race():
        result = original()
        queue.enqueue("analysis", "tenant")
        wakeup._invalidate()  # committed hint between snapshot and sleep
        queue.next_wakeup = original
        return result

    queue.next_wakeup = race
    try:
        started = time.monotonic()
        task = await asyncio.wait_for(wakeup.claim(queue), 5)
        assert task["user_id"] == "tenant"
        assert time.monotonic() - started < 5
    finally:
        await wakeup.stop()


@pytest.mark.asyncio
async def test_retry_backoff_and_notification_coalescing(queue_db, monkeypatch):
    monkeypatch.setattr("intaris.background._RETRY_BACKOFF", [0.2])
    queue = TaskQueue(queue_db)
    wakeup = queue_db.analysis_wakeup()
    await wakeup.start()
    try:
        task_id = queue.enqueue("analysis", "tenant")
        first = await wakeup.claim(queue)
        assert queue.fail(task_id, "temporary", claim_token=first["claim_token"])
        for _ in range(1000):
            wakeup.notify()
        with queue_db.cursor() as cur:
            cur.execute(
                "SELECT next_attempt_at FROM analysis_tasks WHERE id = ?", (task_id,)
            )
            deadline = datetime.fromisoformat(str(cur.fetchone()[0])).timestamp()
        second = await asyncio.wait_for(wakeup.claim(queue), 5)
        assert second["id"] == task_id
        assert second["retry_count"] == 1
        assert datetime.now(timezone.utc).timestamp() >= deadline
    finally:
        await wakeup.stop()


@pytest.mark.asyncio
async def test_crashed_process_lease_recovery(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_claimer, args=(_config(queue_db), child))
    process.start()
    child.close()
    wakeup = queue_db.analysis_wakeup()
    try:
        assert await _receive(parent) == "ready"
        parent.send("claim")
        first = await _receive(parent)
        process.terminate()
        process.join(5)
        assert not process.is_alive()
        assert queue.reset_stale_running() == 0
        with queue_db.cursor() as cur:
            cur.execute(
                "UPDATE analysis_tasks SET heartbeat_at = ? WHERE id = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=599.8)).isoformat(),
                    task_id,
                ),
            )
        await wakeup.start()
        second = await asyncio.wait_for(wakeup.claim(queue), 5)
        assert second["id"] == task_id
        assert second["claim_token"] != first["claim_token"]
    finally:
        await wakeup.stop()
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
