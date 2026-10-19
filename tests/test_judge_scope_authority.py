"""Judge adjudicates model-derived scope; explicit controller gates remain."""

import pytest

from intaris.hybrid import Rule, evaluate_policy
from intaris.policy_evidence import (
    CorrectedAssessment,
    Evidence,
    Observation,
    Review,
)


def evidence(**updates):
    state = Evidence(
        observations={},
        assessment={
            "relationship": "conflicts_with_intent",
            "contextual_restrictions": "conflicts",
            "restriction_reference": "none",
            "target_scope": "consistent",
            "target_reference": "action",
            "risk": "medium",
            "mutation": "mutating",
            "effects": ["write"],
        },
        review=Review(
            obligation_ids=["model.denial"],
            evidence_refs=["original-user"],
            user_would="approve",
            risk="medium",
            basis="routine_within_task",
        ),
    )
    return state.model_copy(update=updates)


def test_exact_live_conflict_discharged_without_inventing_factual_fields():
    state = evidence()
    result = evaluate_policy([], state)
    assert result.outcome == "approve"
    assert result.permission_basis == "independent_scope_review"
    assert not result.outstanding_obligations
    assert state.assessment["relationship"] == "conflicts_with_intent"
    assert "authorization" not in state.assessment


@pytest.mark.parametrize("effect", ["write", "transmit", "execute"])
def test_mutating_risk_abstention_requires_review_not_intent_alignment(effect):
    state = evidence(review=None)
    state.assessment.update(
        relationship="supports_intent",
        risk="unknown",
        contextual_restrictions="consistent",
        effects=[effect],
    )
    pending = evaluate_policy([], state)
    assert pending.outcome == "escalate"
    assert [o.id for o in pending.outstanding_obligations] == ["risk.unknown"]
    state.sources["original-user"] = {"independent_grant": True}
    reviewed = state.model_copy(
        update={
            "review": Review(
                obligation_ids=[o.id for o in pending.outstanding_obligations],
                evidence_refs=["original-user"],
                scope_support_refs=["original-user"],
                user_would="approve",
                risk="low",
                basis="routine_within_task",
            )
        }
    )
    assert evaluate_policy([], reviewed).outcome == "approve"


def test_known_routine_risk_and_configured_grant_do_not_require_extra_review():
    state = evidence(review=None)
    state.assessment.update(
        relationship="supports_intent",
        risk="low",
        contextual_restrictions="consistent",
    )
    assert evaluate_policy([], state).outcome == "approve"
    state.assessment["risk"] = "unknown"
    state = state.model_copy(
        update={
            "observations": {
                "configured_allow": Observation(
                    value=True, source="controller", coverage="complete"
                ),
                "effects": Observation(
                    value=["write"], source="controller", coverage="complete"
                ),
            }
        }
    )
    assert evaluate_policy([], state).outcome == "approve"


@pytest.mark.parametrize(
    "answer,expected", [("approve", "approve"), ("ask", "escalate"), ("refuse", "deny")]
)
def test_risk_correction_cannot_turn_review_deferral_or_refusal_into_approval(
    answer, expected
):
    state = Evidence(
        observations={},
        sources={"original-assignment": {"independent_grant": True}},
        assessment={
            "relationship": "supports_intent",
            "risk": "high",
            "mutation": "mutating",
            "effects": ["transmit"],
            "contextual_restrictions": "consistent",
            "target_scope": "consistent",
        },
        review=Review(
            obligation_ids=["risk.high", "scope.consequential_effect"],
            evidence_refs=["original-assignment"],
            scope_support_refs=["original-assignment"],
            user_would=answer,
            risk="medium",
            basis="routine_within_task" if answer == "approve" else "outside_task",
        ),
    )
    result = evaluate_policy([], state)
    assert result.outcome == expected
    if answer == "ask":
        assert any(
            o.id == "review.deferred" and o.requirement == "human"
            for o in result.outstanding_obligations
        )


