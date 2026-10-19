"""Actual app/SDK acceptance: routing, persisted metadata and linked scope."""

import pytest

from intaris.hybrid import digest
from tests.test_api import _reset_server_globals, env_no_auth, tmp_db  # noqa: F401
from tests.test_source_context_service import (
    create,
    evaluate,
    reason,
    record,
    service,
    service_databases,  # noqa: F401
)

service = service


@pytest.mark.parametrize(
    "service,expected",
    [
        (("llm", "source_context", "single", "sqlite", "auto"), "lines"),
        (("llm", "source_context", "single", "sqlite", "json"), "json"),
        (("llm", "source_context", "single", "sqlite", "auto", "gpt-5.4-nano"), "json"),
        (("jev", "source_context", "single", "sqlite", "auto"), "lines"),
        (("jev", "source_context", "single", "sqlite", "json"), "json"),
        (("jev", "source_context", "single", "sqlite", "lines"), "lines"),
    ],
    indirect=["service"],
)
def test_actual_service_projection_and_metadata(service, expected):
    client, app, wire = service
    sid = create(client)
    record(
        client,
        sid,
        {
            "type": "assistant_message",
            "data": {"content": "May I send exactly draft X to Y?"},
        },
        {"type": "user_message", "data": {"content": "yes"}},
    )
    reason(client, sid)
    result = evaluate(client, sid)
    assert not [body for stage, body in wire if stage == "mock-error"]
    metadata = result["evaluation_metadata"]
    info = metadata["context_input"]
    assert info["l1_projection"] == expected
    assert info["l1_invoked"] is True
    assert info["l1_view_version"] == info["l1_projection_version"]
    stage, body = next((s, b) for s, b in wire if s in {"l1-gpt", "l1-jev"})
    if stage == "l1-gpt":
        prompt = body["messages"][1]["content"]
        assert metadata["stages"][0]["system_characters"] == len(
            body["messages"][0]["content"]
        )
    else:
        combined = body["state"]["prompt"]
        if expected == "lines":
            prompt = "Action: " + combined.split("\nAction: ", 1)[1]
        else:
            prompt = combined[combined.index('{"action":') :]
    assert info["l1_prompt_digest"] == digest(prompt)
    assert metadata["stages"][0]["input_projection"] == expected
    if expected == "lines":
        assert prompt.startswith("Action: write\n")
        assert "Approved action:" not in prompt
        assert "May I send exactly draft X to Y?" not in prompt
        assert "source_ref" not in prompt
        assert "authorization_source" not in metadata["policy_evidence"]["assessment"]
        # The separate single-profile service tests verify the linked proof.
        # This boundary must not turn that proof into L1 authorization fields.
        assert metadata["task_context"]["status"] == "ready"
    audited = client.get(f"/api/v1/audit/{result['call_id']}")
    assert audited.status_code == 200, audited.text
    assert (
        audited.json()["evaluation_metadata"]["context_input"]["l1_projection"]
        == expected
    )


@pytest.mark.parametrize(
    "service", [("llm", "source_context", "single", "sqlite", "lines")], indirect=True
)
def test_static_denial_has_no_projection_used(service):
    client, app, wire = service
    sid = create(client)
    result = evaluate(client, sid, tool="bash", args={"command": "rm -rf /"})
    assert result["decision"] == "deny"
    info = result["evaluation_metadata"]["context_input"]
    assert info["l1_projection_requested"] == "lines"
    assert info["l1_projection"] is None
    assert info["l1_invoked"] is False
    assert info["l1_view_version"] is None
    assert not [body for stage, body in wire if stage in {"l1-gpt", "l1-jev"}]
