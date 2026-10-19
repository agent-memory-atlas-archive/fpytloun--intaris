import pytest

from intaris.hybrid import evaluate_policy
from intaris.policy_evidence import Evidence, Review


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
@pytest.mark.parametrize(
    "relationship", ["directly_serves_intent", "supports_intent", "unrelated"]
)
@pytest.mark.parametrize("authorization", ["absent", "explicit"])
def test_transmit_uses_risk_and_profile_not_mandatory_permission_proof(
    risk, relationship, authorization
):
    state = Evidence(
        observations={},
        assessment={
            "relationship": relationship,
            "risk": risk,
            "effects": ["transmit"],
            "mutation": "mutating",
            "target_scope": "consistent",
            "target_reference": "action",
            "contextual_restrictions": "consistent",
            "authorization": authorization,
            "authorization_source": "user",
            "authorization_scope": "within_scope",
        },
        sources={"user": {"origin": "recorded_user_event", "independent_grant": True}},
    )
    initial = evaluate_policy([], state)
    requires_review = authorization != "explicit" and (
        risk == "high" or (relationship == "unrelated" and risk == "medium")
    )
    assert initial.outcome == ("escalate" if requires_review else "approve")
    assert "scope.consequential_effect" not in [
        o.id for o in initial.outstanding_obligations
    ]
    if not requires_review:
        return
    reviewed = state.model_copy(
        update={
            "review": Review(
                obligation_ids=[o.id for o in initial.outstanding_obligations],
                evidence_refs=["user"],
                scope_support_refs=["user"],
                user_would="approve",
                risk="low",
                basis="scoped_approval",
            )
        }
    )
    assert evaluate_policy([], reviewed).outcome == "approve"
    unsupported = reviewed.model_copy(
        update={"review": reviewed.review.model_copy(update={"evidence_refs": []})}
    )
    assert evaluate_policy([], unsupported).outcome == "approve"


@pytest.mark.parametrize("relationship", ["unrelated", "unclear"])
def test_balanced_low_risk_uncertain_mutation_is_permitted(relationship):
    state = Evidence(
        observations={},
        assessment={
            "relationship": relationship,
            "risk": "low",
            "effects": ["write"],
            "mutation": "mutating",
            "target_scope": "consistent",
            "contextual_restrictions": "consistent",
        },
    )
    result = evaluate_policy([], state)
    assert result.outcome == "approve"
    assert not result.outstanding_obligations


def test_routine_local_work_keeps_fast_path():
    state = Evidence(
        observations={},
        assessment={
            "relationship": "supports_intent",
            "risk": "low",
            "effects": ["write"],
            "mutation": "mutating",
            "target_scope": "consistent",
            "contextual_restrictions": "consistent",
        },
    )
    assert evaluate_policy([], state).outcome == "approve"
