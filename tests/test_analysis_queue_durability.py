"""Analysis queue ownership contract, exercised on both SQL backends."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from intaris.background import TaskQueue


def expire(db, task_id):
    old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    with db.cursor() as cur:
        cur.execute(
            "UPDATE analysis_tasks SET heartbeat_at = ? WHERE id = ?", (old, task_id)
        )


def test_revoked_owner_cannot_touch_complete_or_fail(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    first = queue.claim_next()
    assert queue.reset_stale_running() == 0
    expire(queue_db, task_id)
    assert queue.reset_stale_running() == 1
    second = queue.claim_next()
    assert first["claim_token"] != second["claim_token"]
    assert not queue.touch_heartbeat(task_id, claim_token=first["claim_token"])
    assert not queue.complete(task_id, claim_token=first["claim_token"])
    assert not queue.fail(task_id, "late error", claim_token=first["claim_token"])
    assert queue.touch_heartbeat(task_id, claim_token=second["claim_token"])
    assert queue.complete(task_id, claim_token=second["claim_token"])
    assert not queue.fail(task_id, "late error", claim_token=second["claim_token"])


def test_legacy_running_rows_are_preserved_until_stale(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    queue.claim_next()
    with queue_db.cursor() as cur:
        cur.execute(
            "UPDATE analysis_tasks SET claim_token = NULL WHERE id = ?", (task_id,)
        )
    assert queue.reset_stale_running() == 0
    assert not queue.complete(task_id, claim_token=None)
    expire(queue_db, task_id)
    assert queue.reset_stale_running() == 1
    claimed = queue.claim_next()
    assert claimed["claim_token"]


def test_no_unowned_finalization_api(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    with pytest.raises(TypeError):
        queue.complete(task_id)
    with pytest.raises(TypeError):
        queue.fail(task_id, "failure")
    with pytest.raises(TypeError):
        queue.touch_heartbeat(task_id)
    with pytest.raises(ValueError):
        queue.reset_stale_running(0)


def test_retry_revokes_token_and_delayed_enqueue_is_atomic(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    task = queue.claim_next()
    assert queue.fail(task_id, "retry", claim_token=task["claim_token"])
    assert not queue.complete(task_id, claim_token=task["claim_token"])
    assert queue.claim_next() is None
    queue.enqueue(
        "summary",
        "another-tenant",
        next_attempt_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    )
    assert queue.claim_next() is None


def test_additive_migration_keeps_legacy_claim(queue_db):
    queue = TaskQueue(queue_db)
    task_id = queue.enqueue("analysis", "tenant")
    queue.claim_next()
    with queue_db.cursor() as cur:
        cur.execute("ALTER TABLE analysis_tasks DROP COLUMN claim_token")
    # Reapply the real backend migration without reinitializing the pool.
    queue_db._ensure_tables()
    with queue_db.cursor() as cur:
        cur.execute(
            "SELECT status, claim_token FROM analysis_tasks WHERE id = ?", (task_id,)
        )
        row = cur.fetchone()
    assert row[0] == "running"
    assert row[1] is None
    assert queue.reset_stale_running() == 0
