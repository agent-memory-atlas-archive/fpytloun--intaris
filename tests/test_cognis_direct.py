import json

import httpx
import pytest

from tools.benchmark.cognis_direct import DirectProxy


def models():
    return {
        "data": [
            {
                "id": model,
                "owned_by": "groq",
                "resolution_fingerprint": model,
                "cognis": {"operations": ["chat"]},
                "max_input_tokens": 131072,
                "max_output_tokens": 65536,
            }
            for model in set(DirectProxy.MODEL_IDS.values())
        ]
    }


def test_fixed_20b_and_per_attempt_accounting(tmp_path):
    sent = []

    def handle(request):
        assert request.url.host == "cognis.example.org"
        if request.method == "GET":
            return httpx.Response(200, json=models())
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        )

    proxy = DirectProxy(
        tmp_path,
        "test-key",
        None,
        base_url="https://cognis.example.org/v1",
        transport=httpx.MockTransport(handle),
    )
    try:
        reply = proxy.forward(
            "l1",
            "routes/intaris_evaluation",
            {
                "model": "routes/intaris_evaluation",
                "messages": [],
            },
        )
        assert reply["status"] == 200
        assert sent[0]["model"] == "providers/groq/openai/gpt-oss-20b"
        assert sent[0]["reasoning_effort"] == "low"
        assert proxy.records[0]["model"] == "openai/gpt-oss-20b"
        assert proxy.records[0]["provider"] == "groq"
        assert proxy.budget.snapshot()["attempts"] == 1
        assert proxy.budget.snapshot()["unknown_attempts"] == 0
    finally:
        proxy.close()
    assert "test-key" not in (tmp_path / "provider-attempts.jsonl").read_text()


def test_provider_change_fails_closed_before_inference(tmp_path):
    body = models()
    body["data"][0]["owned_by"] = "changed-provider"
    with pytest.raises(ValueError, match="identity changed"):
        DirectProxy(
            tmp_path,
            "test-key",
            None,
            base_url="https://cognis.example.org/v1",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )
