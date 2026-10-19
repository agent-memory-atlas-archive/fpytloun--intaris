"""Matched mounted-app contention probes with local synthetic S3/metadata.

The synchronous arms reproduce the previous scheduling boundaries on the same
tree. These are not production attribution or throughput benchmarks.
"""

import asyncio
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx

from intaris.background import BackgroundWorker
from intaris.config import AnalysisConfig, EventStoreConfig
from intaris.events.cache import FilesystemEventChunkCache
from intaris.events.store import EventStore
from tests.test_async_evaluation import metadata_backend, wait_for_event
from tests.test_events import TestS3EventBackendCache as S3CacheFixture
from tests.test_hybrid_api import api as api


class LocalS3:
    """Same 1,000 immutable chunks for each scheduling arm."""

    def __init__(self):
        self.objects = {
            f"events/owner/latency/seq_{n:06d}_{n:06d}.ndjson": (
                json.dumps(
                    {
                        "seq": n,
                        "ts": "2026-01-01T00:00:00Z",
                        "type": "message",
                        "data": {"content": "x" * 256},
                    }
                )
                + "\n"
            ).encode()
            for n in range(1, 1001)
        }

    def list_objects_v2(self, **kwargs):
        return {
            "Contents": [{"Key": k, "ETag": '"v1"'} for k in self.objects],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def test_mounted_paired_contention_latency(api, monkeypatch, tmp_path):
    import intaris.server as srv
    from intaris.session import SessionStore

    client, version, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    client.post(
        "/api/v1/intention",
        json={
            "session_id": "latency",
            "intention": "Update project documentation",
        },
    ).raise_for_status()
    sessions = SessionStore(srv._get_db())
    sessions.allocate_event_sequences(
        "latency",
        user_id="owner",
        user_message_flags=[False] * 1000,
    )
    store = EventStore(
        EventStoreConfig(
            backend="filesystem",
            filesystem_path=str(tmp_path / "events"),
        )
    )
    store._backend = S3CacheFixture.backend(
        LocalS3(),
        FilesystemEventChunkCache(
            str(tmp_path / "cache"),
            max_bytes=100_000_000,
            ttl_seconds=3600,
            touch_interval_seconds=60,
            sweep_interval_seconds=60,
        ),
    )
    store.set_session_store(sessions)
    client.app.state._api_app.state.event_store = store
    evaluator = srv._get_evaluator()
    async_driver = evaluator.evaluate_async

    async def old_driver(**kwargs):
        return await asyncio.to_thread(evaluator.evaluate, **kwargs)

    async def scenario():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(4))
        results = {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            headers=dict(client.headers),
        ) as http:
            url = "/api/v1/session/latency/events?after_seq=980&limit=20"
            baseline = await http.get(url)
            baseline.raise_for_status()
            assert len(baseline.json()["events"]) == 20
            for arm in ("sync_wait", "async_wait", "sync_stats", "async_stats"):
                entered, release = threading.Event(), threading.Event()

                def handler(request):
                    entered.set()
                    assert release.wait(3)
                    raise httpx.ReadTimeout("local delayed metadata")

                backend = metadata_backend(handler)
                monkeypatch.setattr(evaluator, "_backend", backend)
                monkeypatch.setattr(
                    evaluator,
                    "evaluate_async",
                    old_driver if arm == "sync_wait" else async_driver,
                )
                evaluations, sampler = [], None
                timer = None
                worker = None
                queue = Mock()
                try:
                    if arm.endswith("wait"):
                        evaluations = [
                            asyncio.create_task(
                                http.post(
                                    "/api/v1/evaluate",
                                    json={
                                        "session_id": "latency",
                                        "tool": "write",
                                        "args": {"file_path": "/repo/doc.md"},
                                    },
                                )
                            )
                            for _ in range(4)
                        ]
                        await wait_for_event(entered)
                        await asyncio.sleep(0.03)
                        timer = threading.Timer(0.25, release.set)
                        timer.start()
                    else:

                        def stats():
                            time.sleep(0.05)
                            return {"pending": 0}

                        queue.get_queue_stats.side_effect = stats
                        worker = BackgroundWorker(
                            Mock(), AnalysisConfig(), task_queue=queue
                        )
                        worker._running = True

                        async def old_sampler():
                            while worker._running:
                                values = queue.get_queue_stats()
                                worker.metrics.task_queue_depth = values.get(
                                    "pending", 0
                                )
                                await asyncio.sleep(10)

                        sampler = asyncio.create_task(
                            old_sampler()
                            if arm == "sync_stats"
                            else worker._queue_metrics_loop()
                        )
                        worker._tasks = [sampler]
                    start = time.perf_counter()
                    response = await http.get(url)
                    results[arm] = round((time.perf_counter() - start) * 1000, 3)
                    response.raise_for_status()
                    assert response.json() == baseline.json()
                    if evaluations:
                        responses = await asyncio.gather(*evaluations)
                        assert [r.status_code for r in responses] == [503] * 4
                        assert backend.lookups == 1
                    else:
                        # Stop also drains the offloaded 50 ms query.
                        await worker.stop()
                        assert queue.get_queue_stats.call_count == 1
                finally:
                    release.set()
                    if timer is not None:
                        timer.cancel()
                    if sampler is not None and not sampler.done():
                        await worker.stop()
                    await asyncio.gather(*evaluations, return_exceptions=True)
                    await backend.aclose()
        return results

    results = client.portal.call(scenario)
    print(
        f"paired {version} warm 1000-chunk event GET ms: {json.dumps(results, sort_keys=True)}"
    )
