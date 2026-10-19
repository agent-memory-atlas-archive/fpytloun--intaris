import asyncio
import json
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from intaris import policy_assessment
from intaris.config import DBConfig, LLMConfig
from intaris.db import Database
from intaris.intention import IntentionBarrier
from intaris.llm import LLMClient
from intaris.session import SessionStore
from tests.test_rich_context_recovery import barrier, completion, finish
from tests.test_single_intention import capture, record
from tests.test_single_intention import stores as stores


def test_observed_managed_analysis_route_mode(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("local-test-token")
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://localhost:1/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    assert (
        LLMClient(LLMConfig(model="routes/intaris_analysis")).profile_response_mode
        == "json_object"
    )
    assert (
        LLMClient(LLMConfig(model="routes/intaris_evaluation")).profile_response_mode
        == "json_schema"
    )


@pytest.mark.asyncio
async def test_assignment_only_profile_uses_primary_object_mode_without_inventing_messages(
    tmp_path,
):
    db = Database(DBConfig(backend="sqlite", path=str(tmp_path / "state.db")))
    sessions = SessionStore(db)
    sessions.create(
        user_id="owner", session_id="assignment-only", intention="Implement the parser"
    )
    llm = Mock(profile_response_mode="json_object")

    def generate(messages, **kwargs):
        assert kwargs["response_mode"] == "json_object"
        sources = json.loads(messages[1]["content"].splitlines()[1])["sources"]
        source = sources[0]
        return json.dumps(
            {
                "compact_intention": "Implement parser",
                "current_request": {"status": "resolved", "item_indices": [0]},
                "items": [
                    {
                        "source_ref": source["ref"],
                        "quote": source["text"],
                        "kind": "assignment",
                        "scope": "current",
                        "status": "active",
                        "temporal_status": "current",
                    }
                ],
            }
        )

    llm.generate.side_effect = generate
    owner = IntentionBarrier(db=db, llm=llm, generation="single")
    await owner.trigger("owner", "assignment-only")
    await owner.wait("owner", "assignment-only")
    assert owner.update_count == 0  # No evidence to regenerate: use declared intention.
    llm.generate.assert_not_called()
    db.close()


@pytest.mark.parametrize("first", ["not JSON", '{"choice": "wrong"}'])
def test_l1_local_parse_or_enum_error_gets_one_validated_repair(first):
    question = {
        "schema": {
            "type": "object",
            "properties": {"choice": {"enum": ["yes"]}},
            "required": ["choice"],
            "additionalProperties": False,
        },
        "prompt": "classify",
        "judge": False,
        "projection": "json",
    }
    llm = Mock()
    llm.generate.side_effect = [first, '{"choice":"yes"}']
    answer, diagnostics, _ = policy_assessment.classify(llm, None, question)
    assert answer == {"choice": "yes"}
    assert diagnostics["validation_attempts"] == 2
    assert llm.generate.call_count == 2


def test_l1_repair_never_accepts_invalid_second_answer():
    question = {
        "schema": {
            "type": "object",
            "properties": {"choice": {"enum": ["yes"]}},
            "required": ["choice"],
            "additionalProperties": False,
        },
        "prompt": "classify",
        "judge": False,
        "projection": "json",
    }
    llm = Mock()
    llm.generate.return_value = "not JSON"
    with pytest.raises(json.JSONDecodeError):
        policy_assessment.classify(llm, None, question)
    assert llm.generate.call_count == 2


@pytest.mark.parametrize(
    "case,expected_requests,ready",
    [
        ("success", 1, True),
        ("transient", 2, True),
        ("exhausted", 4, False),
        ("invalid_quote", 2, False),
        ("invalid_items", 2, True),
        ("oversized_items", 2, True),
        ("invalid_current", 2, True),
        ("oversized_current", 2, True),
    ],
)
def test_managed_profile_primary_object_wire_and_validation(
    stores, case, expected_requests, ready
):
    record(stores, "Read README only.")
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if case == "exhausted" or (case == "transient" and len(requests) == 1):
            return httpx.Response(502, json={"error": {"code": "upstream_error"}})
        content = completion(
            "Invented authorization."
            if case == "invalid_quote"
            else "Read README only."
        )
        if len(requests) == 1 and case in {
            "invalid_items",
            "oversized_items",
            "invalid_current",
            "oversized_current",
        }:
            value = json.loads(content)
            field = "items" if case.endswith("items") else "current"
            value[field] = {} if case.startswith("invalid") else [0] * 13
            content = json.dumps(value)
        return httpx.Response(
            200,
            json={
                "id": "local-profile-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "routes/intaris_analysis",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    llm = LLMClient(LLMConfig(api_key="test-key", model="routes/intaris_analysis"))
    llm._client.close()
    llm._client = OpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    owner = barrier(stores, llm)

    async def run():
        await owner.trigger("owner", "session")
        await finish(owner)

    try:
        asyncio.run(run())
    finally:
        llm.close()
    assert len(requests) == expected_requests
    assert all(row["response_format"] == {"type": "json_object"} for row in requests)
    assert (
        "Return JSON conforming to this schema" in requests[0]["messages"][0]["content"]
    )
    assert (capture(stores)[1]["status"] == "ready") is ready
