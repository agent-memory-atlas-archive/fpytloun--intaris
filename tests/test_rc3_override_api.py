from unittest.mock import AsyncMock

import pytest

from tests.test_hybrid_api import api as api


@pytest.mark.parametrize("idle", [False, True])
def test_operator_override_skips_alignment_wait_and_preserves_error_shape(
    api, monkeypatch, caplog, idle
):
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
                "session_id": "rollback",
                "intention": "Inspect files",
            },
        ).status_code
        == 200
    )
    monkeypatch.setenv("EVALUATION_POLICY_FORCE", "legacy")
    from intaris.session import SessionStore
    from intaris.workflow_scope import WorkflowScopeStore
    from tests.test_workflow_scope import CONTEXT, declaration

    sessions = SessionStore(srv._get_db())
    if version == "hybrid_v1":
        WorkflowScopeStore(srv._get_db()).install(
            declaration("rollback"),
            user_id="owner",
            agent_id="hook",
            claims=CONTEXT,
        )
    if idle:
        sessions.update_status("rollback", "idle", user_id="owner")
    original_policy = sessions.get("rollback", user_id="owner")["policy"]
    for route in client.app.routes:
        if getattr(route, "path", None) == "/api/v1":
            route.app.state.alignment_barrier = AsyncMock()
            route.app.state.alignment_barrier.wait.side_effect = AssertionError(
                "No wait under rollback"
            )
            break
    else:
        raise AssertionError("Missing API mount")
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "rollback",
            "tool": "read",
            "args": {"file_path": "/repo/README"},
            "evaluation_policy_version": "hybrid_v1",
            "expected_policy_revision": 999,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    assert sessions.get("rollback", user_id="owner")["policy"] == original_policy
    srv._get_evaluator()._llm.generate.return_value = (
        '{"aligned":true,"risk":"low","decision":"approve","reasoning":"test"}'
    )
    blocked = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "rollback",
            "tool": "write",
            "args": {"file_path": "/repo/file"},
            "context": {"chat_mode": "plan", "read_only_required": True},
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["decision"] == "deny"
    resolved = client.post(
        "/api/v1/decision",
        json={
            "call_id": blocked.json()["call_id"],
            "decision": "approve",
        },
    )
    assert resolved.status_code == 409, resolved.text
    assert resolved.json()["detail"]["code"] == "read_only_ceiling"
    assert any(
        getattr(record, "hybrid_error", {}).get("code") == "read_only_ceiling"
        for record in caplog.records
    )
