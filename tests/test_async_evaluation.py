"""Deterministic contention and continuation tests; no external HTTP."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from unittest.mock import Mock

import httpx
import pytest

from intaris.evaluation_flow import BackendSelection, run_async
from intaris.evaluator_backend import (
    CACHE_SECONDS,
    EvaluatorBackend,
    EvaluatorBackendError,
    EvaluatorBackendUnavailable,
)
from tests.test_hybrid_api import api as api
from tests.test_hybrid_runtime import stores as stores


async def wait_for_event(event: threading.Event) -> None:
    """Yield to the loop while a gated worker starts, without using its pool."""
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0.001)


def metadata_backend(handler, **kwargs):
    return EvaluatorBackend(
        "auto",
        jev_factory=Mock,
        models_url="https://cognis.test/v1/models",
        token=lambda: "test-token",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def metadata(protocol="chat"):
    return httpx.Response(
        200,
        json={
            "data": [
                {
                    "id": "routes/intaris_evaluation",
                    "cognis": {"operations": [protocol]},
                }
            ]
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "timeout", "malformed"])
async def test_mixed_singleflight_cancel_and_cached_outcome(failure):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        if failure == "timeout":
            raise httpx.ReadTimeout("bounded transport failure")
        return metadata("unsupported" if failure else "chat")

    backend = metadata_backend(handler, timeout_s=0.2)
    pool = ThreadPoolExecutor(1)
    try:
        owner = asyncio.create_task(backend.select_async())
        await wait_for_event(entered)
        sync = pool.submit(backend.select)
        canceled = asyncio.create_task(backend.select_async())
        survivor = asyncio.create_task(backend.select_async())
        await asyncio.sleep(0)
        # The first async caller has no special refresh ownership.
        owner.cancel()
        canceled.cancel()
        for task in (owner, canceled):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not backend._pending.cancelled()
        assert not sync.done()
        release.set()
        if failure:
            cls = (
                EvaluatorBackendUnavailable
                if failure == "timeout"
                else EvaluatorBackendError
            )
            with pytest.raises(cls) as async_error:
                await survivor
            with pytest.raises(cls) as sync_error:
                sync.result(3)
            with pytest.raises(cls) as cached:
                await backend.select_async()
            assert str(async_error.value) == str(sync_error.value) == str(cached.value)
        else:
            assert await survivor is sync.result(3) is None
            assert await backend.select_async() is None
        assert len(calls) == backend.lookups == 1
        # Metadata keeps its configured HTTP timeout, not the model retry budget.
        assert calls[0].extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), 0.2
        )
    finally:
        release.set()
        pool.shutdown()
        await backend.aclose()
    assert not any(
        t.name.startswith("intaris-metadata")
        for t in backend._executor._threads
        if t.is_alive()
    )


@pytest.mark.asyncio
async def test_expiry_and_invalidation_never_publish_stale_or_overlap():
    now = [100.0]
    entered, release = threading.Event(), threading.Event()
    active = maximum = calls = 0

    def handler(request):
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        calls += 1
        try:
            if calls == 1:
                entered.set()
                assert release.wait(3)
                return metadata("systemone")
            return metadata()
        finally:
            active -= 1

    backend = metadata_backend(handler, clock=lambda: now[0])
    try:
        first = asyncio.create_task(backend.select_async())
        await wait_for_event(entered)
        backend.protocol_mismatch("chat")
        waiters = [asyncio.create_task(backend.select_async()) for _ in range(8)]
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        assert await first is None
        assert await asyncio.gather(*waiters) == [None] * 8
        assert calls == 2
        now[0] += CACHE_SECONDS + 1
        assert (
            await asyncio.gather(*(backend.select_async() for _ in range(8)))
            == [None] * 8
        )
        assert calls == 3
        assert maximum == 1
    finally:
        release.set()
        await backend.aclose()
    with pytest.raises(EvaluatorBackendError, match="closed"):
        await backend.select_async()


@pytest.mark.asyncio
async def test_close_drains_refresh_before_closing_http_and_cannot_restart():
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        return metadata()

    backend = metadata_backend(handler)
    waiting = asyncio.create_task(backend.select_async())
    await wait_for_event(entered)
    closing = asyncio.create_task(backend.aclose())
    try:
        async with asyncio.timeout(3):
            while not backend._closed:
                await asyncio.sleep(0)
        assert not closing.done()
        assert not backend._http.is_closed
        backend.protocol_mismatch("chat")
        with pytest.raises(EvaluatorBackendError, match="closed"):
            await backend.select_async()
        release.set()
        with pytest.raises(EvaluatorBackendError, match="closed"):
            await waiting
        await closing
        assert backend._http.is_closed
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(waiting, closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_queued_segment_never_advances_generator():
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(1))
    entered, release = threading.Event(), threading.Event()
    visits = []

    def blocker():
        entered.set()
        assert release.wait(3)

    def flow():
        visits.append("advanced")
        yield BackendSelection(EvaluatorBackend.fixed(None))

    blocked = asyncio.create_task(asyncio.to_thread(blocker))
    await wait_for_event(entered)
    evaluation = asyncio.create_task(run_async(flow()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    evaluation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await evaluation
    release.set()
    await blocked
    await asyncio.to_thread(lambda: None)  # Drain the queued canceled segment.
    assert visits == []


@pytest.mark.asyncio
async def test_continuation_context_error_site_and_canceled_segment_cleanup():
    from intaris.model_retry import _deadline, evaluation_deadline

    variable = ContextVar("continuation_test", default="outside")
    backend = EvaluatorBackend.fixed(None)
    visited = []

    def flow():
        variable.set("inside")
        with evaluation_deadline(30):
            original = _deadline.get()
            visited.append("prefix")
            assert (yield BackendSelection(backend)) is None
            assert variable.get() == "inside"
            assert _deadline.get() == original
            visited.append("suffix")
            return "result"

    with evaluation_deadline(0.1):
        outer = _deadline.get()
        assert await run_async(flow()) == "result"
        assert _deadline.get() == outer
    assert variable.get() == "outside"
    assert visited == ["prefix", "suffix"]

    entered, release, closed = (threading.Event() for _ in range(3))

    def blocked():
        try:
            entered.set()
            assert release.wait(3)
            yield BackendSelection(backend)
            raise AssertionError("canceled evaluation resumed")
        finally:
            assert release.is_set(), "generator closed concurrently with segment"
            closed.set()

    task = asyncio.create_task(run_async(blocked()))
    await wait_for_event(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not closed.is_set()
    release.set()
    await wait_for_event(closed)


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["sync", "async"])
@pytest.mark.parametrize(
    "site", ["alignment", "hybrid", "legacy_model", "legacy_overlay"]
)
async def test_selection_errors_reach_original_site_handlers(stores, driver, site):
    from intaris.policy_activation import HybridError

    _, sessions, audit, llm, evaluator = stores
    policy = {
        "evaluation_policy_version": "hybrid_v1" if site == "hybrid" else "legacy"
    }
    sessions.create(
        user_id="owner", session_id="parent", intention="Inspect notes", policy=policy
    )
    if site == "legacy_overlay":
        policy.update(maximum_outcome="approve", allow_tools=["service:diagnose"])
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect notes",
        parent_session_id="parent" if site == "alignment" else None,
        policy=policy,
    )
    if site == "alignment":
        barrier = Mock()
        barrier.is_misaligned.return_value = "Child is unrelated"
        evaluator._alignment_barrier = barrier

    def unavailable(request):
        raise httpx.ReadTimeout("transport detail")

    backend = metadata_backend(unavailable)
    evaluator._backend = backend
    request = dict(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="service:diagnose" if site in {"alignment", "legacy_overlay"} else "write",
        args={},
        context={"read_only_required": True}
        if site in {"alignment", "legacy_overlay"}
        else None,
    )
    try:

        async def evaluate():
            if driver == "sync":
                return evaluator.evaluate(**request)
            return await evaluator.evaluate_async(**request)

        if site == "legacy_overlay":
            result = await evaluate()
            assert result["decision"] == "deny"
            assert result["evaluation_metadata"]["stages"] == [
                {"stage": "read_only_l1", "status": "unavailable"}
            ]
            assert sessions.get("work", user_id="owner")["total_calls"] == 1
        else:
            cls = HybridError if site == "alignment" else EvaluatorBackendUnavailable
            with pytest.raises(cls) as failed:
                await evaluate()
            if site == "alignment":
                assert failed.value.code == "assessment_failed"
                assert failed.value.status == 503
                assert isinstance(failed.value.__cause__, EvaluatorBackendUnavailable)
            assert sessions.get("work", user_id="owner")["total_calls"] == 0
        assert backend.lookups == 1
        llm.generate.assert_not_called()
    finally:
        await backend.aclose()


@pytest.mark.parametrize("failure", [False, True])
def test_mounted_real_evaluations_release_event_reader_workers(
    api, monkeypatch, tmp_path, failure
):
    """Four metadata waits must not starve warm events in a four-slot pool."""
    import intaris.server as srv
    from intaris.config import EventStoreConfig
    from intaris.events.store import EventStore
    from intaris.session import SessionStore

    client, version, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    for number in range(5):
        client.post(
            "/api/v1/intention",
            json={
                "session_id": f"contended-{number}",
                "intention": "Update local docs",
            },
        ).raise_for_status()
    evaluator = srv._get_evaluator()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        if failure:
            raise httpx.ReadTimeout("metadata unavailable")
        return metadata()

    backend = metadata_backend(handler)
    monkeypatch.setattr(evaluator, "_backend", backend)
    store = EventStore(
        EventStoreConfig(backend="filesystem", filesystem_path=str(tmp_path / "events"))
    )
    store.set_session_store(SessionStore(srv._get_db()))
    client.app.state._api_app.state.event_store = store
    activity = Mock(wraps=evaluator._sessions.update_activity)
    monkeypatch.setattr(evaluator._sessions, "update_activity", activity)
    sync_pool = ThreadPoolExecutor(1)
    sync = None

    async def scenario():
        nonlocal sync
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(4))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            headers=dict(client.headers),
        ) as http:
            url = "/api/v1/session/contended-0/events"
            baseline = await http.get(url)
            baseline.raise_for_status()
            evaluations = [
                asyncio.create_task(
                    http.post(
                        "/api/v1/evaluate",
                        json={
                            "session_id": f"contended-{number}",
                            "tool": "write",
                            "args": {"file_path": "/repo/docs.md", "content": "Update"},
                        },
                    )
                )
                for number in range(4)
            ]
            sync = sync_pool.submit(
                evaluator.evaluate,
                user_id="owner",
                session_id="contended-4",
                agent_id="hook",
                tool="write",
                args={"file_path": "/repo/docs.md", "content": "Update"},
            )
            try:
                await wait_for_event(entered)
                # Let every synchronous prefix reach its original selection site.
                await asyncio.sleep(0.03)
                start = time.perf_counter()
                response = await asyncio.wait_for(http.get(url), 0.5)
                elapsed = (time.perf_counter() - start) * 1000
                response.raise_for_status()
                assert response.json() == baseline.json()
                assert not release.is_set()
                assert all(not task.done() for task in evaluations)
                # Cancellation at metadata wait must not audit or run a suffix.
                evaluations[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await evaluations[0]
            finally:
                release.set()
            responses = await asyncio.gather(*evaluations[1:])
            assert [r.status_code for r in responses] == [
                503 if failure else 200
            ] * 3, [r.text for r in responses]
            if failure:
                assert all(r.headers["Retry-After"] == "5" for r in responses)
                assert all(
                    r.json()["detail"]["code"] == "evaluation_backend_unavailable"
                    for r in responses
                )
                with pytest.raises(EvaluatorBackendUnavailable):
                    sync.result(3)
            else:
                assert all(r.json()["decision"] == "approve" for r in responses)
                assert sync.result(3)["decision"] == "approve"
            return elapsed

    try:
        elapsed = client.portal.call(scenario)
        assert len(calls) == 1
        sessions = SessionStore(srv._get_db())
        assert sessions.get("contended-0", user_id="owner")["total_calls"] == 0
        for number in range(1, 5):
            assert sessions.get(f"contended-{number}", user_id="owner")[
                "total_calls"
            ] == (0 if failure else 1)
        assert activity.call_count == 5
        print(f"mounted {version}: event GET during metadata wait {elapsed:.3f}ms")
    finally:
        release.set()
        sync_pool.shutdown()
        backend.close()


def test_mounted_metadata_independent_fast_paths(api, monkeypatch):
    import intaris.server as srv

    client, version, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    client.post(
        "/api/v1/intention", json={"session_id": "fast", "intention": "Read docs"}
    ).raise_for_status()
    backend = metadata_backend(
        lambda r: (_ for _ in ()).throw(AssertionError("unexpected metadata"))
    )
    monkeypatch.setattr(srv._get_evaluator(), "_backend", backend)
    try:
        if version == "legacy":
            result = client.post(
                "/api/v1/evaluate",
                json={
                    "session_id": "fast",
                    "tool": "read",
                    "args": {"file_path": "/repo/docs.md"},
                },
            )
            result.raise_for_status()
            assert result.json()["decision"] == "approve"
        client.patch(
            "/api/v1/session/fast/status", json={"status": "terminated"}
        ).raise_for_status()
        result = client.post(
            "/api/v1/evaluate",
            json={
                "session_id": "fast",
                "tool": "write",
                "args": {},
            },
        )
        result.raise_for_status()
        assert result.json()["decision"] == "deny"
        assert backend.lookups == 0
        assert backend._executor is None
    finally:
        backend.close()
