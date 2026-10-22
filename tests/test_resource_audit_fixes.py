"""Regression tests for resource fixes without external inference or services."""

import os
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock
from unittest.mock import Mock
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from intaris.metrics import RuntimeMetrics
from intaris.server import APIKeyMiddleware, PerformanceMetricsMiddleware
from intaris.task_context import TaskContext


@pytest.mark.parametrize("status", [200, 401, 500])
def test_mounted_metrics_keep_owner_and_bounded_template(monkeypatch, status):
    from intaris import server
    from intaris.config import Config

    cfg = Config()
    cfg.server.api_key = "audit-test-key"
    monkeypatch.setattr(server, "_get_config", lambda: cfg)

    async def endpoint(request):
        if status == 500:
            raise RuntimeError("endpoint failure")
        return JSONResponse({"ok": True})

    child = Starlette(routes=[Route("/session/{session_id}", endpoint)])
    app = Starlette(
        routes=[Mount("/api/v1", app=child)],
        middleware=[
            Middleware(PerformanceMetricsMiddleware),
            Middleware(APIKeyMiddleware),
        ],
    )
    app.state.runtime_metrics = RuntimeMetrics()
    with TestClient(app, raise_server_exceptions=False) as client:
        for session_id in ("first-private-id", "second-private-id"):
            response = client.get(
                f"/api/v1/session/{session_id}",
                headers={"X-API-Key": "audit-test-key"} if status != 401 else {},
            )
            assert response.status_code == status
    http = app.state.runtime_metrics.snapshot()["http"]
    assert list(http) == [f"GET /api/v1/session/{{session_id}} {status // 100}xx"]
    assert next(iter(http.values()))["requests_total"] == 2


def _context():
    context = TaskContext.__new__(TaskContext)
    context._lock = Lock()
    context._pending = {}
    context._cache = OrderedDict()
    context.capacity = 4
    context.generation = "spans"
    context.sessions = Mock()
    context.collect = Mock(return_value={"source_digest": "digest"})
    context._extract = Mock(
        return_value={"status": "ready", "extraction": {"calls": 1}}
    )
    return context


@pytest.mark.parametrize("stage", ["extract", "freshness_read", "freshness_collect"])
def test_failed_flight_released_and_retry_succeeds(stage):
    context = _context()
    if stage == "extract":
        context._extract.side_effect = RuntimeError("extract failed")
    elif stage == "freshness_read":
        context.sessions.get.side_effect = RuntimeError("read failed")
    else:
        context.collect.side_effect = [
            {"source_digest": "digest"},
            RuntimeError("collect failed"),
        ]
    session = {"session_id": "s", "user_id": "u"}
    with pytest.raises(RuntimeError):
        context.capture(session)
    assert context._pending == {}
    assert context._cache == {}
    context._extract.side_effect = None
    context.sessions.get.side_effect = None
    context.collect.side_effect = None
    assert context.capture(session)["cache"] == "cold"
    assert context.capture(session)["cache"] == "hit"
    assert context._pending == {}


def test_late_failed_waiter_cannot_remove_new_owner(monkeypatch):
    import intaris.task_context as module

    context = _context()
    started, release, joined, waiter_release = (Event() for _ in range(4))

    class ControlledFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                joined.set()
                assert waiter_release.wait(3)
            return super().result(timeout)

    monkeypatch.setattr(module, "Future", ControlledFuture)

    def fail(snapshot):
        started.set()
        assert release.wait(3)
        raise RuntimeError("extraction failed")

    context._extract.side_effect = fail
    session = {"session_id": "s", "user_id": "u"}
    with ThreadPoolExecutor(2) as pool:
        owner = pool.submit(context.capture, session)
        assert started.wait(3)
        waiter = pool.submit(context.capture, session)
        assert joined.wait(3)
        release.set()
        with pytest.raises(RuntimeError):
            owner.result(3)
        assert context._pending == {}
        replacement = Future()
        context._pending["digest"] = replacement
        waiter_release.set()
        with pytest.raises(RuntimeError):
            waiter.result(3)
        assert context._pending["digest"] is replacement


