"""Exercise capacity recovery through the actual OpenAI-compatible transport."""

import asyncio
import copy
import json
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI

from intaris import model_retry
from intaris.config import LLMConfig
from intaris.input_budget import CapacityCache, ContextInsufficientError
from intaris.llm import LLMClient, LLMTemporaryError


def transport(*, unavailable=False, input_limit=131072, output_limit=65536, reply=None):
    gets, posts = [], []

    def handle(request):
        if request.method == "GET":
            gets.append(request.extensions["timeout"]["read"])
            if len(gets) == 1 or unavailable:
                raise httpx.ReadTimeout(
                    "private diagnostic text must not be logged", request=request
                )
            return httpx.Response(
                200,
                json={
                    "id": "routes/intaris_judge",
                    "object": "model",
                    "created": 0,
                    "owned_by": "cognis",
                    "context_window": 131072,
                    "max_input_tokens": input_limit,
                    "max_output_tokens": output_limit,
                },
            )
        posts.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "capacity-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "routes/intaris_judge",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(reply or {}),
                        },
                    }
                ],
            },
        )

    client = LLMClient(LLMConfig(api_key="test-capacity-key"))
    client._client.close()
    client._client = OpenAI(
        api_key="test-capacity-key",
        base_url="https://capacity.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    return client, gets, posts


def protected_packet():
    return [
        {"role": "system", "content": "Assess complete original evidence."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "action": {"command": "x" * 60000 + " staging only"},
                    "original_assignment": "before " * 5500
                    + "Never modify production."
                    + " after" * 6500,
                    "human_resolution": "Approve only the exact staging proposal.",
                }
            ),
        },
    ]


@pytest.mark.parametrize("warm_unknown_cache", [False, True])
def test_unknown_capacity_rejection_refreshes_once_before_inference(
    warm_unknown_cache, caplog
):
    client, gets, posts = transport()
    original = protected_packet()
    saved = copy.deepcopy(original)
    try:
        if warm_unknown_cache:
            client.generate([{"role": "user", "content": "Small packet"}])
        client.generate(original, max_tokens=16000, single_attempt=True)
        assert original == saved
        assert gets == [0.2, 2.0]
        assert len(posts) == (2 if warm_unknown_cache else 1)
        assert posts[-1]["messages"] == original
        assert client.input_view["input_ceiling_tokens"] == 115072
        assert client.input_view["capacity_recovered"] is True
        assert "APITimeoutError" in caplog.text
        assert "private diagnostic text" not in caplog.text
    finally:
        client.close()


@pytest.mark.parametrize("unavailable,input_limit", [(True, 131072), (False, 12000)])
def test_failed_refresh_or_small_capacity_sends_explicit_partial_view(
    unavailable, input_limit
):
    client, gets, posts = transport(unavailable=unavailable, input_limit=input_limit)
    try:
        original = protected_packet()
        saved = copy.deepcopy(original)
        client.generate(original, max_tokens=16000, single_attempt=True)
        assert gets == [0.2, 2.0]
        assert len(posts) == 1
        assert original == saved
        assert client.input_view["trimmed"]
        assert client.input_view["source_coverage"] == "partial"
        assert not client.input_view["overflow"]
        assert "OMITTED MIDDLE" in json.dumps(posts[0])
        assert client.input_view["capacity_recovered"] is not unavailable
    finally:
        client.close()


def test_refresh_applies_new_output_cap_before_complete_envelope_fit():
    client, gets, posts = transport(output_limit=2048)
    try:
        client.generate(protected_packet(), max_tokens=16000, single_attempt=True)
        assert gets == [0.2, 2.0]
        assert posts[0]["max_tokens"] == 2048
        assert client.input_view["requested_output_reserve"] == 2048
        assert client.input_view["input_ceiling_tokens"] == 129024
    finally:
        client.close()


def test_refresh_respects_remaining_deadline():
    client, gets, posts = transport()
    try:
        with model_retry.evaluation_deadline(0.5):
            client.generate(protected_packet(), max_tokens=16000, single_attempt=True)
        assert len(gets) == 2
        assert all(0 < timeout <= 0.5 for timeout in gets)
        assert len(posts) == 1
    finally:
        client.close()


