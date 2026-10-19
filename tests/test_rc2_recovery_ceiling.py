"""Offline reproductions of strict-provider failure and unresolvable plan calls."""

import asyncio
import json
import logging

import httpx
import pytest
from openai import OpenAI

from intaris import read_only
from intaris.classifier import is_read_only, verified_read_only_bash
from intaris.config import LLMConfig
from intaris.llm import LLMClient
from tests.test_hybrid_api import api as api
from tests.test_rich_context_recovery import barrier, completion, finish
from tests.test_single_intention import capture, record
from tests.test_single_intention import stores as stores


@pytest.mark.parametrize("failure_kind", ["success", "provider", "invalid_quote"])
@pytest.mark.parametrize("status", [400, 502, 422])
def test_wire_schema_failure_has_one_json_fallback(
    stores, caplog, failure_kind, status
):
    text = "Read README only."
    record(stores, text + "\n" + "Long assignment. " * 900)
    requests = []

    def transport(req):
        body = json.loads(req.content)
        requests.append(body)
        if len(requests) == 1 or failure_kind == "provider":
            return httpx.Response(
                status,
                json={
                    "error": {
                        "message": "sanitized upstream failure",
                        **(
                            {"code": "upstream_json_validate_failed"}
                            if status == 422
                            else {"upstream_code": "json_validate_failed"}
                        ),
                        "category": "request",
                    },
                },
            )
        content = completion(
            "Invented authorization." if failure_kind == "invalid_quote" else text
        )
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    llm = LLMClient(LLMConfig(api_key="test-key", model="test-model"))
    llm._client.close()
    llm._client = OpenAI(
        api_key="test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    owner = barrier(stores, llm)
    caplog.set_level(logging.INFO, logger="intaris.intention")

    async def run():
        await owner.trigger("owner", "session")
        await finish(owner)

    try:
        asyncio.run(run())
    finally:
        llm.close()
    assert len(requests) == 2
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[1]["response_format"] == {"type": "json_object"}
    assert (
        "Return JSON conforming to this schema" in requests[1]["messages"][0]["content"]
    )
    assert 4096 < requests[0]["max_tokens"] <= 8192
    assert capture(stores)[1]["status"] == (
        "ready" if failure_kind == "success" else "unavailable"
    )
    assert '"response_mode": "json_object"' in caplog.text


READS = [
    "git status --short; git rev-parse HEAD origin/main",
    "git status && git log -5 || git diff --no-ext-diff --no-textconv",
    "ls | head -10",
    "cat README | grep hello | head -5",
    "git diff --no-ext-diff --no-textconv --stat; ls && cat README | head",
]
NON_READS = [
    "git status;rm -rf /repo",
    "git status && cat README; rm file",
    "ls | head; rm file",
    "ls $(rm file)",
    "ls `rm file`",
    "git status > file",
    "cat README >> file",
    "cat README | sh",
    "eval 'ls'",
    "sh -c 'ls'",
    "unknown_command",
    "ls & cat README",
    "git diff --output=result",
    "git diff --out=result",
    "git diff --out result",
    "rg --pre 'rm file' x",
    "git status\nrm file",
    "ls ||",
    "ls; ; cat README",
    "git status; awk 'BEGIN {system(\"rm file\")}'",
]


@pytest.mark.parametrize("command", READS)
def test_compound_read_proof(command):
    assert verified_read_only_bash({"command": command})
    assert is_read_only("bash", {"command": command})
    assert read_only.observe("bash", {"command": command}) == "read_only"


@pytest.mark.parametrize("command", NON_READS)
def test_no_shell_execution_tricks_are_read_proof(command):
    assert not verified_read_only_bash({"command": command})
    assert read_only.observe("bash", {"command": command}) != "read_only"


def test_plan_unknown_denied_and_compound_read_approved_and_resolve_code(api):
    import intaris.server as srv

    client, version, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    assert (
        client.post(
            "/api/v1/intention",
            json={
                "session_id": "plan-ceiling",
                "intention": "Inspect repository",
                "policy": {"maximum_outcome": "approve"},
            },
        ).status_code
        == 200
    )
    srv._get_evaluator()._llm.generate.return_value = json.dumps(
        {
            "mutation": "unclear",
            "effects": ["unknown"],
            "assessment": "unclear",
            "aligned": True,
            "risk": "low",
            "decision": "approve",
            "reasoning": "test",
        }
    )
    request = {
        "session_id": "plan-ceiling",
        "tool": "bash",
        "context": {"chat_mode": "plan", "read_only_required": True},
    }
    denied = client.post(
        "/api/v1/evaluate", json={**request, "args": {"command": "unknown_command"}}
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["decision"] == "deny"
    assert denied.json()["reasoning"] == (
        "This explicit read-only restriction allows only read-only actions, and this command could not be verified as read-only."
    )
    resolution = client.post(
        "/api/v1/decision",
        json={
            "call_id": denied.json()["call_id"],
            "decision": "approve",
        },
    )
    assert resolution.status_code == 409, resolution.text
    assert resolution.json()["detail"]["code"] == "read_only_ceiling"
    allowed = client.post(
        "/api/v1/evaluate", json={**request, "args": {"command": READS[0]}}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["decision"] == "approve"