def test_under_budget_cache_write_does_not_iterate_registry(tmp_path):
    from intaris.events.cache import FilesystemEventChunkCache

    class NoIteration(OrderedDict):
        def __iter__(self):
            raise AssertionError("write traversed whole cache")

    cache = FilesystemEventChunkCache(
        str(tmp_path),
        max_bytes=100000,
        ttl_seconds=3600,
        touch_interval_seconds=10,
        sweep_interval_seconds=60,
    )
    cache._entries = NoIteration()
    for number in range(100):
        cache.put(str(number), b"event")
    assert cache.metrics()["resident_entries"] == 100


@pytest.fixture(params=["sqlite", "postgresql"])
def audit_db(request, tmp_path):
    from intaris.config import DBConfig
    from intaris.db import Database

    url = os.environ.get("TEST_HYBRID_POSTGRES_URL")
    if request.param == "postgresql" and not url:
        pytest.skip("Isolated PostgreSQL URL not configured")
    db = Database(
        DBConfig(
            backend=request.param,
            path=str(tmp_path / "audit.db"),
            database_url=url or "",
        )
    )
    yield db
    db.close()


def test_projection_batches_preserve_duplicates_gaps_and_atomicity(
    audit_db, monkeypatch
):
    from intaris.event_audit import EventAuditStore
    from intaris.session import SessionStore

    user = str(uuid4())
    SessionStore(audit_db).create(user_id=user, session_id="s", intention="Audit test")
    store = EventAuditStore(audit_db)
    events = [
        {
            "seq": seq,
            "ts": "2026-01-01T00:00:00+00:00",
            "type": "tool_call",
            "source": "test",
            "data": {"name": f"tool-{seq}"},
        }
        for seq in range(1, 162)
    ]
    cursor = audit_db.cursor
    inserts = []

    @contextmanager
    def failing_cursor():
        with cursor() as cur:
            execute = cur.execute

            def counted(sql, params=None):
                result = execute(sql, params)
                if "INSERT INTO audit_event_index" in sql:
                    inserts.append(len(params))
                    if len(inserts) == 2:
                        raise RuntimeError("simulated failure after second batch")
                return result

            cur.execute = counted
            yield cur

    with monkeypatch.context() as patch:
        patch.setattr(audit_db, "cursor", failing_cursor)
        with pytest.raises(RuntimeError):
            store.index_events(user_id=user, session_id="s", events=events)
    assert inserts == [880, 880]
    assert store.query(user_id=user, source="events")["total"] == 0
    assert (
        next(s for s in store.projection_sessions() if s["user_id"] == user)["last_seq"]
        == 0
    )
    before = audit_db.metrics()["query_latency"]["count"]
    assert store.index_events(user_id=user, session_id="s", events=events) == 161
    assert audit_db.metrics()["query_latency"]["count"] - before == 5
    assert store.index_events(user_id=user, session_id="s", events=events) == 161
    assert store.query(user_id=user, source="events")["total"] == 161
    # A gap cannot advance the contiguous checkpoint.
    assert (
        store.index_events(
            user_id=user, session_id="s", events=[{**events[0], "seq": 163}]
        )
        == 161
    )


