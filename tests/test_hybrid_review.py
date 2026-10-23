"""Regression evidence for the independent hybrid review findings."""

import json
from unittest.mock import AsyncMock

import anyio
import pytest

from intaris.config import JudgeConfig
from intaris.hybrid import Assessment
from intaris.judge import JudgeReviewer
from intaris.policy_activation import HybridError
from tests.evidence_mock import EvidenceMock as Mock
from tests.test_hybrid_runtime import stores as _shared_stores

stores = _shared_stores


def test_human_approval_cannot_outlive_new_user_restriction(stores):
    _, sessions, audit, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write tests",
        policy={
            "maximum_outcome": "approve",
            "rules": [
                {
                    "id": "human",
                    "effect": "require_approval",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ],
        },
    )
    request = dict(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py"},
    )
    first = evaluator.evaluate(**request)
    audit.resolve_escalation(first["call_id"], "approve", user_id="owner")
    sessions.allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": "Do not write"},
    )
    assert evaluator.evaluate(**request)["decision"] == "escalate"
    with pytest.raises(HybridError, match="current action/policy"):
        evaluator.evaluate(**request, approval_call_id=first["call_id"])


def test_latest_restriction_is_assessed_even_with_existing_permit(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write tests",
        policy={"allow_tools": ["write"]},
    )
    sessions.allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": "Do not write"},
    )
    llm.generate.return_value = Assessment(
        relationship="conflicts_with_intent",
        risk="low",
    ).model_dump_json()
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py"},
    )
    assert result["decision"] == "escalate"
    assert any(
        o["id"] == "model.denial"
        for o in result["evaluation_metadata"]["outstanding_obligations"]
    )
    assert llm.generate.call_count == 1


@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
@pytest.mark.parametrize("command", ["shutdown\npwd", "echo $(reboot)"])
def test_unsupported_shell_syntax_retains_critical_detection(stores, version, command):
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Read files",
        policy={"evaluation_policy_version": version},
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="bash",
        args={"command": command},
    )
    assert result["decision"] == ("deny" if version == "legacy" else "escalate")
    if version == "hybrid_v1":
        evidence = result["evaluation_metadata"]["policy_evidence"]
        assert evidence["observations"]["risk"]["value"] == "critical"
        assert "model.risk.critical" in [
            o["id"] for o in result["evaluation_metadata"]["outstanding_obligations"]
        ]
    if version == "legacy":
        llm.generate.assert_not_called()
    else:
        llm.generate.assert_called_once()


@pytest.mark.parametrize("aligned", [True, False])
def test_child_alignment_without_analysis_and_human_acknowledgment(stores, aligned):
    from intaris.alignment import AlignmentBarrier
    from intaris.evaluator import Evaluator

    db, sessions, audit, llm, evaluator = stores
    evaluator = Evaluator(
        llm=llm,
        session_store=sessions,
        audit_store=audit,
        alignment_barrier=AlignmentBarrier(db=db, llm=llm),
    )
    sessions.create(user_id="owner", session_id="parent", intention="Fix tests")
    sessions.create(
        user_id="owner",
        session_id="child",
        intention="Supporting work",
        parent_session_id="parent",
    )
    llm.generate.return_value = json.dumps(
        {"aligned": aligned, "reasoning": "Alignment check"}
    )
    llm.generate.side_effect = lambda *a, **kw: (
        json.dumps(
            {
                "contextual_restrictions": "consistent",
                "restriction_reference": "session_intention",
            }
        )
        if (kw.get("json_schema") or {}).get("name") == "hybrid_selective_assessment"
        else llm.generate.return_value
    )
    request = dict(
        user_id="owner",
        session_id="child",
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
    )
    result = evaluator.evaluate(**request)
    # Balanced exempts a controller-proven read from an ungrounded misalignment;
    # the classification is still recorded and binds the child's writes
    # (alignment obligations: tests/test_fact_first_policy.py).
    assert result["decision"] == "approve"
    metadata = result["evaluation_metadata"]
    assert "alignment.compatibility" not in [
        o["id"] for o in metadata["outstanding_obligations"]
    ]
    if not aligned:
        assert metadata["parent_alignment"]["state"] == "incompatible"
    # Approving a read does not grant permanent compatibility to the child.
    assert sessions.get("child", user_id="owner")["alignment_state"] == (
        "ready" if aligned else "misaligned"
    )
    assert llm.generate.call_count == 1  # Alignment only; static reads bypass L1.


