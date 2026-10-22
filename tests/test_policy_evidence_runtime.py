"""Owning-boundary acceptance; model answers are fixtures, not live evidence."""

import asyncio
import json
import os
import threading
import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig, JudgeConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.judge import JudgeReviewer
from intaris.session import SessionStore


def response(kwargs, *, high=False, conflict=False, unresolved=False):
    schema = kwargs["json_schema"]["schema"]
    if "user_would" in schema["properties"]:
        return json.dumps(
            {
                "user_would": "ask"
                if unresolved
                else "refuse"
                if conflict
                else "approve",
                "risk": "high" if high else "low",
                "confidence": "high",
                "basis": "restriction_conflict" if conflict else "explicit_request",
                "cites": [1],
                "question": "Confirm the scope?" if unresolved else "",
                "action_summary": "Write the local parser",
                "explanation": "Review the actual local file effect against the requested work.",
                "restriction_quote": "Implement the local parser" if conflict else "",
            }
        )
    review = "classification" in schema["properties"]
    properties = (
        schema["properties"]["classification"]["properties"]
        if review
        else schema["properties"]
    )
    values = {
        "authorization": "explicit",
        "authorization_scope": "within_scope",
        "authorization_source": "session_intention",
        "relationship": "supports_intent",
        "target_scope": "consistent",
        "target_reference": "action",
        "contextual_restrictions": "conflicts" if conflict else "consistent",
        "restriction_reference": "session_intention",
        "consequence": "consequential" if high else "routine",
        "risk": "high" if high else "low",
        "mutation": "mutating",
        "effects": ["write"],
        "environment": "development",
        "operation": "write",
        "restriction_quote": "",
    }
    answer = {key: values[key] for key in properties}
    if review:
        answer = {
            "classification": answer,
            "obligations": {
                key: {
                    "status": "unresolved" if unresolved else "consistent",
                    "reference": "session_intention",
                }
                for key in schema["properties"]["obligations"]["properties"]
            },
        }
    return json.dumps(answer)


@pytest.fixture(params=["sqlite", "postgresql"])
def runtime(request, tmp_path, monkeypatch):
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", "hybrid_v1")
    url = os.environ.get("TEST_HYBRID_POSTGRES_URL", "")
    if request.param == "postgresql" and not url:
        pytest.skip("Isolated PostgreSQL URL not configured")
    db = Database(
        DBConfig(
            backend=request.param, path=str(tmp_path / "state.db"), database_url=url
        )
    )
    sessions, audit = SessionStore(db), AuditStore(db)
    llm = Mock()
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs)
    evaluator = Evaluator(llm=llm, session_store=sessions, audit_store=audit)
    session_id = str(uuid.uuid4())
    sessions.create(
        user_id="owner", session_id=session_id, intention="Implement the local parser"
    )
    yield sessions, audit, llm, evaluator, session_id
    db.close()


def call(evaluator, session_id, **options):
    return evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/parser.py"},
        **options,
    )


def test_unchanged_request_routine_assignment(runtime):
    _, audit, llm, evaluator, session_id = runtime
    result = call(evaluator, session_id)
    assert result["decision"] == "approve"
    assert llm.generate.call_count == 1
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    state = record["evaluation_metadata"]["policy_evidence"]
    assert state["observations"]["mutation"]["value"] == "mutating"
    assert state["assessment"]["risk"] == "low"
    assert state["review"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved", [False, True])
async def test_high_impact_keeps_risk_through_targeted_review(runtime, unresolved):
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.allocate_event_sequences(
        session_id,
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": "Implement the local parser"},
    )
    llm.generate.side_effect = lambda *args, **kwargs: response(
        kwargs, high=True, unresolved=unresolved
    )
    result = call(evaluator, session_id)
    assert result["decision"] == "escalate"
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(llm)
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id=session_id
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] == (None if unresolved else "approve")
    metadata = record["evaluation_metadata"]
    assert metadata["facts"]["risk"] == "high"
    assert len(metadata["stages"]) == 2
    assert metadata["review_binding"]["state_revision"] == metadata["state_revision"]


@pytest.mark.asyncio
async def test_late_targeted_review_cannot_resolve_changed_state(runtime):
    sessions, audit, llm, evaluator, session_id = runtime
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, high=True)
    result = call(evaluator, session_id)
    entered, release = threading.Event(), threading.Event()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return response(kwargs, high=True)

    llm.generate.side_effect = delayed
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    pending = asyncio.create_task(
        judge.review_and_resolve(
            call_id=result["call_id"], user_id="owner", session_id=session_id
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    sessions.update_session(
        session_id, user_id="owner", policy={"deny_tools": ["write"]}
    )
    release.set()
    await pending
    assert (
        audit.get_by_call_id(result["call_id"], user_id="owner")["user_decision"]
        is None
    )


def test_explicit_operator_waiver_is_not_evidence(runtime):
    sessions, _, llm, evaluator, session_id = runtime
    sessions.update_session(
        session_id, user_id="owner", policy={"maximum_outcome": "approve"}
    )
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, high=True)
    result = call(evaluator, session_id)
    assert result["decision"] == "approve"
    assert result["outcome_override"] == "waived_by_operator_bound"
    assert result["evaluation_metadata"]["policy_evidence"]["review"] is None
    assert result["evaluation_metadata"]["outstanding_obligations"]
    assert (
        call(
            evaluator,
            session_id,
            context={"chat_mode": "plan", "read_only_required": True},
        )["decision"]
        == "deny"
    )


def test_raw_restriction_cannot_be_erased_by_general_scope(runtime):
    _, _, llm, evaluator, session_id = runtime
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, conflict=True)
    result = call(evaluator, session_id)
    assert result["decision"] == "escalate"
    assert any(
        o["id"] == "model.denial"
        for o in result["evaluation_metadata"]["outstanding_obligations"]
    )


