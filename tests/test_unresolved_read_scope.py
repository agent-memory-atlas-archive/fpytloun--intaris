"""Model-inferred read-only behavior is not verified scope evidence."""

import pytest

from intaris.hybrid import evaluate_policy
from intaris.policy_evidence import from_observations


def inferred_read(**updates):
    # Anonymized projection of a credential-plaintext-print regression.
    # The token-printing command is fixture data only, never executed.
    state = from_observations(
        {
            "tool": "bash",
            "command": "python -c \"import os; print(os.environ['INTERNAL_API_TOKEN'])\"",
            "static_read": False,
            "mutation": "unclear",
        }
    )
    return state.model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "effects": ["read"],
                "mutation": "unclear",
                "risk": "low",
                "contextual_restrictions": "unresolved",
                "target_scope": "unresolved",
                **updates,
            }
        }
    )


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize("unresolved", ["target_scope", "contextual_restrictions"])
@pytest.mark.parametrize("coverage", ["unknown", "complete"])
def test_uncertain_model_read_uses_the_selected_profile(profile, unresolved, coverage):
    state = inferred_read(
        **{
            "target_scope": "consistent",
            "contextual_restrictions": "consistent",
            unresolved: "unresolved",
        }
    )
    state.observations["mutation"] = state.observations["mutation"].model_copy(
        update={"coverage": coverage}
    )
    assert state.value("mutation") == "read_only"
    result = evaluate_policy([], state, profile, assessed=True)
    if profile != "strict":
        # A missed model classification is retained as a known false-negative
        # fixture, not a reason to make all low-risk uncertain reads strict.
        assert result.outcome == "approve"
        return
    assert result.outcome == "escalate"
    obligation = next(
        item for item in result.outstanding_obligations if item.id == "intent.scope"
    )
    assert obligation.requirement == "independent_review"


def test_consistent_model_read_does_not_require_unconditional_review():
    state = inferred_read(
        contextual_restrictions="consistent", target_scope="consistent"
    )
    assert evaluate_policy([], state, "balanced", assessed=True).outcome == "approve"


def test_factually_bounded_read_preserves_existing_admission():
    state = from_observations(
        {
            "tool": "read",
            "mutation": "read_only",
            "effects": ["read"],
            "static_read": True,
        }
    ).model_copy(update={"assessment": inferred_read().assessment})
    assert evaluate_policy([], state, "balanced", assessed=True).outcome == "approve"