def test_failed_child_alignment_is_not_fail_open(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(user_id="owner", session_id="parent", intention="Fix tests")
    sessions.create(
        user_id="owner",
        session_id="child",
        intention="Supporting work",
        parent_session_id="parent",
    )
    llm.generate.side_effect = RuntimeError("provider unavailable")
    with pytest.raises(HybridError) as failure:
        evaluator.evaluate(
            user_id="owner",
            session_id="child",
            agent_id=None,
            tool="read",
            args={"file_path": "/repo/test.py"},
        )
    assert failure.value.code == "alignment_unavailable"
    assert failure.value.status == 503
    assert failure.value.retryable
    assert sessions.get("child", user_id="owner")["alignment_state"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,policy,assessment,expected",
    [
        (
            "auto",
            {"judge": {"allowed_decisions": ["deny"]}},
            Assessment(relationship="supports_intent", risk="low"),
            "deny",
        ),
        ("auto", {}, Assessment(), "deny"),
        ("advisory", {}, Assessment(), None),
    ],
)
async def test_judge_constraints_apply_to_classification(
    stores, mode, policy, assessment, expected
):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner", session_id="work", intention="Fix tests", policy=policy
    )
    llm.generate.return_value = Assessment(
        relationship="supports_intent", risk="high"
    ).model_dump_json()
    first = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py"},
    )
    assert first["decision"] == "escalate"
    llm.generate.return_value = assessment.model_dump_json()
    reviewer = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode=mode),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await reviewer.review_and_resolve(
        call_id=first["call_id"], user_id="owner", session_id="work", agent_id=None
    )
    assert (
        audit.get_by_call_id(first["call_id"], user_id="owner")["user_decision"]
        == expected
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("misaligned", [False, True])
async def test_native_sdk_transport_preserves_uncertainty_and_blocks_replay(
    stores, misaligned
):
    from mcp import ClientSession
    from mcp.shared.message import SessionMessage

    from intaris.mcp.proxy import MCPProxy
    from intaris.server import _session_agent_id, _session_user_id

    _, sessions, audit, llm, evaluator = stores
    if misaligned:
        sessions.create(user_id="owner", session_id="parent", intention="Fix tests")
        sessions.create(
            user_id="owner",
            session_id="child",
            intention="Other work",
            parent_session_id="parent",
        )
        llm.generate.return_value = json.dumps(
            {"aligned": False, "reasoning": "Unrelated"}
        )
    upstream = Mock()
    upstream.call_tool = AsyncMock(
        side_effect=TimeoutError("response lost after write")
    )
    connections = Mock()
    connections.get_or_connect = AsyncMock(return_value=upstream)
    connections.evict = AsyncMock()
    servers = Mock()
    server = {
        "name": "up",
        "enabled": True,
        "agent_pattern": "*",
        "tools_cache": [
            {
                "name": "write",
                "inputSchema": {"type": "object"},
                "description": "Write a file",
            }
        ],
    }
    servers.get_server.return_value = server
    servers.list_servers.return_value = [server]
    servers.get_all_tool_preferences.return_value = {}
    proxy = MCPProxy(
        connection_manager=connections,
        evaluator=evaluator,
        session_store=sessions,
        audit_store=audit,
        server_store=servers,
    )
    if misaligned:
        proxy._ensure_session = AsyncMock(return_value="child")
    client_send, server_read = anyio.create_memory_object_stream[SessionMessage](10)
    server_send, client_read = anyio.create_memory_object_stream[SessionMessage](10)
    user_token, agent_token = _session_user_id.set("owner"), _session_agent_id.set(None)
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(
                proxy.server.run,
                server_read,
                server_send,
                proxy.server.create_initialization_options(),
            )
            async with ClientSession(client_read, client_send) as client:
                await client.initialize()
                arguments = {"path": "/repo/file", "content": "write"}
                meta = {"intaris": {"logical_call_id": "native-write"}}
                response = await client.call_tool("up:write", arguments, meta=meta)
                assert response.isError
                assert response.model_dump(by_alias=True).get("_meta"), (
                    response.model_dump(by_alias=True)
                )
                metadata = response.model_dump(by_alias=True)["_meta"]["intaris"]
                if misaligned:
                    assert metadata["code"] == "alignment_unavailable"
                    assert metadata["execution_state"] == "not_dispatched"
                    assert metadata["retryable"] is True
                    assert audit.get_recent("child", user_id="owner", limit=10) == []
                    repeated = await client.call_tool("up:write", arguments, meta=meta)
                    assert repeated.isError
                    assert (
                        repeated.model_dump(by_alias=True)["_meta"]["intaris"]["code"]
                        == "alignment_unavailable"
                    )
                    assert upstream.call_tool.await_count == 0
                    assert audit.get_recent("child", user_id="owner", limit=10) == []
                    group.cancel_scope.cancel()
                    return
                assert metadata["execution_state"] == "uncertain"
                assert metadata["retryable"] is False
                assert metadata["evaluation_metadata"]["admission"] is not None
                repeated = await client.call_tool("up:write", arguments, meta=meta)
                assert repeated.isError
                assert upstream.call_tool.await_count == 1
            group.cancel_scope.cancel()
    finally:
        _session_user_id.reset(user_token)
        _session_agent_id.reset(agent_token)


def test_active_snapshot_survives_database_restart(stores, tmp_path):
    from intaris.audit import AuditStore
    from intaris.config import DBConfig
    from intaris.db import Database
    from intaris.evaluator import Evaluator
    from intaris.session import SessionStore

    db, sessions, _, llm, _ = stores
    original = sessions.create(
        user_id="owner",
        session_id="work",
        intention="Read tests",
        policy={"profile": "strict", "deny_tools": ["write"]},
    )
    db.close()
    reopened = Database(DBConfig(backend="sqlite", path=str(tmp_path / "hybrid.db")))
    try:
        restored = SessionStore(reopened)
        assert (
            restored.get("work", user_id="owner")["active_policy"]
            == original["active_policy"]
        )
        evaluator = Evaluator(
            llm=llm, session_store=restored, audit_store=AuditStore(reopened)
        )
        assert (
            evaluator.evaluate(
                user_id="owner",
                session_id="work",
                agent_id=None,
                tool="write",
                args={"path": "/repo/test"},
            )["decision"]
            == "deny"
        )
        assert not llm.generate.called
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"aligned": "false"},
        {"aligned": None},
        {"aligned": 0},
        {"aligned": 1},
        {"aligned": False},
        {"aligned": True},
    ],
)
@pytest.mark.asyncio
async def test_alignment_strict_boolean_on_sync_and_async_paths(stores, payload):
    from intaris.alignment import AlignmentBarrier, check_intention_alignment
    from intaris.evaluator import Evaluator

    db, sessions, audit, llm, evaluator = stores
    evaluator = Evaluator(
        llm=llm,
        session_store=sessions,
        audit_store=audit,
        alignment_barrier=AlignmentBarrier(db=db, llm=llm),
    )
    sessions.create(user_id="owner", session_id="parent", intention="Fix tests")
    sessions.create(
        user_id="owner",
        session_id="child",
        intention="Supporting work",
        parent_session_id="parent",
    )
    llm.generate.return_value = json.dumps(
        {"reasoning": "Alignment evidence", **payload}
    )
    llm.generate.side_effect = lambda *a, **kw: (
        json.dumps(
            {
                "contextual_restrictions": "consistent",
                "restriction_reference": "session_intention",
            }
        )
        if (kw.get("json_schema") or {}).get("name") == "hybrid_selective_assessment"
        else llm.generate.return_value
    )
    valid = type(payload.get("aligned")) is bool
    # Legacy intentionally retains the existing fail-open exception fallback.
    assert check_intention_alignment(
        llm=llm, parent_intention="Fix", child_intention="Support"
    )[0] == (payload["aligned"] if valid else True)
    barrier = AlignmentBarrier(db=db, llm=llm)
    await barrier.trigger("owner", "child")
    await barrier.wait("owner", "child")
    state = sessions.get("child", user_id="owner")["alignment_state"]
    assert (
        state == ("ready" if payload["aligned"] else "misaligned")
        if valid
        else state != "ready"
    )
    sessions.set_alignment_state("child", user_id="owner", state="pending")
    request = dict(
        user_id="owner",
        session_id="child",
        agent_id=None,
        tool="read",
        args={"path": "/repo/test"},
    )
    if valid:
        # A balanced static read is exempt from an ungrounded misalignment.
        assert evaluator.evaluate(**request)["decision"] == "approve"
    else:
        with pytest.raises(HybridError) as failure:
            evaluator.evaluate(**request)
        assert failure.value.code == "alignment_unavailable"
        assert failure.value.status == 503
        assert failure.value.retryable
        assert audit.get_recent("child", user_id="owner", limit=10) == []