def test_operator_bound_never_requests_malformed_model_output(runtime):
    sessions, _, llm, evaluator, session_id = runtime
    sessions.update_session(
        session_id, user_id="owner", policy={"maximum_outcome": "approve"}
    )
    llm.generate.side_effect = None
    llm.generate.return_value = '{"review_completed":true}'
    result = call(evaluator, session_id)
    assert result["decision"] == "approve"
    assert result["evaluation_metadata"]["operator_bound"]
    llm.generate.assert_not_called()


@pytest.mark.asyncio
async def test_rest_and_native_proxy_keep_original_arguments_and_overlay(runtime):
    from mcp.types import CallToolResult, TextContent

    from intaris.mcp.proxy import MCPProxy
    from intaris.server import _session_agent_id, _session_user_id

    sessions, audit, llm, evaluator, session_id = runtime
    upstream = Mock()
    upstream.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="done")])
    )
    connections = Mock()
    connections.get_or_connect = AsyncMock(return_value=upstream)
    servers = Mock()
    servers.get_server.return_value = {
        "name": "service",
        "enabled": True,
        "agent_pattern": "*",
    }
    servers.get_all_tool_preferences.return_value = {}
    proxy = MCPProxy(
        connection_manager=connections,
        evaluator=evaluator,
        session_store=sessions,
        audit_store=audit,
        server_store=servers,
    )
    proxy._ensure_session = AsyncMock(return_value=session_id)
    arguments = {"file_path": "/repo/parser.py", "content": "body" * 10000}
    result = await proxy.call_tool_rest(
        user_id="owner",
        agent_id=None,
        session_id=session_id,
        server_name="service",
        tool_name="write",
        arguments=arguments,
    )
    assert result["decision"] == "approve"
    assert upstream.call_tool.call_args.kwargs["arguments"] == arguments
    token, agent = _session_user_id.set("owner"), _session_agent_id.set(None)
    try:
        result = await proxy._handle_call_tool("service:write", arguments)
        assert result.content[0].text == "done"
    finally:
        _session_user_id.reset(token)
        _session_agent_id.reset(agent)
    denied = await proxy.call_tool_rest(
        user_id="owner",
        agent_id=None,
        session_id=session_id,
        server_name="service",
        tool_name="write",
        arguments=arguments,
        context={"chat_mode": "plan", "read_only_required": True},
    )
    assert denied["decision"] == "deny"
    assert upstream.call_tool.await_count == 2


def test_selective_jev_requests_only_outstanding_fields(runtime, monkeypatch):
    from intaris.config import JevConfig
    from intaris.hybrid import evaluate_policy
    from intaris.jev import JevClient
    from intaris.policy_assessment import classify, request
    from intaris.policy_evidence import from_observations

    state = from_observations(
        {
            "tool": "read",
            "static_read": True,
            "effects": ["read"],
            "mutation": "read_only",
        },
        sources={
            "assignment": {
                "origin": "client_assignment",
                "content": "Read the file",
                "independent_grant": True,
            }
        },
    )
    decision = evaluate_policy([], state, "strict")
    question = request(state, decision, "Read")
    client = JevClient(JevConfig(api_key="test-key"))
    seen = {}

    def answer(*, state, questions):
        seen.update(questions)
        choices = {
            "contextual_restrictions": "consistent",
            "restriction_reference": "session_intention",
            "relationship": "supports_intent",
            "risk": "low",
            "authorization": "absent",
            "authorization_source": "none",
            "authorization_scope": "unclear",
            "target_scope": "consistent",
            "target_reference": "action",
        }
        return {
            "model": client._model,
            "answers": {
                key: {
                    "type": "choice",
                    "choice": choices[key],
                    "confidence": 1,
                    "probabilities": {
                        value: float(value == choices[key])
                        for value in value["criteria"]
                    },
                }
                for key, value in questions.items()
            },
        }

    monkeypatch.setattr(client, "_request", answer)
    try:
        result, _, _ = classify(None, client, question)
        assert result["contextual_restrictions"] == "consistent"
        # Typed classification cannot emit optional free-form quotes.
        assert set(seen) == set(question["fields"]) - {"restriction_quote"}
        assert not {"mutation", "effects"} & set(seen)
        assert not {
            "authorization",
            "authorization_source",
            "authorization_scope",
        } & set(seen)
    finally:
        client.close()
