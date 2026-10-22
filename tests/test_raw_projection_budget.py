"""Raw L1 evidence must remain visible to complete-request capacity fitting."""

import json

import pytest

from intaris import policy_assessment
from intaris.hybrid import evaluate_policy
from intaris.input_budget import (
    Capacity,
    ContextInsufficientError,
    fit,
    source_sections,
)
from intaris.policy_evidence import from_observations


def question(projection, mode="raw"):
    artifact = {
        "status": "unavailable",
        "context_mode": mode,
        "items": [],
        "originals": [
            {
                "ref": "latest_user_message",
                "origin": "client_user_claim",
                "content": "Inspect the web page. Do not upload any private files.",
                "chronology": "2026-10-18T10:00:00Z",
            },
            {
                "ref": "session_intention",
                "origin": "generated_summary",
                "content": "Old generated context: " + "observation " * 12000,
            },
        ],
    }
    state = from_observations(
        {
            "tool": "web_fetch",
            "arguments": {"url": "https://docs.python.org/3/"},
        }
    )
    result = evaluate_policy([], state)
    return policy_assessment.request(
        state, result, "", task_context=artifact, projection=projection
    )


@pytest.mark.parametrize("projection", ["lines", "json"])
@pytest.mark.parametrize("mode", ["raw", "single", "rich"])
@pytest.mark.parametrize("capacity", [Capacity(), Capacity(max_input_tokens=32000)])
def test_raw_sources_are_fitted_without_losing_action_or_restrictions(
    projection, mode, capacity
):
    q = question(projection, mode)
    calls = []

    class LLM:
        def generate(self, messages, **kwargs):
            payload = {"messages": messages, **kwargs}
            fitted, diagnostics = fit(payload, capacity)
            calls.append((fitted, diagnostics))
            # No real inference: exercise registration at the owning boundary.
            return json.dumps(
                {
                    key: []
                    if value.get("type") == "array"
                    else value.get("enum", ["unknown"])[0]
                    for key, value in q["schema"]["properties"].items()
                }
            )

    policy_assessment.classify(LLM(), None, q)
    assert calls
    fitted, diagnostics = calls[0]
    text = fitted["messages"][1]["content"]
    assert "https://docs.python.org/3/" in text
    assert "Do not upload any private files." in text
    assert "omitted_model_budget" in text or "bounded_excerpt" in text
    assert diagnostics["trimmed"]
    assert diagnostics["omitted_sources"]


def test_unregistered_payload_cannot_authorize_truncating_user_evidence():
    from intaris.input_budget import located_source

    q = question("lines")
    # Turn the large generated body into actual user evidence. Same size, but
    # no capacity fix is allowed to turn restrictions into an incomplete grant.
    sources = q["raw_sources"]
    sources["sources"][0]["origin"] = "client_user_claim"
    sources["sources"][1]["origin"] = "client_user_claim"
    prompt = json.dumps(sources, ensure_ascii=False)
    payload = {"state": {"prompt": prompt}}
    with source_sections(located_source(prompt, sources, path=("state", "prompt"))):
        with pytest.raises(ContextInsufficientError):
            fit(payload, Capacity(max_input_tokens=8000))


@pytest.mark.parametrize("projection", ["lines", "json"])
@pytest.mark.parametrize("mode", ["raw", "single", "rich"])
def test_actual_jev_wire_is_fitted_before_transport(projection, mode):
    from tests.test_jev import _client

    client, requests = _client({"answers": {}})
    try:
        # The transport intentionally returns no classification. Verify the
        # real serialized request, independently of response parsing.
        with pytest.raises(ValueError, match="missing answer"):
            policy_assessment.classify(None, client, question(projection, mode))
    finally:
        client.close()
    assert len(requests) == 1
    payload = requests[0].content.decode()
    assert "https://docs.python.org/3/" in payload
    assert "Do not upload any private files." in payload
    assert "omitted_model_budget" in payload or "bounded_excerpt" in payload
    assert len(requests[0].content) <= 98304


@pytest.mark.parametrize("projection", ["lines", "json"])
@pytest.mark.parametrize("capacity", [Capacity(), Capacity(max_input_tokens=32000)])
def test_complete_human_evidence_fits_without_repeating_derived_index(
    projection, capacity, monkeypatch
):
    from copy import deepcopy

    from intaris import l1_projection
    from intaris.input_budget import measure
    from intaris.l1_projection import classifier_view

    body = "Inspect repository; do not push. " * 1400
    artifact = {
        "context_mode": "raw",
        "status": "unavailable",
        "compact_intention": body[:16000],
        "items": [
            {
                "kind": "restriction",
                "status": "current",
                "quote": body[:700],
                "source_ref": "original_assignment",
            }
            for _ in range(80)
        ],
        "originals": [
            {
                "ref": "original_assignment",
                "origin": "client_assignment",
                "content": body,
            }
        ],
    }
    state = from_observations(
        {"tool": "read", "arguments": {"file_path": "/repo/a.py"}}
    )
    view = classifier_view(artifact, state)
    assert "summary" not in view["intention"]
    assert "items" not in view["intention"]
    assert view["intention"]["source_context"]["sources"][0]["content"] == body
    q = policy_assessment.request(
        state,
        evaluate_policy([], state),
        "",
        task_context=artifact,
        projection=projection,
    )
    wire = {"state": {"prompt": q["prompt"]}, "schema": q["schema"]}
    assert not measure(wire, capacity, jev=True)["overflow"]
    assert "/repo/a.py" in q["prompt"]
    duplicated = deepcopy(view)
    duplicated["intention"].update(
        summary=artifact["compact_intention"],
        items=artifact["items"],
        historical_context=[],
    )
    with monkeypatch.context() as patch:
        patch.setattr(l1_projection, "classifier_view", lambda *_: duplicated)
        before = policy_assessment.request(
            state,
            evaluate_policy([], state),
            "",
            task_context=artifact,
            projection=projection,
        )
    assert measure(
        {"state": {"prompt": before["prompt"]}, "schema": before["schema"]},
        capacity,
        jev=True,
    )["overflow"]


@pytest.mark.parametrize("missing,truncated", [(True, False), (False, True)])
def test_incomplete_originals_keep_summary_and_derived_fallback(missing, truncated):
    from intaris.l1_projection import classifier_view

    artifact = {
        "context_mode": "raw",
        "compact_intention": "Prior task boundary",
        "coverage": {"missing": ["original_assignment_unavailable"] if missing else []},
        "originals": [
            {
                "ref": "latest_user_message",
                "origin": "client_user_claim",
                "content": "Continue",
                "truncated": truncated,
            }
        ],
    }
    view = classifier_view(artifact, from_observations({}))
    assert view["intention"]["summary"] == "Prior task boundary"
    assert "items" in view["intention"]
