"""Fixture/adapter wiring checks only; no canonical-v2 or model quality claim."""

from __future__ import annotations

import copy
from argparse import Namespace

import httpx
import pytest

from intaris.hybrid import validate_rules
from tools.benchmark.matched import run
from tools.benchmark.provider_recorder import ProviderRecorder
from tools.benchmark.v2_comparison import (
    BASELINE_SHA256,
    assert_compiler_free,
    assess_contrast,
    freeze,
    run_table,
    verify,
)


def manifest(tmp_path):
    boundary = tmp_path / "boundary.md"
    boundary.write_text("Reviewed v2 comparison boundary, fixture test")
    return freeze(boundary)


def test_frozen_manifest_keeps_lanes_and_baseline_identity(tmp_path):
    value = manifest(tmp_path)
    verify(value)
    assert value["baseline_sha256"] == BASELINE_SHA256
    assert len(value["table_cases"]) == 78
    assert len(value["pairs"]) == 21
    assert not value["live_ready"]
    assert value["compiler_calls_expected"] == 0
    assert not {"scenarios", "gold_labels"} & value.keys()
    altered = copy.deepcopy(value)
    altered["table_cases"][0]["expected"]["raw"] = ["deny"]
    with pytest.raises(ValueError, match="changed after freeze"):
        verify(altered)


def test_high_risk_declared_grant_preserves_risk_all_profiles(tmp_path):
    value = manifest(tmp_path)
    cases = [
        c
        for c in value["table_cases"]
        if c["property"] == "high_risk_complete_declared_grant"
    ]
    assert len(cases) == 3
    for case in cases:
        assert case["input"]["assessments"]["risk"] == "high"
        assert case["expected"]["raw"] == ["approve"]
        assert case["input"]["observations"]["targets"]["coverage"] == "complete"
        assert case["input"]["declarations"][0]["whole_call"]


def test_semantic_review_is_not_declared_permission_or_strict_override(tmp_path):
    value = manifest(tmp_path)
    for case in value["table_cases"]:
        if case["property"] == "high_risk_semantic_before_judge":
            assert "approve" not in case["expected"]["final"]
            assert case["input"]["review"] is None
            assert not case["input"]["declarations"]
        if case["property"] == "high_risk_semantic_after_bound_review":
            assert case["input"]["assessments"]["risk"] == "high"
            assert case["input"]["review"]["source"] == "engine_record"
            assert case["input"]["review"]["action_bound"]
            if case["profile"] == "strict":
                assert "approve" not in case["expected"]["final"]
            else:
                assert case["expected"]["final"] == ["approve"]


def test_bounds_waiver_and_protected_floor_are_not_satisfaction(tmp_path):
    cases = manifest(tmp_path)["table_cases"]
    for case in cases:
        if case["property"] == "ordinary_scope_bound_waiver":
            assert case["expected"]["raw"] == ["deny"]
            assert case["expected"]["final"] == ["approve"]
            assert (
                case["expected"]["permission_disposition"] == "waived_by_operator_bound"
            )
        if case["property"] == "plan_mutation_maximum_approve":
            assert case["input"]["bounds"]["maximum"] == "approve"
            assert "approve" not in case["expected"]["final"]
        if case["property"] == "independent_review_bound_waiver":
            assert (
                case["expected"]["permission_disposition"] == "waived_by_operator_bound"
            )
            requirement = (
                "declared_or_human_authority"
                if case["profile"] == "strict"
                else "independent_review"
            )
            assert case["expected"]["obligation_kinds"] == [requirement]


def test_read_effects_not_capability_and_risk_not_review_heuristic(tmp_path):
    for case in manifest(tmp_path)["table_cases"]:
        if case["property"] == "plan_read_via_capable_tool":
            assert case["input"]["observations"]["effects"]["values"] == ["read"]
            assert "execute" in case["input"]["tool_capabilities"]
        if case["property"] == "consequential_low_risk_label_before_review":
            assert case["input"]["assessments"]["risk"] == "low"
            assert "approve" not in case["expected"]["final"]
        if case["property"] == "high_impact_scoped_human_authority":
            assert case["input"]["review"]["action_bound"]
            assert case["input"]["review"]["source"] == "human_record"
            assert case["expected"]["final"] == ["approve"]


