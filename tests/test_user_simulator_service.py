import uuid

import pytest

from tests.test_api import _reset_server_globals, env_no_auth, tmp_db  # noqa: F401
from tests.test_source_context_service import (
    reason,
    record,
)
from tests.test_source_context_service import (
    service as service,
)
from tests.test_source_context_service import (
    service_databases as service_databases,
)


@pytest.mark.parametrize(
    "service", [("jev", "source_context", "single", "sqlite")], indirect=True
)
@pytest.mark.parametrize("maximum", [None, "escalate", "approve"])
@pytest.mark.parametrize("minimum", [None, "escalate"])
def test_raw_review_runs_judge_unless_operator_bound(service, maximum, minimum):
    client, app, wire = service
    sid = "simulator-" + uuid.uuid4().hex
    policy = {
        "evaluation_policy_version": "hybrid_v1",
        "profile": "balanced",
        "judge": {
            "allowed_decisions": ["approve", "deny", "defer"],
            "on_uncertain": "defer",
        },
    }
    if maximum:
        policy["maximum_outcome"] = maximum
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": sid,
            "intention": "Maintain the inbox",
            "policy": policy,
        },
    )
    assert created.status_code == 200, created.text
    record(
        client,
        sid,
        {"type": "user_message", "data": {"content": "Delete the selected duplicate."}},
    )
    reason(client, sid)
    payload = {"session_id": sid, "tool": "inbox.delete", "args": {"ids": ["selected"]}}
    if minimum:
        payload["minimum_outcome"] = minimum
    result = client.post("/api/v1/evaluate", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["decision"] == ("escalate" if minimum else "approve")
    if maximum == "approve":
        assert not any(
            stage in {"judge", "l1-jev", "task-context"} for stage, body in wire
        )
        assert result.json()["evaluation_metadata"]["operator_bound"]
        return
    assert [stage for stage, body in wire].count("judge") == 1
    l1 = next(body for stage, body in wire if stage == "l1-jev")
    assert "mutation" not in l1["questions"]
    assert not any(key.startswith("effect_") for key in l1["questions"])


@pytest.mark.parametrize(
    "service", [("jev", "source_context", "single", "sqlite")], indirect=True
)
def test_read_name_is_not_a_read_only_proof(service):
    client, app, wire = service
    sid = "read-name-" + uuid.uuid4().hex
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": sid,
            "intention": "Inspect the local repository",
            "policy": {
                "evaluation_policy_version": "hybrid_v1",
                "profile": "bounded_autonomy",
                "read_only_required": True,
            },
        },
    )
    assert created.status_code == 200
    result = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": sid,
            "tool": "service.get_and_delete",
            "args": {"delete": True},
        },
    )
    assert result.status_code == 200, result.text
    l1 = next(body for stage, body in wire if stage == "l1-jev")
    assert "mutation" in l1["questions"]
    assert "effect_delete" in l1["questions"]
    assert result.json()["decision"] != "approve"


@pytest.mark.parametrize(
    "service", [("jev", "source_context", "single", "sqlite")], indirect=True
)
@pytest.mark.parametrize("maximum", [None, "approve"])
def test_human_can_override_judge_denial_unless_operator_skips_judge(service, maximum):
    client, app, wire = service
    sid = "human-override-" + uuid.uuid4().hex
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": sid,
            "intention": "Maintain the inbox",
            "policy": {
                "evaluation_policy_version": "hybrid_v1",
                **({"maximum_outcome": maximum} if maximum else {}),
                "judge": {"allowed_decisions": ["deny"], "on_uncertain": "deny"},
            },
        },
    )
    assert created.status_code == 200, created.text
    record(
        client,
        sid,
        {"type": "user_message", "data": {"content": "Delete the selected duplicate."}},
    )
    reason(client, sid)
    result = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": sid,
            "tool": "inbox.delete",
            "args": {"ids": ["selected"]},
        },
    )
    assert result.status_code == 200, result.text
    if maximum == "approve":
        assert result.json()["decision"] == "approve"
        assert not any(stage == "judge" for stage, body in wire)
        return
    assert result.json()["decision"] == "deny"
    resolution = client.post(
        "/api/v1/decision",
        json={
            "call_id": result.json()["call_id"],
            "decision": "approve",
        },
    )
    assert resolution.status_code == 200, resolution.text


@pytest.mark.parametrize(
    "service", [("jev", "source_context", "single", "sqlite")], indirect=True
)
@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
def test_jev_invalid_response_escalates_instead_of_http_error(
    service, monkeypatch, version
):
    from intaris.jev import JevClient

    client, app, wire = service
    sid = "invalid-jev-" + uuid.uuid4().hex
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": sid,
            "intention": "Maintain inbox",
            "policy": {"evaluation_policy_version": version},
        },
    )
    assert created.status_code == 200, created.text
    # A real malformed provider shape exercises the owning validation boundary.
    monkeypatch.setattr(JevClient, "_request", lambda *a, **kw: {"answers": {}})
    app.state._api_app.state.judge_reviewer = None
    result = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": sid,
            "tool": "inbox.delete",
            "args": {"ids": ["selected"]},
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["decision"] == "escalate"
    assert "validation failed" in result.json()["reasoning"]
    if version == "hybrid_v1":
        metadata = result.json()["evaluation_metadata"]
        assert metadata["policy_evidence"]["assessment_failed"]
        assert any(
            o["id"] == "assessment.invalid_response" and o["requirement"] == "human"
            for o in metadata["outstanding_obligations"]
        )
