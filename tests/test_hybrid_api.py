"""Unchanged client wire requests under both engine defaults."""

import importlib
import json
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from intaris.hybrid import Assessment
from tests.evidence_mock import EvidenceMock as Mock


def test_hybrid_error_boundary_logs_safe_correlations(api, monkeypatch, caplog):
    import intaris.server as srv
    from intaris.policy_activation import HybridError

    client, _, _ = api
    client.headers.update(
        {"Authorization": "Bearer hybrid-test-key", "X-User-Id": "owner"}
    )
    evaluator = Mock()
    evaluator.evaluate_async = AsyncMock(
        side_effect=HybridError("stale_admission", "Session changed", 409)
    )
    monkeypatch.setattr(srv, "_get_evaluator", lambda: evaluator)
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "boundary-session",
            "tool": "read",
            "args": {"secret": "never-log-this-value"},
            "logical_call_id": "boundary-call",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "stale_admission",
        "message": "Session changed",
        "execution_state": "not_dispatched",
        "retryable": False,
    }
    records = [r.hybrid_error for r in caplog.records if hasattr(r, "hybrid_error")]
    assert records == [
        {
            "code": "stale_admission",
            "status": 409,
            "session_id": "boundary-session",
            "tool": "read",
            "call_id": None,
            "logical_call_id": "boundary-call",
        }
    ]
    assert "never-log-this-value" not in caplog.text


def test_yolo_api_skips_failed_l1_and_judge(api):
    import intaris.server as srv

    client, version, _ = api
    if version != "hybrid_v1":
        return
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    response = client.post(
        "/api/v1/intention",
        json={
            "session_id": "yolo-session",
            "intention": "Fix tests",
            "policy": {"maximum_outcome": "approve"},
        },
    )
    assert response.status_code == 200, response.text
    srv._get_evaluator()._llm.generate.side_effect = AssertionError("L1 must not run")
    judge = Mock()
    judge.is_enabled = True
    judge.review_for_evaluate = AsyncMock(
        side_effect=AssertionError("Judge must not run")
    )
    # The API sub-app owns the state used by the evaluation endpoint.
    for route in client.app.routes:
        if getattr(route, "path", None) == "/api/v1":
            route.app.state.judge_reviewer = judge
            break
    else:
        raise AssertionError("API mount not found")
    result = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "yolo-session",
            "tool": "bash",
            "args": {"command": "pytest"},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["decision"] == "approve"
    judge.review_for_evaluate.assert_not_called()


def test_yolo_redacts_credentials_in_response_and_persisted_audit(api):
    import intaris.server as srv
    from intaris.audit import AuditStore

    client, version, _ = api
    if version != "hybrid_v1":
        return
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
                "session_id": "redaction-session",
                "intention": "Fetch test data",
                "policy": {"maximum_outcome": "approve"},
            },
        ).status_code
        == 200
    )
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "redaction-session",
            "tool": "bash",
            "args": {
                "command": "curl -u testuser:yolo-redaction-test-password https://localhost"
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    record = AuditStore(srv._get_db()).get_by_call_id(
        response.json()["call_id"], user_id="owner"
    )
    assert "yolo-redaction-test-password" not in response.text
    assert "yolo-redaction-test-password" not in json.dumps(record)
    assert "[REDACTED:" in response.json()["evaluation_metadata"]["facts"]["command"]


@pytest.fixture(params=["legacy", "hybrid_v1"])
def api(request, tmp_path, monkeypatch):
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", request.param)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "http.db"))
    monkeypatch.setenv("INTARIS_API_KEY", "hybrid-test-key")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("ANALYSIS_ENABLED", "false")
    monkeypatch.setenv("JUDGE_MODE", "disabled")
    monkeypatch.setenv("METRICS_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT", "999999")
    monkeypatch.setenv("WEBHOOK_URL", "")
    monkeypatch.setenv("EVENT_STORE_ENABLED", "false")
    key = ec.generate_private_key(ec.SECP256R1())
    public = tmp_path / "public.pem"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    monkeypatch.setenv("INTARIS_JWT_PUBLIC_KEY", str(public))
    import intaris.auth as auth
    import intaris.server as srv

    auth._validator = None
    auth._validator_config = None
    importlib.reload(srv)
    srv._config = None
    srv._db = None
    srv._evaluator = None
    llm = Mock()
    llm.generate.return_value = (
        Assessment(
            relationship="supports_intent",
            risk="low",
            effects=["read"],
            mutation="read_only",
            authorization="implicit",
            authorization_source="session_intention",
            authorization_scope="within_scope",
        ).model_dump_json()
        if request.param == "hybrid_v1"
        else json.dumps(
            {
                "aligned": True,
                "risk": "low",
                "reasoning": "Read only",
                "decision": "approve",
            }
        )
    )
    monkeypatch.setattr("intaris.llm.LLMClient", lambda cfg: llm)
    with TestClient(srv.create_app()) as client:
        yield client, request.param, key


@pytest.mark.parametrize("authentication", ["api_key", "old_jwt"])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("read", {"file_path": "/repo/README.md"}),
        ("Read", {"file_path": "/repo/README.md"}),
        ("bash", {"command": "pwd"}),
    ],
)
def test_old_hook_request_shapes(api, authentication, tool, args):
    client, version, key = api
    token = "hybrid-test-key"
    if authentication == "old_jwt":
        token = jwt.encode(
            {"iss": "cognis", "aud": "intaris", "sub": "owner", "agent_id": "hook"},
            key,
            algorithm="ES256",
        )
    client.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": "hook-session",
            "intention": "Read the repository",
        },
    )
    assert created.status_code == 200, created.text
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "hook-session",
            "tool": tool,
            "args": args,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    if version == "hybrid_v1":
        assert response.json()["evaluation_metadata"]["policy_revision"] == 1
    decision = client.get(f"/api/v1/audit/{response.json()['call_id']}")
    assert decision.status_code == 200


