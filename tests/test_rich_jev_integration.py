"""Merged single-profile adapter through actual app and offline L1 transports."""

import copy
import json
from functools import partial

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
MATRIX = [
    (backend, "source_context", "single", db, projection)
    for backend in ("llm", "jev")
    for db in ("sqlite", "postgresql")
    for projection in ("lines", "json")
]


def test_size_comparison_uses_pinned_renderer_not_merged_generation(monkeypatch):
    from intaris import task_context
    from tools.l1_projection_sizes import base_messages, specimen

    artifact, state, obligations = specimen("inbox-inspection-only")
    expected = base_messages(artifact, state, obligations)

    def live_renderer(*args, **kwargs):
        raise AssertionError("Pinned baseline must not use the live module")

    monkeypatch.setattr(task_context, "render", live_renderer)
    assert base_messages(artifact, state, obligations) == expected
    assert task_context.render is live_renderer


def l1_prompt(wire, backend):
    stage = "l1-gpt" if backend == "llm" else "l1-jev"
    body = next(body for s, body in wire if s == stage)
    if backend == "llm":
        return body["messages"][1]["content"], body
    combined = body["state"]["prompt"]
    if "\nAction: " in combined:
        return "Action: " + combined.split("\nAction: ", 1)[1], body
    return combined[combined.index('{"action":') :], body


def rich_json(prompt):
    return json.loads(prompt)


def check_contract(result, wire, backend, projection):
    artifact = result["evaluation_metadata"]["task_context"]
    assert "profile" not in artifact
    assert "profile_sources" not in artifact
    assert artifact["extraction"]["calls"] == 0
    assert artifact["extraction_calls_this_evaluation"] == 0
    assert artifact["compact_intention"] == ""
    for item in artifact["items"]:
        source = next(
            r for r in artifact["originals"] if r["ref"] == item["source_ref"]
        )
        assert item["quote"] == source["content"][item["start"] : item["end"]]
        antecedent = item.get("antecedent")
        assert item["scope_refs"] == (
            [antecedent["ref"]] if antecedent else source["scope_refs"]
        )
    assert not [body for s, body in wire if s in {"mock-error", "extract"}]
    prompt, body = l1_prompt(wire, backend)
    info = result["evaluation_metadata"]["context_input"]
    assert info["l1_projection"] == projection
    assert info["l1_prompt_digest"] == digest(prompt)
    assert info["l1_invoked"] is True
    if backend == "llm":
        assert body["model"] == "openai/gpt-oss-20b"
    else:
        assert body["model"] == "jev-1.13.0"
        assert body["questions"]
    # Presentation must not change the adapter artifact or its digest.
    from intaris import policy_assessment
    from tools.l1_projection_sizes import specimen

    _, state, obligations = specimen("inbox-inspection-only")
    before = copy.deepcopy(artifact)
    for mode in ("json", "lines"):
        policy_assessment.request(
            state, obligations, "", task_context=artifact, projection=mode
        )
        assert artifact == before
    return artifact, prompt


@pytest.mark.parametrize("service", MATRIX, indirect=True)
def test_validated_current_indices_drive_both_projections(service, request):
    client, app, wire = service

    def answer(sources):
        source = next(s for s in sources if s["text"].startswith("Inspect logs."))
        return {
            "title": "Inspect tests",
            "intention": "Inspect tests without deploying.",
            "items": [
                {
                    "kind": "request",
                    "quote": "UNSUPPORTED_QUOTE",
                    "msg": source["msg"],
                    "status": "current",
                },
                {
                    "kind": "request",
                    "quote": "Inspect logs.",
                    "msg": source["msg"],
                    "status": "current",
                },
                {
                    "kind": "request",
                    "quote": "Inspect tests.",
                    "msg": source["msg"],
                    "status": "current",
                },
                {
                    "kind": "restriction",
                    "quote": "Do not deploy.",
                    "msg": source["msg"],
                    "status": "current",
                },
            ],
            # Raw index 0 is invalid; raw index 2 must become adapted index 1.
            "current": [0, 2],
        }

    app.state.offline_provider_hooks["intention_answer"] = answer
    sid = create(client)
    record(
        client,
        sid,
        {
            "type": "user_message",
            "data": {"content": "Inspect logs. Inspect tests. Do not deploy."},
        },
    )
    reason(client, sid)
    # This case verifies the completed profile's index mapping, not concurrent
    # regeneration. Await the actual barrier rather than racing its short HTTP
    # wait under parallel-suite CPU contention. Production race tests separately
    # require stale snapshots to remain blocked.
    client.portal.call(
        partial(
            app.state.intention_barrier.wait,
            "default",
            sid,
            timeout_override=30.0,
        )
    )
    captured_session = client.get(f"/api/v1/session/{sid}")
    assert captured_session.status_code == 200, captured_session.text
    assert captured_session.json()["intention_profile"]
    result = evaluate(client, sid)
    backend, _, _, _, projection = request.node.callspec.params["service"]
    artifact, prompt = check_contract(result, wire, backend, projection)
    assert artifact["status"] == "ready", {
        "artifact": artifact,
        "profile": captured_session.json()["intention_profile"],
    }
    assert artifact["current_request"] == {"status": "supported", "item_indices": [1]}
    assert [i["quote"] for i in artifact["items"]] == [
        "Inspect logs.",
        "Inspect tests.",
        "Do not deploy.",
    ]
    if projection == "lines":
        assert prompt.startswith("Action: write\n")
        assert "Inspect tests." in prompt
        lines = prompt.splitlines()
        assert any(
            "historical_context" in line and "Inspect logs." in line for line in lines
        )
        assert not any(
            line.startswith("Intention.items[") and "Inspect logs." in line
            for line in lines
        )
        assert "selection: not_current" in prompt
        assert prompt.count("Do not deploy.") == 1
        assert "RAW CAPTURED TASK SOURCES" not in prompt
        assert "CONVERSATION TAIL" not in prompt
        assert "UNSUPPORTED_QUOTE" not in prompt
    else:
        payload = rich_json(prompt)
        assert [
            i["text"] for i in payload["intention"]["items"] if i["kind"] == "request"
        ] == ["Inspect tests."]
        assert any(
            i["text"] == "Inspect logs." and i["selection"] == "not_current"
            for i in payload["intention"]["historical_context"]
        )


