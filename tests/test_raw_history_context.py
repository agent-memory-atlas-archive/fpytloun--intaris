"""Raw-source fallback and behavioral history must not manufacture permission."""

import asyncio
import json
import uuid
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from intaris.config import JudgeConfig, LLMConfig
from intaris.evaluator import Evaluator
from intaris.hybrid import evaluate_policy
from intaris.intention_profile import parse_profile, quote_span
from intaris.judge import JudgeReviewer
from intaris.judge_simulator import classify, request, user_explanation
from intaris.llm import LLMClient
from intaris.recent_actions import capture, summarize
from intaris.task_context import TaskContext
from tests.test_intent_aligned import state
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime
from tests.test_task_context import append_reasoning


def question():
    s = state()
    return request(
        {"tool": "edit"},
        {"policy": {"profile": "balanced"}, "intention": "Implement parser"},
        s,
        evaluate_policy([], s, "balanced"),
        {"originals": [], "items": []},
    )


@pytest.mark.parametrize("mode", ["json_object", "json_schema"])
def test_judge_sdk_mode_and_local_repair(mode):
    sent = []

    def transport(req):
        body = json.loads(req.content)
        sent.append(body)
        content = (
            "not JSON"
            if len(sent) == 1
            else json.dumps(
                {
                    "user_would": "approve",
                    "basis": "routine_within_task",
                    "risk": "low",
                    "confidence": "high",
                    "cites": [1],
                    "question": "",
                    "action_summary": "Edit the local parser",
                    "explanation": "This edit supports the current parser implementation task.",
                    "restriction_quote": "",
                }
            )
        )
        return httpx.Response(
            200,
            json={
                "id": str(uuid.uuid4()),
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    llm = LLMClient(
        LLMConfig(
            api_key="test-key",
            model="routes/intaris_judge" if mode == "json_object" else "local-test",
        )
    )
    llm._client.close()
    llm._client = OpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    try:
        answer, diagnostics = classify(llm, question())
    finally:
        llm.close()
    assert len(sent) == 2
    assert all(row["response_format"]["type"] == mode for row in sent)
    assert answer.user_would == "approve"
    assert diagnostics["validation_errors"] == ["JSONDecodeError"]


def test_judge_exhausted_validation_is_specific_and_fails_closed():
    llm = Mock(judge_response_mode="json_object")
    llm.generate.return_value = '{"user_would":"approve","invented":true}'
    answer, diagnostics = classify(llm, question())
    assert llm.generate.call_count == 2
    assert answer.user_would == "ask"
    assert diagnostics["response_origin"] == "assessment_failed"
    explanation = user_explanation(answer, {}, "escalate")
    assert "automated reviewer couldn't complete its review" in explanation
    assert "Retry or request human review" not in explanation
    assert "Hybrid" not in explanation


def test_quote_typography_only_and_original_offsets():
    source = "Use “parser” — do not deploy."
    start, end = quote_span(source, 'Use "parser" - do not deploy.')
    assert source[start:end] == source
    for paraphrase in ("Use parser and do not deploy.", "Use “parser” — deploy."):
        with pytest.raises(ValueError):
            quote_span(source, paraphrase)
    with pytest.raises(ValueError, match="ambiguous"):
        quote_span("Read it. Read it.", "Read it.")


def test_profile_envelope_failure_has_safe_field_diagnostic():
    diagnostics = []
    raw = json.dumps(
        {"title": "x" * 51, "intention": "Read README", "items": [], "current": []}
    )
    assert parse_profile(raw, {}, "Read README", diagnostics=diagnostics) is None
    assert diagnostics[0]["errors"][0]["loc"] == ["title"]
    assert "x" * 51 not in json.dumps(diagnostics)


def insert_history(audit, sid, user="owner"):
    audit.insert(
        call_id=str(uuid.uuid4()),
        user_id=user,
        session_id=sid,
        agent_id=None,
        tool="read",
        args_redacted={"path": "/repo/parser.py", "output": "OUTPUT_SENTINEL"},
        classification="read",
        decision="approve",
        risk="low",
        reasoning="DO NOT COPY",
        latency_ms=0,
        evaluation_path="fast",
    )


def test_history_bound_and_no_outputs():
    rows = [
        {
            "tool": "read",
            "args_redacted": {
                "path": f"/repo/{i}/" + "a" * 220,
                "output": "OUTPUT_SENTINEL",
            },
        }
        for i in range(30)
    ]
    result = summarize(rows)
    assert 1 <= len(result) <= 15
    assert len(json.dumps(result, ensure_ascii=False)) <= 2000
    assert "OUTPUT_SENTINEL" not in json.dumps(result)
    assert result[-1]["target"].startswith("/repo/0/")


def test_raw_fallback_and_recent_actions_reach_l1_and_judge(runtime):
    sessions, audit, llm, _, sid = runtime
    append_reasoning(
        audit,
        user_id="owner",
        session_id=sid,
        content="User message: Implement the parser in /repo/parser.py.",
        context={"role": "user"},
    )
    insert_history(audit, sid)
    context = TaskContext(
        llm=None,
        sessions=sessions,
        audit=audit,
        generation="single",
        extractor_identity={"model": "local-test"},
    )
    evaluator = Evaluator(
        llm=llm, session_store=sessions, audit_store=audit, task_context=context
    )
    # Force review to verify both owning inputs, rather than just the renderer.
    llm.generate.side_effect = lambda *a, **kw: response(kw, high=True)
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="write",
        args={"path": "/repo/parser.py"},
    )
    artifact = result["evaluation_metadata"]["task_context"]
    assert artifact["context_mode"] == "raw"
    assert artifact["status"] == "raw"
    prompt = llm.generate.call_args.args[0][1]["content"]
    assert "Implement the parser in /repo/parser.py." in prompt
    assert "source_context" in prompt
    assert "RECENT ATTEMPTED ACTIONS" not in prompt
    assert "OUTPUT_SENTINEL" not in prompt
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(llm)
    reviewer = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    asyncio.run(
        reviewer.review_and_resolve(
            call_id=result["call_id"], user_id="owner", session_id=sid
        )
    )
    review_call = next(
        call
        for call in llm.generate.call_args_list
        if call.kwargs.get("json_schema", {}).get("name") == "user_simulator"
    )
    payload = json.loads(review_call.args[0][1]["content"])
    assert "recent_actions" not in payload
    assert any("Implement the parser" in source for source in payload["originals"])
    assert "OUTPUT_SENTINEL" not in json.dumps(payload)
    saved = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert saved["user_decision"] == "approve"
    assert "context is currently unavailable" not in saved.get("reasoning", "")
    # Tenant and session filtering belongs to the history owner.
    assert capture(audit, {"user_id": "other-owner", "session_id": sid}) == []
    assert capture(audit, {"user_id": "owner", "session_id": sid + "-other"}) == []


def test_history_enrichment_uses_strictly_prior_projection(tmp_path, monkeypatch):
    from tools import paid_write_replay

    source = tmp_path / "source.json"
    output = tmp_path / "output"
    output.mkdir()
    rows = [{"call_id": str(uuid.uuid4())} for _ in range(300)]
    source.write_text(json.dumps(rows))

    def query(sql):
        assert "BEGIN READ ONLY" in sql
        assert "h.user_id=a.user_id AND h.session_id=a.session_id" in sql
        assert "h.timestamp::timestamptz < a.timestamp::timestamptz" in sql
        assert "LIMIT 15" in sql
        assert "tool_output" not in sql
        return [
            {**r, "original_assignment": "Implement parser", "recent_actions": []}
            for r in rows
        ]

    monkeypatch.setattr(paid_write_replay, "query", query)
    paid_write_replay.enrich_history(source, output)
    assert (output / "paid-input.json").stat().st_mode & 0o777 == 0o600
    assert len(json.loads((output / "paid-input.json").read_text())) == 300
    with pytest.raises(ValueError, match="replace"):
        paid_write_replay.enrich_history(source, output)


def test_jev_failure_preserves_frozen_history_for_judge(runtime, monkeypatch):
    from intaris import policy_assessment

    sessions, audit, llm, _, sid = runtime
    insert_history(audit, sid)
    sent = []

    def invalid_classification(llm, jev, question):
        assert "recent_actions" not in question
        raise ValueError("Invalid JEV output")

    monkeypatch.setattr(policy_assessment, "classify", invalid_classification)
    evaluator = Evaluator(
        llm=llm, jev=Mock(), session_store=sessions, audit_store=audit
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="write",
        args={"path": "/repo/parser.py"},
    )
    assert "recent_actions" not in result["evaluation_metadata"]["stages"][0]
    reviewer = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    llm.generate.side_effect = lambda *a, **kw: response(kw)
    asyncio.run(
        reviewer.review_and_resolve(
            call_id=result["call_id"],
            user_id="owner",
            session_id=sid,
        )
    )
    payload = json.loads(llm.generate.call_args.args[0][1]["content"])
    assert "recent_actions" not in payload
    assert sent == []
    assert capture(audit, {"user_id": "owner", "session_id": sid})