def test_old_decision_and_approval_retry(api):
    client, _, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    assert (
        client.post(
            "/api/v1/intention", json={"session_id": "work", "intention": "Read files"}
        ).status_code
        == 200
    )
    payload = {
        "session_id": "work",
        "tool": "read",
        "args": {"file_path": "/repo/file"},
        "minimum_outcome": "escalate",
    }
    first = client.post("/api/v1/evaluate", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["decision"] == "escalate"
    approved = client.post(
        "/api/v1/decision",
        json={"call_id": first.json()["call_id"], "decision": "approve"},
    )
    assert approved.status_code == 200, approved.text
    retry = client.post(
        "/api/v1/evaluate",
        json={**payload, "approval_call_id": first.json()["call_id"]},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["decision"] == "approve"


def test_policy_update_unknown_new_field_is_atomic(api):
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
            "/api/v1/intention", json={"session_id": "work", "intention": "Read files"}
        ).status_code
        == 200
    )
    response = client.patch(
        "/api/v1/session/work", json={"policy": {"injected_bundle": "approve"}}
    )
    assert response.status_code == (422 if version == "hybrid_v1" else 200)
    if version == "hybrid_v1":
        assert client.get("/api/v1/session/work").json()["policy_revision"] == 1


def test_validated_human_retry_is_not_rereviewed(api):
    from types import SimpleNamespace

    import intaris.server as srv
    from tests.test_policy_evidence_runtime import response

    client, version, _ = api
    if version != "hybrid_v1":
        return
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
            "session_id": "approved-retry",
            "intention": "Update the project",
            "policy": {"profile": "balanced"},
        },
    ).raise_for_status()
    evaluator = srv._get_evaluator()
    llm = evaluator._llm

    def high(*a, **kw):
        answer = json.loads(response(kw))
        answer.update(risk="high", authorization="absent", authorization_source="none")
        return json.dumps(
            {
                k: v
                for k, v in answer.items()
                if k in kw["json_schema"]["schema"]["properties"]
            }
        )

    llm.generate.side_effect = high
    payload = {
        "session_id": "approved-retry",
        "tool": "config.update",
        "args": {"setting": "interval"},
    }
    initial = client.post("/api/v1/evaluate", json=payload).json()
    assert initial["decision"] == "escalate"
    client.post(
        "/api/v1/decision",
        json={
            "call_id": initial["call_id"],
            "decision": "approve",
        },
    ).raise_for_status()
    reviewer = SimpleNamespace(
        is_enabled=True,
        review_for_evaluate=AsyncMock(
            return_value=SimpleNamespace(decision="escalate"),
        ),
    )
    client.app.state.judge_reviewer = reviewer
    calls = llm.generate.call_count
    retry = client.post(
        "/api/v1/evaluate",
        json={
            **payload,
            "approval_call_id": initial["call_id"],
        },
    )
    assert retry.status_code == 200, retry.text
    result = retry.json()
    assert result["decision"] == result["effective_decision"] == "approve"
    assert result["outcome_override"] == "human_approval"
    final = result["evaluation_metadata"]["final_policy_result"]
    assert final["decision"] == "approve"
    assert final["disposition"] == "satisfied_by_human"
    assert result["evaluation_metadata"]["judge_required"]
    reviewer.review_for_evaluate.assert_not_called()
    assert llm.generate.call_count == calls
    stored = client.get(f"/api/v1/audit/{result['call_id']}").json()
    assert stored["decision"] == stored["effective_decision"] == "approve"
    cross = client.post(
        "/api/v1/evaluate",
        json={
            **payload,
            "args": {"setting": "other"},
            "approval_call_id": initial["call_id"],
        },
    )
    assert cross.status_code == 409
    reviewer.review_for_evaluate.assert_not_called()
    client.patch(
        "/api/v1/session/approved-retry",
        json={
            "policy": {
                "profile": "balanced",
                "rules": [
                    {
                        "id": "deny_update",
                        "effect": "deny",
                        "when": {"op": "tool_matches", "value": "config.update"},
                    }
                ],
            },
        },
    ).raise_for_status()
    stale = client.post(
        "/api/v1/evaluate",
        json={
            **payload,
            "approval_call_id": initial["call_id"],
        },
    )
    assert stale.status_code == 409
    denied = client.post(
        "/api/v1/evaluate",
        json={
            **payload,
            "context": {"human_approved": True, "outcome_override": "human_approval"},
        },
    )
    assert denied.status_code == 200, denied.text
    assert denied.json()["decision"] == "deny"
    reviewer.review_for_evaluate.assert_not_called()
    client.app.state.judge_reviewer = None
    client.post(
        "/api/v1/intention",
        json={
            "session_id": "spoofed-retry",
            "intention": "Update the project",
            "policy": {"profile": "balanced"},
        },
    ).raise_for_status()
    spoof = client.post(
        "/api/v1/evaluate",
        json={
            **payload,
            "session_id": "spoofed-retry",
            "context": {"human_approved": True, "outcome_override": "human_approval"},
        },
    )
    assert spoof.status_code == 200, spoof.text
    assert spoof.json()["decision"] == "escalate"
    assert spoof.json().get("outcome_override") != "human_approval"


