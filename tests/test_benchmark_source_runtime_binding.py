"""Public-owner wiring, not runtime/model QA."""

import asyncio
import copy
import json

import pytest

from tools.benchmark.source_history_manifest import manifest_sha256
from tools.benchmark.source_history_runner import _freshness
from tools.benchmark.source_price import global_bound, request_bound
from tools.benchmark.source_runtime_binding import (
    ArtifactBinding,
    audit_resolution,
    await_recorded_generation,
    preserved_watermarks,
    provider_input_evidence,
)
from tools.benchmark.source_scope import PRIMARY, attempt_cap, expansion


def test_expansion_retains_preregistered_cap_and_exact_fixture_identity():
    scope = expansion(manifest_sha256())
    assert scope["original_attempt_cap"] == 4000
    assert attempt_cap(scope, PRIMARY, manifest_sha256()) == 6000
    assert scope["known_price_usd"] == 10 and scope["primary_evaluations"] == 1800
    assert not scope["model_execution_authorized"]
    wrong = copy.deepcopy(scope)
    wrong["authorized_attempt_cap"] = 7000
    with pytest.raises(ValueError):
        attempt_cap(wrong, PRIMARY)
    with pytest.raises(ValueError):
        expansion("b" * 64)


def test_observer_never_calls_capture_and_missing_stays_missing():
    class Owner:
        def capture(self, session):
            raise AssertionError("Observer must not generate")

    assert (
        ArtifactBinding(Owner(), "unit-tenant", "unit-session").observe("a" * 64)[
            "state"
        ]
        == "UNESTABLISHED"
    )

    class Missing(Owner):
        def observe_artifact(self, user_id, session_id, digest):
            return None

    assert (
        ArtifactBinding(Missing(), "unit-tenant", "unit-session").observe("a" * 64)[
            "state"
        ]
        == "MISSING"
    )


def test_observer_deepcopy_and_scope_validation():
    source = {
        "tenant": "unit-tenant",
        "session": "unit-session",
        "artifact_digest": "a" * 64,
        "items": [],
    }

    class Owner:
        def observe_artifact(self, *args):
            return source

    binding = ArtifactBinding(Owner(), "unit-tenant", "unit-session")
    observed = binding.observe("a" * 64)
    observed["artifact"]["items"].append("mutation in test")
    assert source["items"] == []
    with pytest.raises(ValueError):
        binding.observe("b" * 64)


def test_component_generation_label_not_existing_artifact_or_e2e():
    class Owner:
        def capture(self, session):
            return {"compact_intention": session["intention"]}

    result = ArtifactBinding(Owner(), "unit-tenant", "unit-session").generate_component(
        {
            "user_id": "unit-tenant",
            "session_id": "unit-session",
            "intention": "Recorded intention",
        },
    )
    assert (
        result["evidence_level"] == "component_generation" and not result["end_to_end"]
    )


@pytest.mark.parametrize("resolver", ["user", "judge", None])
def test_resolution_uses_actual_audit_resolver_not_user_decision(resolver):
    class Client:
        def get_audit_record(self, call_id):
            return {
                "session_id": "unit-session",
                "resolved_by": resolver,
                "user_decision": "approve",
            }

    result = audit_resolution(Client(), "unit-session", "unit-call")
    assert result["human_source_eligible"] is (resolver == "user")
    with pytest.raises(ValueError):
        audit_resolution(Client(), "another-session", "unit-call")


def test_barrier_ready_requires_provider_completion_revision_not_text_change():
    class Barrier:
        async def wait(self, *args, **kwargs):
            assert kwargs["timeout_override"] == 5.0
            return True

    state = {"latest_user_message_seq": 9, "intention": "Already changed"}
    completion = {
        "user_id": "unit-tenant",
        "session_id": "unit-session",
        "source_revision": 9,
        "completed": False,
    }

    def run():
        return asyncio.run(
            await_recorded_generation(
                barrier=Barrier(),
                user_id="unit-tenant",
                session_id="unit-session",
                read_session=lambda: state,
                provider_completion=lambda: completion,
            )
        )

    assert run()["state"] == "UNESTABLISHED"
    completion["completed"] = True
    assert run()["state"] == "UNESTABLISHED"
    completion["generation_accepted"] = True
    completion["applied_source_revision"] = 9
    assert run()["state"] == "ready"
    completion["source_revision"] = 8
    assert run()["state"] == "UNESTABLISHED"