def test_alignment_reload_refreshes_policy_hash_expectation(stores, monkeypatch):
    from intaris.alignment import AlignmentBarrier
    from intaris.evaluator import Evaluator

    db, sessions, audit, llm, evaluator = stores
    evaluator = Evaluator(
        llm=llm,
        session_store=sessions,
        audit_store=audit,
        alignment_barrier=AlignmentBarrier(db=db, llm=llm),
    )
    sessions.create(user_id="owner", session_id="parent", intention="Fix tests")
    child = sessions.create(
        user_id="owner",
        session_id="child",
        intention="Supporting work",
        parent_session_id="parent",
    )
    llm.generate.return_value = json.dumps({"aligned": True, "reasoning": "Supporting"})
    original = sessions.set_alignment_state

    def commit_then_activate(*args, **kwargs):
        result = original(*args, **kwargs)
        sessions.update_session(
            "child",
            user_id="owner",
            policy={"deny_tools": ["forbidden_external_publish"]},
        )
        return result

    monkeypatch.setattr(sessions, "set_alignment_state", commit_then_activate)
    with pytest.raises(HybridError, match="Policy hash changed"):
        evaluator.evaluate(
            user_id="owner",
            session_id="child",
            agent_id=None,
            tool="read",
            args={"path": "/repo/test"},
            expected_policy_hash=child["policy_hash"],
        )
    assert not audit.get_recent("child", user_id="owner", limit=10)