def test_exhausted_deadline_cannot_probe_or_send():
    client, gets, posts = transport()
    try:
        with model_retry.evaluation_deadline(0):
            with pytest.raises(LLMTemporaryError):
                client.generate(protected_packet(), max_tokens=16000)
        assert gets == posts == []
    finally:
        client.close()


def test_confirmed_input_limit_does_not_trigger_recovery():
    client, gets, posts = transport(input_limit=12000)
    try:
        client.input_capacity()  # First metadata attempt fails.
        client.input_capacity(refresh=True)  # Confirm the small actual limit.
        client.generate(protected_packet(), max_tokens=16000)
        assert gets == [0.2, 2.0]
        assert len(posts) == 1
        assert client.input_view["trimmed"]
        assert client.input_view["source_coverage"] == "partial"
        assert not client.input_view["overflow"]
    finally:
        client.close()


@pytest.mark.parametrize("unavailable,input_limit", [(True, 131072), (False, 12000)])
def test_protected_system_overhead_never_trimmed_or_dispatched(
    unavailable, input_limit
):
    client, gets, posts = transport(unavailable=unavailable, input_limit=input_limit)
    packet = [{"role": "system", "content": "Trusted system instruction. " * 8000}]
    original = copy.deepcopy(packet)
    try:
        with pytest.raises(ContextInsufficientError) as error:
            client.generate(packet, max_tokens=16000, single_attempt=True)
        assert gets == [0.2, 2.0]
        assert posts == []
        assert packet == original
        assert error.value.diagnostics == client.input_view
        assert error.value.diagnostics["overflow"]
    finally:
        client.close()


def test_refresh_never_reuses_other_tenant_capacity():
    cache = CapacityCache()
    old = ("endpoint", "judge", "tenant-one")
    new = ("endpoint", "judge", "tenant-two")
    cache.resolve(old, lambda _: {"max_input_tokens": 131072})
    assert cache.resolve(new, lambda _: {}, refresh=True).status == "unknown"
    assert cache.resolve(new, lambda _: {}, refresh=True).max_input_tokens is None


def test_legacy_judge_builder_recovers_and_keeps_complete_current_arguments(tmp_path):
    from intaris.audit import AuditStore
    from intaris.config import DBConfig, JudgeConfig
    from intaris.db import Database
    from intaris.judge import JudgeReviewer
    from intaris.session import SessionStore

    db = Database(DBConfig(path=str(tmp_path / "judge-capacity.db")))
    sessions, audit = SessionStore(db), AuditStore(db)
    intention = (
        "Review staging evidence. " * 2000
        + "Never modify production; preserve all scope qualifiers."
    )
    sessions.create(
        user_id="capacity-tenant",
        session_id="capacity-session",
        intention=intention,
    )
    args = {"command": "printf '%s' '" + "x" * 58000 + "' # staging only"}
    record = audit.insert(
        call_id="capacity-review",
        agent_id=None,
        user_id="capacity-tenant",
        session_id="capacity-session",
        tool="bash",
        args_redacted=args,
        classification="write",
        evaluation_path="llm",
        decision="escalate",
        risk="high",
        reasoning="Scope is uncertain; independent review required.",
        latency_ms=0,
    )
    client, gets, posts = transport(
        reply={
            "decision": "approve",
            "risk": "low",
            "confidence": "high",
            "reasoning": "No runtime effects; scoped inert test response.",
        }
    )
    reviewer = JudgeReviewer(
        llm=client,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=MagicMock(),
    )
    try:
        asyncio.run(
            reviewer.review_and_resolve(
                call_id=record["call_id"],
                user_id="capacity-tenant",
                session_id="capacity-session",
                agent_id=None,
            )
        )
        assert gets == [0.2, 2.0]
        assert len(posts) == 1
        assert args["command"] in posts[0]["messages"][1]["content"]
        assert intention in posts[0]["messages"][1]["content"]
        stored = audit.get_by_call_id(record["call_id"], user_id="capacity-tenant")
        assert stored["user_decision"] == "approve"
        assert stored["evaluation_metadata"]["judge_model_view"]["capacity_recovered"]
    finally:
        client.close()
