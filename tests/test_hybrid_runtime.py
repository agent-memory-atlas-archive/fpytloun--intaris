"""Hybrid runtime compatibility and persistence without model network calls."""

import json
from unittest.mock import AsyncMock

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.hybrid import Assessment, PolicyResult
from intaris.hybrid_runtime import finalize
from intaris.policy_activation import HybridError, PolicyCompiler, prepare_policy
from intaris.session import SessionStore
from tests.evidence_mock import EvidenceMock as Mock


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", "hybrid_v1")
    db = Database(DBConfig(backend="sqlite", path=str(tmp_path / "hybrid.db")))
    sessions, audit = SessionStore(db), AuditStore(db)
    llm = Mock()
    llm.generate.return_value = Assessment(
        relationship="supports_intent",
        risk="low",
        operation="write",
        authorization="explicit",
        authorization_source="session_intention",
        authorization_scope="within_scope",
        environment="development",
        effects=["write"],
    ).model_dump_json()
    evaluator = Evaluator(llm=llm, session_store=sessions, audit_store=audit)
    return db, sessions, audit, llm, evaluator


def test_old_create_and_evaluate_requests_use_server_default(stores):
    _, sessions, audit, llm, evaluator = stores
    session = sessions.create(user_id="owner", session_id="work", intention="Fix tests")
    assert session["policy"]["profile"] == "balanced"
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
    )
    assert result["decision"] == "approve"
    assert result["evaluation_metadata"]["admission"] is not None
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        == "read_only_static"
    )
    llm.generate.assert_not_called()
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["evaluation_metadata"]["policy_hash"] == session["policy_hash"]


def test_write_uses_finite_classification(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(user_id="owner", session_id="work", intention="Fix tests")
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py", "content": "new"},
    )
    assert result["decision"] == "approve"
    assert result["classification"] == "write"
    assert llm.generate.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
