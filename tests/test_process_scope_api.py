import json
import time
from unittest.mock import Mock

import jwt
import pytest

from intaris.hybrid import digest
from intaris.policy_assessment import PolicyAssessment
from tests.test_hybrid_api import api as api
from tests.test_process_scope import declaration


def signed(client, key, scope):
    now = int(time.time())
    claims = {
        "iss": "cognis",
        "aud": "intaris",
        "sub": "owner",
        "agent_id": "agent",
        "typ": "controller",
        "session_id": scope.session_id,
        "jti": "process-test",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "capabilities": ["intaris:controller_context", "intaris:process_lifecycle"],
        "process_scope_digest": digest(scope.model_dump()),
    }
    client.headers["Authorization"] = "Bearer " + jwt.encode(
        claims, key, algorithm="ES256"
    )


@pytest.mark.parametrize("review_cleanup", [False, True])
def test_admitted_spawn_then_exact_cleanup_with_real_capability(api, review_cleanup):
    import intaris.server as srv

    client, _, key = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "agent",
        }
    )
    scope, args, _ = declaration()
    response = client.post(
        "/api/v1/intention",
        json={
            "session_id": scope.session_id,
            "intention": "Use a temporary helper to inspect the task",
            "policy": {
                "evaluation_policy_version": "hybrid_v1",
                # Fact-first balanced routing tolerates unresolved restriction
                # opinions; strict retains the independent-review branch.
                "profile": "strict" if review_cleanup else "balanced",
            },
        },
    )
    assert response.status_code == 200, response.text
    answers = PolicyAssessment(
        relationship="supports_intent",
        risk="medium",
        effects=["execute"],
        mutation="mutating",
        contextual_restrictions="consistent",
        target_scope="consistent",
        target_reference="action",
    ).model_dump()
    srv._get_evaluator()._llm.generate.side_effect = lambda *a, **kw: json.dumps(
        {field: answers[field] for field in kw["json_schema"]["schema"]["properties"]}
    )
    signed(client, key, scope)
    spawned = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": scope.session_id,
            "tool": "bash",
            "args": args,
            "process_scope": scope.model_dump(),
        },
    )
    assert spawned.status_code == 200, spawned.text
    assert spawned.json()["decision"] == "approve"
    assert spawned.json()["evaluation_metadata"]["process_lifecycle"]["authenticated"]
    cleanup, args, _ = declaration("terminate")
    cleanup = cleanup.model_copy(update={"spawn_call_id": spawned.json()["call_id"]})
    if review_cleanup:
        from intaris.config import JudgeConfig
        from intaris.judge import JudgeReviewer

        answers["contextual_restrictions"] = "unresolved"
        answers["risk"] = "low"
        llm = Mock(_timeout_ms=15000)
        llm.generate.return_value = json.dumps(
            {
                "user_would": "approve",
                "risk": "low",
                "confidence": "high",
                "basis": "routine_within_task",
                "cites": [],
                "question": "",
                "action_summary": "Stop the temporary diagnostic helper.",
                "explanation": "Task-owned cleanup is permitted by the current restrictions.",
                "restriction_quote": "",
            }
        )
        evaluator = srv._get_evaluator()
        reviewer = JudgeReviewer(
            llm=llm,
            config=JudgeConfig(mode="auto"),
            audit_store=evaluator._audit,
            session_store=evaluator._sessions,
            evaluator=evaluator,
        )
        for route in client.app.routes:
            if getattr(route, "path", None) == "/api/v1":
                route.app.state.judge_reviewer = reviewer
    signed(client, key, cleanup)
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": cleanup.session_id,
            "tool": "bash_kill",
            "args": args,
            "process_scope": cleanup.model_dump(),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    if review_cleanup:
        llm.generate.assert_called_once()
        metadata = response.json()["evaluation_metadata"]
        assert not metadata["final_policy_result"]["outstanding_obligations"]
    else:
        assert (
            response.json()["evaluation_metadata"]["policy_result"]["permission_basis"]
            == "task_process_lifecycle"
        )
    # A controller capability for one exact process cannot cover altered arguments.
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": cleanup.session_id,
            "tool": "bash_kill",
            "args": {"shell_id": "another-process"},
            "process_scope": cleanup.model_dump(),
        },
    )
    assert response.status_code == 403


def test_ordinary_token_cannot_supply_lifecycle_scope(api):
    client, _, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "agent",
        }
    )
    scope, args, _ = declaration()
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": scope.session_id,
            "tool": scope.tool,
            "args": args,
            "process_scope": scope.model_dump(),
        },
    )
    assert response.status_code == 403
