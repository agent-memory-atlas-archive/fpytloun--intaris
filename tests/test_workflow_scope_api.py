"""JWT authority and declaration-only HTTP behavior."""

import json
import time
from unittest.mock import Mock

import jwt
import pytest

from intaris.hybrid import digest
from tests.test_hybrid_api import api as api
from tests.test_workflow_scope import ARGS, CONTEXT, declaration


def setup(api):
    client, _, key = api
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
            "session_id": "workflow-api",
            "intention": "Read repository",
            "policy": {
                "evaluation_policy_version": "hybrid_v1",
                "maximum_outcome": "approve",
            },
        },
    )
    assert response.status_code == 200, response.text
    body = declaration("workflow-api").model_dump()
    now = int(time.time())
    claims = {
        "iss": "cognis",
        "aud": "intaris",
        "sub": "owner",
        "agent_id": "hook",
        "typ": "controller",
        "session_id": "workflow-api",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": "workflow-test",
        "capabilities": ["intaris:controller_context"],
        **CONTEXT,
        "declaration_digest": digest(body),
    }
    return client, key, body, claims


def sign(client, key, claims):
    client.headers["Authorization"] = "Bearer " + jwt.encode(
        claims, key, algorithm="ES256"
    )


def test_declaration_only_and_exact_evaluation(api):
    client, key, body, claims = setup(api)
    before = client.get("/api/v1/session/workflow-api").json()
    sign(client, key, claims)
    app = client.app.state._api_app
    event_bus = app.state.event_bus
    app.state.event_bus = Mock()
    try:
        first = client.post(
            "/api/v1/intention",
            json={"session_id": "workflow-api", "workflow_step": body},
        )
        assert first.status_code == 200, first.text
        assert set(first.json()) == {"ok", "declaration_digest", "admission_revision"}
        assert first.json()["declaration_digest"] == digest(body)
        assert (
            client.post(
                "/api/v1/intention",
                json={"session_id": "workflow-api", "workflow_step": body},
            ).json()
            == first.json()
        )
        app.state.event_bus.publish.assert_not_called()
    finally:
        app.state.event_bus = event_bus
    after = client.get("/api/v1/session/workflow-api").json()
    assert before["intention"] == after["intention"]
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "workflow-api",
            "tool": "read",
            "args": ARGS,
            "logical_call_id": "call-1",
            "controller_context": CONTEXT,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "approve"
    assert response.json()["evaluation_metadata"]["workflow_scope"]["matched"]


@pytest.mark.parametrize(
    "binding",
    [
        "ordinary",
        "declaration_digest",
        "session_id",
        "sub",
        "agent_id",
        "governing_execution_id",
        "governing_snapshot_id",
    ],
)
def test_ordinary_or_misbound_auth_cannot_install(api, binding):
    client, key, body, claims = setup(api)
    if binding != "ordinary":
        if binding.startswith("governing_"):
            claims.pop(binding)
        else:
            claims[binding] = "another-binding"
        sign(client, key, claims)
    response = client.post(
        "/api/v1/intention", json={"session_id": "workflow-api", "workflow_step": body}
    )
    assert response.status_code in ({409} if binding == "sub" else {401, 403}), (
        response.text
    )


def test_conflicting_body_and_session_update_cannot_overwrite(api):
    client, key, body, claims = setup(api)
    sign(client, key, claims)
    assert (
        client.post(
            "/api/v1/intention",
            json={"session_id": "workflow-api", "workflow_step": body},
        ).status_code
        == 200
    )
    changed = {**body, "tool": "write"}
    claims["declaration_digest"] = digest(changed)
    sign(client, key, claims)
    response = client.post(
        "/api/v1/intention",
        json={"session_id": "workflow-api", "workflow_step": changed},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workflow_step_conflict"
    response = client.post(
        "/api/v1/intention",
        json={
            "session_id": "workflow-api",
            "workflow_step": body,
            "intention": "Replace scope",
        },
    )
    assert response.status_code == 422
    client.headers["Authorization"] = "Bearer hybrid-test-key"
    response = client.patch(
        "/api/v1/session/workflow-api",
        json={
            "details": {"source": "controller_workflow_step", "workflow_step": changed},
            "workflow_step": changed,
        },
    )
    assert response.status_code in {200, 422}, response.text
    sign(client, key, claims)
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "workflow-api",
            "tool": "write",
            "args": ARGS,
            "logical_call_id": "call-1",
            "controller_context": CONTEXT,
        },
    )
    assert response.status_code == 200, response.text
    # The rejected overwrite must not grant scope: the original declaration
    # still mismatches this call, which is then evaluated normally.
    scope = (response.json().get("evaluation_metadata") or {}).get("workflow_scope")
    assert scope is not None and not scope.get("matched")
    assert scope.get("reason_code") == "workflow_step.declaration_mismatch"


def test_declared_scope_never_enters_judge_citations(api):
    from intaris.hybrid import evaluate_policy
    from intaris.judge_simulator import request
    from intaris.policy_evidence import from_observations

    _, _, body, _ = setup(api)
    state = from_observations(
        {"tool": "read", "mutation": "read_only", "effects": ["read"]}
    )
    state = state.model_copy(
        update={"workflow_scope": {"matched": True, "declaration": body}}
    )
    prepared = request(
        {"tool": "read", "args_redacted": ARGS},
        {"intention": "Read"},
        state,
        evaluate_policy([], state),
        {
            "items": [],
            "originals": [
                {
                    "ref": "workflow-declaration",
                    "origin": "controller_workflow_step",
                    "content": json.dumps(body),
                }
            ],
        },
    )
    assert "workflow-declaration" not in prepared["citable"].values()
    assert set(prepared["citable"].values()) == {"session_intention"}
    # Common factual context is not an independent grant source.
    assert state.sources == {}