def test_incoherent_correction_cannot_reassert_reviewed_model_conflict():
    state = evidence(
        sources={"original-user": {"kind": "instruction"}},
        review=Review(
            obligation_ids=["model.denial"],
            evidence_refs=["original-user"],
            user_would="approve",
            risk="medium",
            basis="routine_within_task",
            corrected_assessment=CorrectedAssessment(
                relationship="conflicts_with_intent",
                contextual_restrictions="conflicts",
                restriction_reference="original-user",
                target_scope="conflicts",
                target_reference="action",
            ),
        ),
    )
    assert evaluate_policy([], state).outcome == "approve"


@pytest.mark.parametrize("vote", ["ask", "refuse"])
def test_only_approval_discharges_model_scope(vote):
    state = evidence()
    review = state.review.model_copy(update={"user_would": vote})
    assert (
        evaluate_policy([], state.model_copy(update={"review": review})).outcome
        != "approve"
    )


def test_review_must_target_the_actual_obligation():
    state = evidence()
    review = state.review.model_copy(update={"obligation_ids": ["intent.unrelated"]})
    assert (
        evaluate_policy([], state.model_copy(update={"review": review})).outcome
        != "approve"
    )


def test_readonly_controller_requirement_still_denies_mutation():
    assert evaluate_policy([], evidence(read_only_required=True)).outcome == "deny"


def test_observed_conflict_is_not_overturned():
    state = evidence(
        observations={
            "relationship": Observation(
                value="conflicts_with_intent", source="controller", coverage="complete"
            ),
        }
    )
    assert evaluate_policy([], state).outcome != "approve"


def test_critical_judge_risk_still_denies():
    state = evidence()
    review = state.review.model_copy(update={"risk": "critical"})
    assert (
        evaluate_policy([], state.model_copy(update={"review": review})).outcome
        == "deny"
    )


def test_reviewed_risk_replaces_model_and_heuristic_labels_without_erasing_them():
    state = evidence()
    assessed = state.model_copy(
        update={"assessment": {**state.assessment, "risk": "critical"}}
    )
    assert evaluate_policy([], assessed).outcome == "approve"
    observed = assessed.model_copy(
        update={
            "observations": {"risk": Observation(value="critical", source="controller")}
        }
    )
    assert evaluate_policy([], observed).outcome == "approve"
    assert observed.observations["risk"].value == "critical"
    assert observed.assessment["risk"] == "critical"
    assert observed.review.risk != "critical"


def test_configured_denial_still_denies():
    state = evidence(
        observations={"tool": Observation(value="bash", source="controller")}
    )
    rules = [
        Rule.model_validate(
            {
                "id": "operator-deny",
                "effect": "deny",
                "when": {"op": "tool_matches", "value": "bash"},
            }
        )
    ]
    assert evaluate_policy(rules, state).outcome == "deny"


def test_legacy_bare_vote_is_not_promoted_to_scope_adjudication():
    state = evidence()
    review = state.review.model_copy(update={"basis": None})
    assert (
        evaluate_policy([], state.model_copy(update={"review": review})).outcome
        != "approve"
    )


def test_judge_scope_vote_cannot_overwrite_validated_human_refusal():
    state = evidence()
    observed = state.model_copy(
        update={
            "observations": {
                **state.observations,
                "contextual_restrictions": Observation(
                    value="conflicts", source="human_resolution", coverage="complete"
                ),
            }
        }
    )
    assert evaluate_policy([], observed).outcome != "approve"


def test_live_scoped_approval_needs_no_second_authorization_receipt():
    state = evidence()
    review = state.review.model_copy(update={"basis": "scoped_approval"})
    assert (
        evaluate_policy([], state.model_copy(update={"review": review})).outcome
        == "approve"
    )
    reviewed = state.model_copy(
        update={
            "review": review,
            "sources": {
                "original-user": {
                    "independent_grant": True,
                    "origin": "client_user_claim",
                }
            },
        }
    )
    assert evaluate_policy([], reviewed).outcome == "approve"