async def test_native_mcp_without_extensions_matches_rest(stores, version):
    from mcp.types import CallToolResult, TextContent

    from intaris.mcp.proxy import MCPProxy
    from intaris.server import _session_agent_id, _session_user_id

    _, sessions, audit, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write the test file",
        policy={"evaluation_policy_version": version, "allow_tools": ["up:write"]},
    )
    upstream = Mock()
    upstream.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="written")])
    )
    connections = Mock()
    connections.get_or_connect = AsyncMock(return_value=upstream)
    connections.evict = AsyncMock()
    servers = Mock()
    servers.get_server.return_value = {
        "name": "up",
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
    result = await proxy.call_tool_rest(
        user_id="owner",
        agent_id=None,
        session_id="work",
        server_name="up",
        tool_name="write",
        arguments={"content": "test"},
    )
    assert result["decision"] == "approve"
    user_token, agent_token = _session_user_id.set("owner"), _session_agent_id.set(None)
    try:
        native = await proxy._handle_call_tool("up:write", {"content": "test"})
    finally:
        _session_user_id.reset(user_token)
        _session_agent_id.reset(agent_token)
    assert (native.content if version == "hybrid_v1" else native)[0].text == "written"
    assert upstream.call_tool.await_count == 2


def test_policy_update_during_assessment_rejects_admission(stores):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(user_id="owner", session_id="work", intention="Fix tests")
    answer = llm.generate.return_value

    def change(*args, **kwargs):
        sessions.update_session(
            "work", user_id="owner", policy={"deny_tools": ["write"]}
        )
        return answer

    llm.generate.side_effect = change
    with pytest.raises(HybridError, match="changed during evaluation"):
        evaluator.evaluate(
            user_id="owner",
            session_id="work",
            agent_id=None,
            tool="write",
            args={"file_path": "/repo/test.py"},
        )
    assert not audit.get_recent("work", user_id="owner", limit=10)


def test_ready_policy_never_compiles_and_atomic_cas(stores):
    _, sessions, _, _, _ = stores
    session = sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={
            "rules": [
                {
                    "id": "human",
                    "effect": "require_approval",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ]
        },
    )
    updated = sessions.update_session(
        "work",
        user_id="owner",
        policy={"profile": "strict"},
        expected_policy_revision=session["policy_revision"],
    )
    assert updated["policy_revision"] == 2
    with pytest.raises(HybridError):
        sessions.update_session(
            "work", user_id="owner", policy={}, expected_policy_revision=1
        )
    assert sessions.get("work", user_id="owner")["policy"]["profile"] == "strict"


def test_compiler_preserves_container_effect_and_tenant_cache():
    llm = Mock()
    rule = {
        "id": "deny_policies.0",
        "effect": "deny",
        "when": {"op": "eq", "field": "operation", "value": "write"},
    }
    llm.generate.return_value = json.dumps(
        {"supported": True, "reason": "", "rules": [rule]}
    )
    compiler = PolicyCompiler(llm, route="test-route")
    policy = {
        "evaluation_policy_version": "hybrid_v1",
        "deny_policies": ["Do not write"],
    }
    first = prepare_policy(policy, tenant="owner", compiler=compiler)
    assert prepare_policy(policy, tenant="owner", compiler=compiler) == first
    assert llm.generate.call_count == 1
    prepare_policy(policy, tenant="other", compiler=compiler)
    assert llm.generate.call_count == 2
    llm.generate.return_value = json.dumps(
        {
            "supported": True,
            "reason": "",
            "rules": [{**rule, "effect": "allow"}],
        }
    )
    with pytest.raises(HybridError, match="effect"):
        prepare_policy(policy, tenant="third", compiler=compiler)


@pytest.mark.parametrize("maximum", [None, "approve", "escalate", "deny"])
@pytest.mark.parametrize("minimum", [None, "approve", "escalate", "deny"])
@pytest.mark.parametrize("raw", ["approve", "escalate", "deny"])
def test_mandatory_floor_survives_every_outcome_cap(maximum, minimum, raw):
    result = PolicyResult(outcome=raw, mandatory_rule_ids=["require-human"])
    decision, _ = finalize(result, maximum=maximum, minimum=minimum)
    assert decision != "approve"
    if minimum == "deny":
        assert decision == "deny"


def test_real_human_floor_and_unchanged_native_retry(stores):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={
            "maximum_outcome": "approve",
            "rules": [
                {
                    "id": "require-human",
                    "effect": "require_approval",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ],
        },
    )
    args = dict(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py"},
    )
    first = evaluator.evaluate(**args)
    assert first["decision"] == "escalate"
    with pytest.raises(HybridError, match="human floor"):
        audit.resolve_escalation(
            first["call_id"], "approve", user_id="owner", resolved_by="judge"
        )
    audit.resolve_escalation(first["call_id"], "approve", user_id="owner")
    assert evaluator.evaluate(**args)["decision"] == "approve"
    assert all(
        call.kwargs["json_schema"]["name"] == "hybrid_selective_assessment"
        for call in llm.generate.call_args_list
    )


def test_latest_recorded_restriction_invalidates_inflight_assessment(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(user_id="owner", session_id="work", intention="Fix tests")
    answer = llm.generate.return_value

    def change(*args, **kwargs):
        sessions.allocate_event_sequences(
            "work",
            user_id="owner",
            user_message_flags=[True],
            latest_user_evidence={
                "source": "recording_client",
                "content": "Do not write",
            },
        )
        return answer

    llm.generate.side_effect = change
    with pytest.raises(HybridError, match="changed during evaluation"):
        evaluator.evaluate(
            user_id="owner",
            session_id="work",
            agent_id=None,
            tool="write",
            args={"file_path": "/repo/test.py"},
        )


@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
def test_old_policy_shapes_still_accepted(stores, version):
    _, sessions, _, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Read files",
        policy={"evaluation_policy_version": version, "deny_tools": ["write"]},
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py"},
    )
    assert result["decision"] == "deny"


def test_logical_id_replay_and_changed_arguments_rejected(stores):
    _, sessions, _, _, evaluator = stores
    sessions.create(user_id="owner", session_id="work", intention="Read files")
    args = dict(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
        logical_call_id="read-test",
    )
    assert evaluator.evaluate(**args)["decision"] == "approve"
    with pytest.raises(HybridError, match="already admitted"):
        evaluator.evaluate(**args)
    with pytest.raises(HybridError, match="arguments changed"):
        evaluator.evaluate(**{**args, "args": {"file_path": "/repo/other.py"}})


@pytest.mark.parametrize("vote", ["keep", "refresh", "uncertain"])
def test_optional_intention_gate(stores, vote):
    from intaris.intention import generate_intention

    db, sessions, audit, llm, _ = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={"intention_gate": "jev"},
    )
    gate = Mock()
    audit.insert(
        call_id="gate-user-message",
        user_id="owner",
        session_id="work",
        agent_id=None,
        record_type="reasoning",
        content="User message: Fix the new failing test too.",
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
    )
    gate.intention_gate.return_value = vote
    llm.generate.return_value = json.dumps(
        {"title": "Tests", "intention": "Fix the new failing test"}
    )
    generate_intention(
        user_id="owner",
        session_id="work",
        llm=llm,
        session_store=sessions,
        db=db,
        intention_source="user",
        jev=gate,
    )
    assert gate.intention_gate.call_count == 1
    assert llm.generate.call_count == (0 if vote == "keep" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_mcp_original_arguments_and_no_execution_retry(stores, lost_response):
    from mcp.types import CallToolResult, TextContent

    from intaris.hybrid import action_digest
    from intaris.mcp.proxy import MCPProxy

    _, sessions, audit, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write development file",
        policy={"allow_tools": ["up:write"]},
    )
    upstream = Mock()
    upstream.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="written")]),
        side_effect=TimeoutError("response lost") if lost_response else None,
    )
    connections = Mock()
    connections.get_or_connect = AsyncMock(return_value=upstream)
    connections.evict = AsyncMock()
    servers = Mock()
    servers.get_server.return_value = {
        "name": "up",
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
    arguments = {"path": "/repo/file", "content": "document-" * 5000}
    result = await proxy.call_tool_rest(
        user_id="owner",
        agent_id=None,
        session_id="work",
        server_name="up",
        tool_name="write",
        arguments=arguments,
        logical_call_id="write-document",
    )
    assert upstream.call_tool.await_count == 1
    assert upstream.call_tool.call_args.kwargs["arguments"] == arguments
    assert result["evaluation_metadata"]["action_digest"] == action_digest(
        "owner", "work", "up:write", arguments
    )
    assert result["evaluation_metadata"]["execution_state"] == (
        "uncertain" if lost_response else "completed"
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert (
        record["evaluation_metadata"]["execution_state"]
        == result["evaluation_metadata"]["execution_state"]
    )
    with pytest.raises(HybridError, match="already admitted"):
        await proxy.call_tool_rest(
            user_id="owner",
            agent_id=None,
            session_id="work",
            server_name="up",
            tool_name="write",
            arguments=arguments,
            logical_call_id="write-document",
        )
    assert upstream.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_judge_classifies_without_satisfying_human_floor(stores):
    from intaris.config import JudgeConfig
    from intaris.judge import JudgeReviewer

    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={
            "rules": [
                {
                    "id": "human",
                    "effect": "require_approval",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ]
        },
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/file"},
    )
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id="work", agent_id=None
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] is None
    assert record["judge_decision"] == "defer"
    # Human-only outstanding obligation does not re-ask irrelevant L1 fields.
    assert (
        record["evaluation_metadata"]["policy_evidence"]["assessment"]["relationship"]
        == "supports_intent"
    )
    # Balanced: a missing_permission ask on low-risk work is repaired once and
    # then approved; the human checkpoint still holds the call.
    stage = record["evaluation_metadata"]["stages"][-1]
    assert stage["model_answer"]["user_would"] == "ask"
    assert stage["classification"]["user_would"] == "ask"
    assert stage["downgrade_reason"] == "invalid_basis"
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id="work", agent_id=None
    )
    # L1 plus one independent Judge invocation with its single basis repair.
    assert llm.generate.call_count == 3