def test_long_legacy_compactions_do_not_hide_windows_with_unknown_coverage(audit_db):
    import json

    from intaris.analyzer import _get_session_summaries_for_analysis
    from intaris.session import SessionStore
    from tests.test_analyzer import _insert_summary

    user = str(uuid4())
    sessions = SessionStore(audit_db)
    for sid in ("compacted", "windows"):
        sessions.create(user_id=user, session_id=sid, intention="Summary parity")
        for index in range(120):
            _insert_summary(
                audit_db,
                sid,
                user_id=user,
                window_start=f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00",
            )
    for day in (2, 4):
        _insert_summary(
            audit_db,
            "compacted",
            user_id=user,
            summary_type="compacted",
            window_start=f"2026-01-{day:02d}T00:00:00",
        )
    with audit_db.cursor() as cur:
        cur.execute(
            """SELECT id, created_at, evidence_metadata, session_id, summary_type, summary, intent_alignment,
                      risk_indicators, tools_used, call_count, approved_count,
                      denied_count, escalated_count, window_start, window_end, trigger
               FROM session_summaries WHERE user_id = ? ORDER BY window_start ASC""",
            (user,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    expected = {}
    for sid in ("compacted", "windows"):
        selected = [row for row in rows if row["session_id"] == sid]
        expected[sid] = [row for row in selected if row["summary_type"] == "window"]
        for row in expected[sid]:
            for field in ("risk_indicators", "tools_used"):
                row[field] = json.loads(row[field])
    result = _get_session_summaries_for_analysis(audit_db, user, None, 30)
    assert {sid: value["summaries"] for sid, value in result.items()} == expected
    assert sum(len(value["summaries"]) for value in result.values()) == 240


def test_s3_cache_returns_immutable_snapshot_and_counts_chunks():
    from tests.test_events import TestS3EventBackendCache

    client = TestS3EventBackendCache.FakeClient()
    client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
    backend = TestS3EventBackendCache.backend(client)
    snapshot = backend._list_chunks("alice", "session")
    assert isinstance(snapshot, tuple)
    assert backend._list_chunks("alice", "session") is snapshot
    metrics = backend.metrics()
    assert metrics["chunk_cache_chunks"] == 1
    assert "chunk_cache_estimated_bytes" not in metrics
    backend.append(
        "alice",
        "session",
        [
            {"seq": 2, "ts": "t", "type": "message", "data": {}},
        ],
    )
    assert len(snapshot) == 1
    assert len(backend._list_chunks("alice", "session")) == 2
    assert backend.metrics()["chunk_cache_chunks"] == 2
    backend._delete_by_prefix = Mock()
    backend.delete_session("alice", "session")
    assert backend.metrics()["chunk_cache_chunks"] == 0
    assert "chunk_cache_estimated_bytes" not in backend.metrics()


def test_chunk_count_tracks_refresh_replacement_eviction_and_user_deletion():
    from tests.test_events import TestS3EventBackendCache

    client = TestS3EventBackendCache.FakeClient()
    client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
    backend = TestS3EventBackendCache.backend(client)
    backend._delete_by_prefix = Mock()
    backend._list_chunks("alice", "session")
    backend._list_chunks("alice", "session", force_refresh=True)
    assert backend.metrics()["chunk_cache_chunks"] == 1
    event = {"seq": 1, "ts": "t", "type": "message", "data": {}}
    backend.append("alice", "session", [event])
    assert backend.metrics()["chunk_cache_chunks"] == 1
    client.objects = [
        "events/alice/session/seq_000001_000001.ndjson",
        "events/alice/session/seq_000002_000002.ndjson",
        "events/bob/session/seq_000001_000001.ndjson",
    ]
    backend._list_chunks("alice", "session", force_refresh=True)
    backend._list_chunks("bob", "session")
    assert backend.metrics()["chunk_cache_chunks"] == 3
    backend._cache_max_entries = 1
    with backend._cache_lock:
        backend._trim_caches_locked()
    assert backend.metrics()["chunk_cache_chunks"] == 1
    backend.delete_all_for_user("alice")
    assert backend.metrics()["chunk_cache_chunks"] == 1
    backend.delete_all_for_user("bob")
    assert backend.metrics()["chunk_cache_chunks"] == 0


def test_chunk_count_scrape_does_not_traverse_cache():
    from tests.test_events import TestS3EventBackendCache

    class NoTraversal(dict):
        def values(self):
            raise AssertionError("metrics traversed chunk cache")

    backend = TestS3EventBackendCache.backend(TestS3EventBackendCache.FakeClient())
    backend._chunk_cache = NoTraversal()
    assert backend.metrics()["chunk_cache_chunks"] == 0


def test_concurrent_index_refresh_counts_only_published_snapshot():
    from threading import Barrier

    from tests.test_events import TestS3EventBackendCache

    barrier = Barrier(4)

    class ConcurrentClient(TestS3EventBackendCache.FakeClient):
        def list_objects_v2(self, **kwargs):
            barrier.wait(timeout=5)
            return super().list_objects_v2(**kwargs)

    client = ConcurrentClient()
    client.objects = ["events/alice/session/seq_000001_000001.ndjson"]
    backend = TestS3EventBackendCache.backend(client)
    with ThreadPoolExecutor(4) as pool:
        snapshots = list(
            pool.map(
                lambda _: backend._list_chunks("alice", "session", force_refresh=True),
                range(4),
            )
        )
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert backend.metrics()["chunk_cache_chunks"] == 1


@pytest.mark.asyncio
async def test_queue_metrics_have_one_sampler_and_keep_ten_second_cadence(
    monkeypatch, audit_db
):
    from intaris.background import BackgroundWorker
    from intaris.config import AnalysisConfig

    worker = BackgroundWorker(audit_db, AnalysisConfig(), task_queue=Mock())
    worker._task_queue.get_queue_stats.return_value = {"pending": 7}
    worker._running = True
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            worker._running = False

    monkeypatch.setattr("intaris.background.asyncio.sleep", sleep)
    await worker._queue_metrics_loop()
    assert sleeps == [10, 10, 10]
    assert worker._task_queue.get_queue_stats.call_count == 3
    assert worker.metrics.task_queue_depth == 7
    worker._task_queue.claim_next.assert_not_called()


def test_freshness_mismatch_releases_owner_without_caching():
    from intaris.policy_activation import HybridError

    context = _context()
    context.collect.side_effect = [
        {"source_digest": "digest"},
        {"source_digest": "new-digest"},
    ]
    with pytest.raises(HybridError):
        context.capture({"session_id": "s", "user_id": "u"})
    assert context._pending == {}
    assert context._cache == {}


def test_sqlite_uncompacted_summary_scan_scales_linearly(tmp_path):
    from intaris.analyzer import _get_session_summaries_for_analysis
    from intaris.config import DBConfig
    from intaris.db import Database
    from intaris.session import SessionStore
    from tests.test_analyzer import _insert_summary

    db = Database(DBConfig(backend="sqlite", path=str(tmp_path / "scaling.db")))
    user = "scaling-user"
    SessionStore(db).create(user_id=user, session_id="s", intention="Scaling check")
    counts = []
    queries = []
    try:
        for size in (100, 400):
            for _ in range(size - (100 if counts else 0)):
                _insert_summary(db, "s", user_id=user)
            ticks = 0

            def progress():
                nonlocal ticks
                ticks += 1
                return 0

            with db.connection() as conn:
                conn.set_progress_handler(progress, 100)
                conn.set_trace_callback(queries.append)
                try:
                    result = _get_session_summaries_for_analysis(db, user, None, 30)
                finally:
                    conn.set_progress_handler(None, 0)
                    conn.set_trace_callback(None)
            assert len(result["s"]["summaries"]) == size
            counts.append(ticks)
        # Four times the history must not perform sixteen times the SQL work.
        assert counts[1] < counts[0] * 5
        query = next(q for q in queries if "FROM session_summaries AS selected" in q)
        with db.connection() as conn:
            plan = list(conn.execute("EXPLAIN QUERY PLAN " + query))
        assert not any("CORRELATED" in row[3] for row in plan)
        # Current evidence selection reads all rows once before choosing coverage;
        # its indexed, non-correlated scan must retain linear scaling.
        assert any("SEARCH selected" in row[3] for row in plan)
    finally:
        db.close()
