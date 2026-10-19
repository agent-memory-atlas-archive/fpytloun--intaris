"""Transport alone is not evidence of an external or irreversible mutation."""

import pytest

from intaris.effect_scope import requires_explicit_grant
from intaris.hybrid import evaluate_policy
from intaris.policy_evidence import Evidence, Observation, from_observations
from tests.test_intent_aligned import state


def test_captured_git_fetch_evidence_without_review_or_grant():
    # Synthetic policy-relevant projection of a captured fetch operation.
    s = Evidence(
        observations={
            "effects": Observation(
                value=["unknown", "write"],
                source="tool_contract",
                coverage="partial",
                references=("action",),
            ),
            "mutation": Observation(
                value="mutating",
                source="tool_contract",
                coverage="complete",
                references=("action",),
            ),
            "external_or_irreversible": Observation(
                value=None,
                source="static_observation",
                coverage="unknown",
                references=("action",),
            ),
        },
        assessment={
            "risk": "low",
            "effects": ["read", "execute", "transmit", "write"],
            "relationship": "supports_intent",
            "target_scope": "consistent",
            "authorization": "implicit",
            "target_reference": "action",
            "authorization_scope": "unclear",
            "authorization_source": "none",
            "restriction_reference": "none",
            "contextual_restrictions": "consistent",
        },
    )
    for profile in ("balanced", "bounded_autonomy"):
        result = evaluate_policy([], s, profile, assessed=True)
        assert result.outcome == "approve"
        assert not result.outstanding_obligations
    assert evaluate_policy([], s, "strict", assessed=True).outcome == "escalate"
    assert s.review is None and not s.human_approved
    assert not s.known("external_or_irreversible")


@pytest.mark.parametrize("tool", ["bash", "network.exchange", "mail.send"])
@pytest.mark.parametrize("mutation", ["read_only", "mutating", "unknown"])
@pytest.mark.parametrize("external", [None, False])
def test_low_risk_transport_needs_no_scope_review_in_permissive_modes(
    tool, mutation, external
):
    s = state(
        effects=["read", "write", "transmit"],
        mutation=mutation,
        external_or_irreversible=external,
    )
    s = from_observations({"tool": tool}).model_copy(
        update={"assessment": s.assessment}
    )
    assert s.value("tool") == tool
    assert not requires_explicit_grant(s)
    for profile in ("balanced", "bounded_autonomy"):
        initial = evaluate_policy([], s, profile, assessed=True)
        assert initial.outcome == "approve"
        assert not initial.outstanding_obligations
    assert evaluate_policy([], s, "strict", assessed=True).outcome == "escalate"
    assert s.value("authorization") == "absent"


@pytest.mark.parametrize(
    "boundary",
    [
        {"external_or_irreversible": True},
        {"effects": ["transmit", "delete"]},
        {"effects": ["transmit", "disclose"]},
        {"reversibility": "irreversible"},
        {"environment": "production", "mutation": "mutating"},
    ],
)
def test_low_risk_effect_labels_do_not_impose_strict_scope_review(boundary):
    s = state(**({"effects": ["transmit"]} | boundary))
    assert requires_explicit_grant(s)
    for profile in ("balanced", "bounded_autonomy"):
        initial = evaluate_policy([], s, profile, assessed=True)
        # Effect labels alone do not override the low-risk assessment in
        # permissive modes. Concrete conflicts/hard observations are tested
        # separately; requiring scope proof here would recreate strict policy.
        assert initial.outcome == "approve"
        assert not initial.outstanding_obligations