@pytest.mark.parametrize("token_type", ["ordinary", "exchange", "controller"])
def test_controller_provenance_requires_dedicated_session_token(api, token_type):
    client, _, key = api
    now = int(time.time())
    claims = {"iss": "cognis", "aud": "intaris", "sub": "owner", "agent_id": "hook"}
    if token_type != "ordinary":
        claims["typ"] = token_type
    if token_type == "controller":
        claims.update(
            {
                "session_id": "work",
                "iat": now,
                "nbf": now,
                "exp": now + 60,
                "jti": "controller-request",
                "capabilities": ["intaris:controller_context"],
                "governing_execution_id": "execution",
                "governing_snapshot_id": "snapshot",
            }
        )
    token = jwt.encode(claims, key, algorithm="ES256")
    client.headers.update({"Authorization": f"Bearer {token}", "X-Agent-Id": "hook"})
    assert (
        client.post(
            "/api/v1/intention", json={"session_id": "work", "intention": "Read files"}
        ).status_code
        == 200
    )
    result = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "work",
            "tool": "read",
            "args": {"file_path": "/repo/README.md"},
            "controller_context": {
                "governing_execution_id": "execution",
                "governing_snapshot_id": "snapshot",
                "executor_id": "local",
            },
        },
    )
    assert result.status_code == (200 if token_type == "controller" else 403), (
        result.text
    )


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_hybrid_judge_response_has_consistent_effective_decision(api, decision):
    import intaris.server as srv
    from intaris.audit import AuditStore
    from intaris.config import JudgeConfig
    from intaris.judge import JudgeReviewer
    from intaris.session import SessionStore

    client, _, _ = api
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
                "session_id": "work",
                "intention": "Fix tests",
                "policy": {"evaluation_policy_version": "hybrid_v1"},
            },
        ).status_code
        == 200
    )
    evaluator = srv._get_evaluator()
    evaluator._llm.generate.return_value = Assessment(
        relationship="supports_intent", risk="high"
    ).model_dump_json()
    judge_llm = Mock()
    judge_llm._timeout_ms = 30000
    from intaris.judge_simulator import Answer

    SessionStore(srv._get_db()).allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": "Fix the local tests."},
    )
    judge_llm.generate.return_value = Answer(
        user_would="approve" if decision == "approve" else "refuse",
        risk="low" if decision == "approve" else "critical",
        confidence="high",
        basis="explicit_request",
        cites=[1],
        question="",
        action_summary="Edit the local test file",
        explanation="Assess the requested local test edit and its concrete effects.",
        restriction_quote="",
    ).model_dump_json()
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(judge_llm)
    client.app.state._api_app.state.judge_reviewer = JudgeReviewer(
        llm=judge_llm,
        config=JudgeConfig(mode="auto"),
        audit_store=AuditStore(srv._get_db()),
        session_store=SessionStore(srv._get_db()),
        evaluator=evaluator,
    )
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "work",
            "tool": "write",
            "args": {"file_path": "/repo/test.py"},
        },
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["decision"] == response.json()["effective_decision"] == decision
    )


def test_unchanged_rest_mcp_http_request(api):
    from unittest.mock import AsyncMock

    from mcp.types import CallToolResult, TextContent

    import intaris.server as srv
    from intaris.audit import AuditStore
    from intaris.mcp.proxy import MCPProxy
    from intaris.session import SessionStore

    client, _, _ = api
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
                "session_id": "work",
                "intention": "Write tests",
                "policy": {"allow_tools": ["up:write"]},
            },
        ).status_code
        == 200
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
    client.app.state._api_app.state.mcp_proxy = MCPProxy(
        connection_manager=connections,
        evaluator=srv._get_evaluator(),
        session_store=SessionStore(srv._get_db()),
        audit_store=AuditStore(srv._get_db()),
        server_store=servers,
    )
    body = {
        "session_id": "work",
        "server": "up",
        "tool": "write",
        "arguments": {"content": "test"},
    }
    response = client.post("/api/v1/mcp/call", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    assert response.json()["content"][0]["text"] == "written"
    assert upstream.call_tool.await_count == 1