@pytest.mark.parametrize("service", MATRIX, indirect=True)
@pytest.mark.parametrize("profile", ["strict", "balanced"])
def test_unavailable_single_profile_uses_profile_specific_fallback(
    service, request, profile
):
    client, app, wire = service
    app.state.offline_provider_hooks["intention_answer"] = lambda sources: {
        "title": "Unavailable extraction",
        "intention": "Incomplete generated summary.",
        "items": [],
        "current": [],
    }
    sid = create(client, profile=profile)
    record(
        client,
        sid,
        {
            "type": "user_message",
            "data": {"content": "UNAVAILABLE_ORIGINAL_CANARY. Do not delete anything."},
        },
    )
    reason(client, sid)
    result = evaluate(client, sid)
    backend, _, _, _, projection = request.node.callspec.params["service"]
    if profile == "balanced":
        artifact = result["evaluation_metadata"]["task_context"]
        assert artifact["status"] == "raw"
        assert artifact["context_mode"] == "raw"
        prompt, _ = l1_prompt(wire, backend)
        assert "UNAVAILABLE_ORIGINAL_CANARY" in json.dumps(artifact["originals"])
        assert "UNAVAILABLE_ORIGINAL_CANARY" in prompt
        assert "Do not delete anything." in prompt
        assert (
            "Captured source context:" if projection == "lines" else '"source_context"'
        ) in prompt
        assert artifact["items"] == []
        # The captured session summary is retained only with its provenance in
        # raw source context, never substituted for an active generated task.
        from intaris.l1_projection import classifier_view
        from intaris.policy_evidence import Evidence

        projected = classifier_view(
            artifact,
            Evidence.model_validate(result["evaluation_metadata"]["policy_evidence"]),
        )
        assert projected["intention"]["summary"] != "Incomplete generated summary."
        assert projected["intention"]["items"] == []
        return
    artifact, prompt = check_contract(result, wire, backend, projection)
    assert artifact["status"] == "unavailable"
    assert artifact["items"] == []
    assert artifact["current_request"] == {"status": "unknown", "item_indices": []}
    # Raw evidence is now independent of successful compact extraction.
    # Unknown compact scope stays unknown; source origins still govern grants.
    assert "UNAVAILABLE_ORIGINAL_CANARY" in json.dumps(artifact["originals"])
    assert "UNAVAILABLE_ORIGINAL_CANARY" not in prompt
    assert "Incomplete generated summary." not in prompt
    assert "RAW CAPTURED TASK SOURCES" not in prompt
    if projection == "lines":
        assert prompt.startswith("Action: write\n")
        assert "Intention.status: unavailable" in prompt
        assert len(prompt.encode()) < 96000
    else:
        payload = rich_json(prompt)
        assert payload["intention"]["status"] == "unavailable"


@pytest.mark.parametrize("service", MATRIX, indirect=True)
def test_linked_single_confirmation_preserves_non_authorizing_antecedent(
    service, request
):
    client, app, wire = service
    sid = create(client)
    proposal = "May I send exactly draft X to Y?"
    record(
        client,
        sid,
        {"type": "assistant_message", "data": {"content": proposal}},
        {"type": "user_message", "data": {"content": "yes"}},
    )
    reason(client, sid)
    result = evaluate(client, sid)
    backend, _, _, _, projection = request.node.callspec.params["service"]
    artifact, prompt = check_contract(result, wire, backend, projection)
    assert artifact["status"] == "ready"
    assert artifact["current_request"] == {"status": "unknown", "item_indices": []}
    approval = next(i for i in artifact["items"] if i["kind"] == "approval")
    assert approval["quote"] == "yes"
    assert approval["antecedent"]["content"] == proposal
    assert approval["scope_refs"] == [approval["antecedent"]["ref"]]
    assert approval["antecedent"]["origin"] == "linked_non_authorizing_context"
    if projection == "lines":
        assert "Approved action:" not in prompt
        assert proposal not in prompt
        assert "RAW CAPTURED TASK SOURCES" not in prompt
    else:
        payload = rich_json(prompt)
        assert proposal not in json.dumps(payload)
