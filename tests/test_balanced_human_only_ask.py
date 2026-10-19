"""Permissive admission replaces the removed second-checker receipt contract."""

import pytest

from intaris.hybrid import Rule, evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.judge_simulator import Answer, receipt
from intaris.policy_evidence import Evidence, Observation


def pending(**updates):
    return Evidence(
        observations={
            "tool": Observation(value="write", source="controller", coverage="complete")
        },
        sources={"user-task": {"constraint_kinds": ["restriction"]}},
        assessment={
            "relationship": "unclear",
            "contextual_restrictions": "unresolved",
            "restriction_reference": "user-task",
            "target_scope": "unresolved",
            "target_reference": "action",
            "risk": "low",
            "mutation": "mutating",
            "effects": ["write", "unknown"],
        }
        | updates,
    )


@pytest.mark.parametrize("profile", ["balanced", "bounded_autonomy"])
@pytest.mark.parametrize("relationship", ["supports_intent", "unrelated", "unclear"])
def test_low_risk_needs_no_permission_proof_or_review(profile, relationship):
    result = evaluate_policy([], pending(relationship=relationship), profile)
    assert result.outcome == "approve"
    assert not result.outstanding_obligations


def test_medium_task_support_including_process_cleanup_is_not_a_checkpoint():
    result = evaluate_policy([], pending(risk="medium"), "balanced")
    assert result.outcome == "approve"


def test_strict_still_reviews_uncertain_scope():
    assert evaluate_policy([], pending(), "strict").outcome == "escalate"


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "updates",
    [
        {"risk": "critical"},
        {"relationship": "conflicts_with_intent"},
        {"contextual_restrictions": "conflicts"},
        {"target_scope": "conflicts"},
    ],
)
def test_actual_conflicts_and_critical_risk_still_require_resolution(profile, updates):
    assert evaluate_policy([], pending(**updates), profile).outcome != "approve"


def test_explicit_controls_and_human_refusals_still_win():
    state = pending()
    rule = Rule.model_validate(
        {
            "id": "human-release",
            "effect": "require_approval",
            "when": {"op": "tool_matches", "value": "write"},
        }
    )
    assert evaluate_policy([rule], state).outcome == "escalate"
    assert (
        evaluate_policy(
            [], state.model_copy(update={"read_only_required": True})
        ).outcome
        == "deny"
    )
    result = evaluate_policy([], state)
    assert finalize_result(result, maximum=None, minimum="deny").decision == "deny"
    assert (
        finalize_result(
            result, maximum=None, minimum=None, human_resolution="deny"
        ).decision
        == "deny"
    )


def test_actual_judge_deferral_is_not_silently_turned_into_approval():
    state = pending(risk="high")
    initial = evaluate_policy([], state)
    answer = Answer(
        user_would="ask",
        risk="high",
        confidence="high",
        basis="missing_permission",
        cites=[],
        question="Which recipient should receive the private report?",
        action_summary="Send the report.",
        explanation="The destination is unspecified.",
    )
    state = state.model_copy(update={"review": receipt(state, initial, answer)})
    assert evaluate_policy([], state).outcome == "escalate"
