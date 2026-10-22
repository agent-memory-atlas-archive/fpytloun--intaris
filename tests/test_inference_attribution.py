import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from openai import OpenAI

from intaris.config import LLMConfig
from intaris.inference_attribution import (
    attributed,
    attribution_headers,
    llm_attribution,
)
from intaris.llm import LLMClient

PREFIX = "X-Cognis-Inference-"


def test_scope_nested_reset_and_validation():
    assert attribution_headers() == {}
    with llm_attribution(operation="l3", task_id="task-123") as outer:
        with llm_attribution(operation="compaction") as inner:
            headers = attribution_headers()
            assert headers[PREFIX + "Task-ID"] == "task-123"
            assert inner.operation_id != outer.operation_id
        assert attribution_headers()[PREFIX + "Operation-ID"] == outer.operation_id
    assert attribution_headers() == {}
    with pytest.raises(ValueError):
        with llm_attribution(operation="content"):
            pass
    with llm_attribution(operation="l3", session_id="secret\ntext"):
        assert PREFIX + "Session-ID" not in attribution_headers()


def test_threads_isolated_and_worker_decorator():
    @attributed("intention")
    def work(*, session_id):
        return attribution_headers()

    with ThreadPoolExecutor(max_workers=2) as pool:
        headers = list(pool.map(lambda i: work(session_id=f"session-{i}"), range(8)))
    assert len({h[PREFIX + "Operation-ID"] for h in headers}) == 8
    assert attribution_headers() == {}


def test_async_scope_isolation():
    async def work(index):
        with llm_attribution(operation="l3", session_id=f"session-{index}"):
            await asyncio.sleep(0)
            return attribution_headers()[PREFIX + "Session-ID"]

    async def run():
        return await asyncio.gather(*(work(i) for i in range(8)))

    assert asyncio.run(run()) == [f"session-{i}" for i in range(8)]


@pytest.mark.parametrize("managed", [False, True])
def test_real_sdk_headers_retries_and_provider_body(monkeypatch, managed):
    requests = []
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "https://inference.example.com/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", "/not-read-by-mock")
    config = LLMConfig()
    if not managed:
        config.base_url = "https://provider.example.com/v1"
    client = LLMClient(config, transient_retries=0)
    client._client.close()
    assert client._managed_inference is managed

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"id": config.model, "object": "model"})
        requests.append(request)
        # Empty output causes a content repair, not an SDK retry.
        return httpx.Response(
            200,
            json={
                "id": "completion-123",
                "object": "chat.completion",
                "created": 1,
                "model": "model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "" if len(requests) == 1 else "done",
                        },
                    }
                ],
            },
        )

    client._client = OpenAI(
        api_key="test-key",
        base_url=config.base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    try:
        with llm_attribution(operation="summary_window") as scope:
            assert (
                client.generate([{"role": "user", "content": "private body"}]) == "done"
            )
        assert len(requests) == 2
        for request in requests:
            body = json.loads(request.content)
            assert set(body) <= {
                "model",
                "messages",
                "temperature",
                "max_tokens",
                "reasoning_effort",
            }
            if managed:
                assert request.headers[PREFIX + "Operation-ID"] == scope.operation_id
            else:
                assert not any(
                    k.startswith("x-cognis-inference-") for k in request.headers
                )
        if managed:
            assert (
                requests[0].headers[PREFIX + "Client-Request-ID"]
                != requests[1].headers[PREFIX + "Client-Request-ID"]
            )
    finally:
        client.close()
