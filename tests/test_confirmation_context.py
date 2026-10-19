"""No model is mocked into granting a keyword: verify prompts and cite boundaries."""

import pytest

from intaris.confirmation_context import CONFIRMATION_INSTRUCTIONS
from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import request
from intaris.policy_assessment import system_prompt
from intaris.policy_evidence import from_observations


@pytest.mark.parametrize("confirmation", ["Odešli", "yes, send it", "ok"])
def test_confirmation_survives_judge_citation_and_exact_scope_guidance(confirmation):
    artifact = {
        "originals": [
            {
                "ref": "message:request",
                "origin": "client_user_claim",
                "content": "Draft the email to the supplier. Let me review before sending.",
                "truncated": False,
            },
            {
                "ref": "latest_user_message",
                "origin": "client_user_claim",
                "content": confirmation,
                "truncated": False,
            },
        ],
        "items": [],
        "status": "ready",
    }
    state = from_observations(
        {"tool": "send_email", "mutation": "mutating", "effects": ["transmit"]}
    )
    question = request(
        {"tool": "send_email", "args_redacted": {"to": "supplier"}},
        {"intention": "Send reviewed supplier email"},
        state,
        evaluate_policy([], state),
        artifact,
    )
    assert CONFIRMATION_INSTRUCTIONS in question["messages"][0]["content"]
    assert any(
        source["text"] == confirmation for source in question["citation_sources"]
    )
    assert "changed, contradicted or revoked" in question["messages"][0]["content"]
    assert (
        "recipient, modified content, extra effects"
        in question["messages"][0]["content"]
    )
    # Mere prompt text is not itself a grant or a keyword bypass.
    assert evaluate_policy([], state).permission_basis != "read_only_static"


@pytest.mark.parametrize("projection", ["json", "lines"])
def test_independent_assessment_uses_confirmation_contract(projection):
    prompt = system_prompt(
        {
            "projection": projection,
            "schema": {},
            "has_operator_policy": False,
            "judge": True,
        }
    )
    assert CONFIRMATION_INSTRUCTIONS in prompt
    assert "Never derive the referent solely from the current tool call" in prompt


@pytest.mark.parametrize("confirmation", ["Odešli", "yes, send it", "ok"])
@pytest.mark.parametrize(
    "context", ["missing proposal", "changed recipient", "revoked confirmation"]
)
def test_confirmation_keywords_do_not_install_deterministic_authority(
    confirmation, context
):
    state = from_observations(
        {
            "tool": "send_email",
            "mutation": "mutating",
            "effects": ["transmit"],
            "description": confirmation,
            "context": context,
        }
    )
    assert evaluate_policy([], state).outcome != "approve"


def test_confirmation_cannot_override_operator_denial_or_read_only_ceiling():
    from intaris.read_only import enforce

    state = from_observations(
        {
            "tool": "send_email",
            "mutation": "mutating",
            "effects": ["transmit"],
            "configured_deny": True,
            "description": "yes, send it",
        }
    )
    assert evaluate_policy([], state).outcome == "deny"
    assert enforce("approve", {"read_only_required": True}, "mutating")[0] == "deny"
