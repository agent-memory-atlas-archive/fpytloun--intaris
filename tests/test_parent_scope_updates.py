"""Legacy evaluation receives original parent scope, not only a stale summary."""

import json

from intaris.evaluation_context import parent_scope_evidence


def test_parent_update_is_delivered_without_erasing_independent_restrictions():
    parent = {
        "intention": "Prepare wiring diagram",
        "original_assignment": "Prepare wiring diagram. Do not purchase anything.",
        "latest_user_evidence": {
            "content": "Now compare supplier offers for six workshop lamps.",
            "timestamp": "2024-01-15T10:00:00Z",
            "origin": "user_message",
        },
    }
    actual = json.loads(parent_scope_evidence(parent))
    assert actual["generated_intention"] == parent["intention"]
    assert actual["original_assignment"] == parent["original_assignment"]
    assert actual["latest_user_evidence"] == parent["latest_user_evidence"]
    assert "not independent human authorization" in actual["interpretation"]
    assert "independent restrictions remain binding" in actual["interpretation"]


def test_legacy_parent_without_captured_sources_retains_summary():
    assert (
        parent_scope_evidence({"intention": "Existing legacy task"})
        == "Existing legacy task"
    )


def test_conditional_recovery_guidance_reaches_both_classifiers():
    from intaris import judge_simulator, l1_projection
    from intaris.task_support import INSTRUCTION_SCOPE_GUIDANCE

    assert "if it cannot launch" in INSTRUCTION_SCOPE_GUIDANCE
    assert "Do not infer that the condition occurred" in INSTRUCTION_SCOPE_GUIDANCE
    assert "Explicit no-rollback" in INSTRUCTION_SCOPE_GUIDANCE
    assert INSTRUCTION_SCOPE_GUIDANCE in judge_simulator.SYSTEM
    assert INSTRUCTION_SCOPE_GUIDANCE in l1_projection.SYSTEM