def test_native_digest_watermarks_retained_without_int_coercion():
    artifact = {
        "watermarks": dict.fromkeys(
            ("messages", "decisions", "latest_user", "intention"), "a" * 64
        )
    }
    assert preserved_watermarks(artifact) == artifact["watermarks"]
    snapshot = {
        "source_digest": "a" * 64,
        "source_watermark": "b" * 64,
        "decision_watermark": "c" * 64,
        "coverage": {
            "source_history": "complete",
            "source_reference_context": "complete",
            "unavailable_reference_context": [],
        },
        "cache_state": "hit",
        "generation_status": "ready",
        "latest_user_text": "Read only.",
        "generated_intention": "Read only.",
    }
    assert _freshness(snapshot)["state"] == "CAPTURED"


def test_public_pricing_conservative_no_cache_discount_and_output_validation():
    assert global_bound() == pytest.approx(0.0589824)
    assert request_bound("openai/gpt-oss-120b", {"max_tokens": 16384}) == pytest.approx(
        0.0294912
    )
    assert request_bound("typesafe/jev-1.13-20260917", {}) == pytest.approx(0.001344)
    with pytest.raises(ValueError):
        request_bound("openai/gpt-oss-120b", {"max_tokens": 65537})
    with pytest.raises(ValueError):
        request_bound("openai/gpt-oss-120b", {"max_tokens": True})
    assert json.dumps(expansion(manifest_sha256())).find("6000") >= 0


def test_jev_serialized_state_is_inspected_for_hidden_original_bodies():
    artifact = {
        "originals": [
            {"ref": "event:7", "content": "Unique full history body not selected."}
        ],
        "items": [{"quote": "Selected short quote."}],
        "source_digest": "a" * 64,
        "artifact_digest": "b" * 64,
    }
    payload = {
        "messages": [{"content": "Selected short quote."}],
        "state": {"authorization_sources": ["Unique full history body not selected."]},
    }
    actual = provider_input_evidence(payload, artifact, stage="l1")
    assert actual["original_body_leaks"] == ["event:7"]
    assert not actual["compact_l1_verified"]
    assert "Unique full history" not in json.dumps(actual)
    assert (
        provider_input_evidence(payload, None, stage="l1")["compact_l1_verified"]
        is None
    )


def test_legitimate_compact_intention_field_is_not_original_history_leak():
    body = "Current compact intention equals captured session intention."
    artifact = {
        "originals": [{"ref": "session:intention", "content": body}],
        "items": [],
        "compact_intention": body,
        "artifact_digest": "a" * 64,
    }
    prompt = (
        "RICH INTENTION (bounded interpretation):\n"
        + json.dumps(
            {
                "compact_intention": body,
                "artifact_digest": "a" * 64,
                "status": "unavailable",
            }
        )
        + "\nSOURCE REFERENCE METADATA (not grants):\n{}"
    )
    payload = {"messages": [{"role": "user", "content": prompt}]}
    assert provider_input_evidence(payload, artifact, stage="l1")["compact_l1_verified"]
    encoded = {"state": json.dumps(payload)}
    assert provider_input_evidence(encoded, artifact, stage="l1")["compact_l1_verified"]
    payload["state"] = {"authorization_sources": [body]}
    assert not provider_input_evidence(payload, artifact, stage="l1")[
        "compact_l1_verified"
    ]


def test_regenerated_expansion_rejects_history_fixture_drift(monkeypatch):
    import tools.benchmark.source_history_manifest as module

    old = expansion(manifest_sha256())
    monkeypatch.setattr(module, "manifest_sha256", lambda: "b" * 64)
    with pytest.raises(ValueError):
        expansion("b" * 64)
    with pytest.raises(ValueError):
        attempt_cap(old, PRIMARY)