def test_pairs_only_change_ready_policy_not_action_or_intent(tmp_path):
    value = manifest(tmp_path)
    for pair in value["pairs"]:
        assert len(pair["variants"]) == 2
        assert set(pair["variants"][0]) == {"id", "policy"}
        for variant in pair["variants"]:
            policy = variant["policy"]
            assert not {"allow_policies", "deny_policies"} & policy.keys()
            validate_rules(policy["rules"])
            assert policy["profile"] == pair["profile"]
        assert pair["compiler_calls_expected"] == 0
        assert pair["needs_canonical_wire_review"]
    assert any(p["pair"] == "mixed_targets" for p in value["pairs"])
    assert any(p["pair"] == "wrong_checkout" for p in value["pairs"])


def test_missing_canonical_binding_fails_without_legacy_engine_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="not ready"):
        run_table(manifest(tmp_path), None)


def test_callback_wiring_is_explicitly_not_canonical_policy_evidence(tmp_path):
    value = manifest(tmp_path)
    seen = []

    def scripted(case):
        seen.append(case["id"])
        case["input"]["assessments"]["risk"] = "low"
        return {
            "raw": "deny",
            "final": "deny",
            "obligation_kinds": [],
            "permission_disposition": "blocked",
        }

    result = run_table(value, scripted)
    assert len(seen) == len(value["table_cases"])
    assert not result["canonical_binding_confirmed"]
    assert result["binding_module"] == __name__
    assert result["binding_qualified_name"].endswith("scripted")
    assert result["manifest_sha256"] == value["sha256"]
    assert result["boundary_sha256"] == value["boundary_sha256"]
    assert len(result["binding_source_sha256"]) == 64
    # Callback cannot edit the frozen oracle/input; no assertion of engine success.
    verify(value)
    assert any(
        c["input"]["assessments"]["risk"] == "high" for c in value["table_cases"]
    )


def test_unobserved_live_premise_not_scored_as_semantic_failure(tmp_path):
    pair = next(
        p
        for p in manifest(tmp_path)["pairs"]
        if p["pair"] == "high_impact_typed_permission"
    )
    row = {
        "intaris": {"evaluation_metadata": {"facts": {"risk": "low"}}},
        "terminal": {"state": "terminal", "decision": "deny", "audit": {}},
    }
    result = assess_contrast(row, pair, "declared")
    assert not result["conformant"]  # behavior remains a real failed approval
    assert result["conditional_coverage"] == "unestablished"
    assert result["semantic_verdict"] == "not_established"
    assert pair["classification_premise"]["risk"] == ["high"]


def test_extra_outstanding_obligation_cannot_pass_empty_reference(tmp_path):
    value = manifest(tmp_path)

    def scripted(case):
        expected = case["expected"]
        return {
            "raw": expected["raw"][0],
            "final": expected["final"][0],
            "obligation_kinds": [
                *expected["obligation_kinds"],
                "unrelated_alternative",
            ],
            "permission_disposition": expected["permission_disposition"],
        }

    result = run_table(value, scripted)
    assert all(not row["checks"]["obligations"] for row in result["results"])


def test_compiler_guard_counts_rejected_attempt_not_just_wire_requests():
    def must_not_call(_request):
        raise AssertionError("Compiler request reached upstream")

    with ProviderRecorder("unit-local-key", forbid_compiler=True) as relay:
        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(must_not_call))
        with httpx.Client() as client:
            response = client.post(
                relay.url + "/compiler/chat/completions",
                headers={"Authorization": f"Bearer {relay.token}"},
                json={"model": "openai/gpt-oss-120b"},
            )
        assert response.status_code == 409
        assert relay.records == []
        assert relay.validation_errors[0]["stage"] == "compiler"
        with pytest.raises(RuntimeError, match="Compiler attempt"):
            assert_compiler_free(relay.records, relay.validation_errors)


def test_unchanged_lane_rejects_other_cohorts_before_credentials(tmp_path):
    from tools.benchmark.matched import freeze as old_freeze
    from tools.benchmark.matched import write_json

    old = old_freeze()
    path = tmp_path / "other-cohort.json"
    write_json(path, old)
    with pytest.raises(ValueError, match="exact frozen47"):
        run(Namespace(mode="legacy", manifest=path, comparison_lane="unchanged_v2"))
