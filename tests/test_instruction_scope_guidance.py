"""Prompt-boundary tests, not claims of live-model classification accuracy."""

import json

import pytest

from intaris import judge_simulator, l1_projection
from intaris.hybrid import evaluate_policy
from intaris.policy_evidence import from_observations
from intaris.task_support import INSTRUCTION_SCOPE_GUIDANCE


@pytest.mark.parametrize(
    "instruction",
    [
        "Kill that background browser preview.",
        "Do not start previews again.",
        "Keep this service stopped until I approve.",
        "Stop that preview because its port conflicts with the test runner.",
    ],
)
def test_specific_stop_and_ongoing_constraints_reach_both_models_unchanged(instruction):
    latest = "Implement and validate the next UI changes."
    artifact = {
        "context_mode": "raw",
        "status": "unavailable",
        "items": [],
        "originals": [
            {
                "ref": "stop-instruction",
                "origin": "user_message",
                "content": instruction,
                "truncated": False,
            },
            {
                "ref": "later-task",
                "origin": "user_message",
                "content": latest,
                "truncated": False,
            },
        ],
    }
    state = from_observations(
        {
            "tool": "bash",
            "args": {"command": "npm run dev -- --host 127.0.0.1 --port 18192"},
        }
    )
    question = judge_simulator.request(
        {"tool": "bash", "args_redacted": state.value("args")},
        {"intention": latest, "policy": {"profile": "balanced"}},
        state,
        evaluate_policy([], state),
        artifact,
    )
    judge_payload = json.loads(question["messages"][1]["content"])
    l1_payload = l1_projection.render(artifact, state, {})
    assert instruction in l1_payload
    assert latest in l1_payload
    assert instruction in str(judge_payload)
    assert latest in str(judge_payload)
    assert INSTRUCTION_SCOPE_GUIDANCE in l1_projection.SYSTEM
    assert INSTRUCTION_SCOPE_GUIDANCE in question["messages"][0]["content"]


def test_shared_guidance_preserves_refusals_conditions_and_no_automatic_revocation():
    assert "unsatisfied stop instruction" in INSTRUCTION_SCOPE_GUIDANCE
    assert "does not automatically revoke" in INSTRUCTION_SCOPE_GUIDANCE
    assert "real human refusal remains binding" in INSTRUCTION_SCOPE_GUIDANCE
    assert "agent\nclaims" in INSTRUCTION_SCOPE_GUIDANCE
    assert "action-intention-view-v9" == l1_projection.VERSION
