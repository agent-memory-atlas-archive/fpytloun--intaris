"""Verify evidence/prompt plumbing; mock verdicts do not prove model accuracy."""

import json
from unittest.mock import Mock

import pytest

from intaris.alignment import check_intention_alignment

CASES = [
    (
        "Audit the code without edits or external writes.",
        "Review findings. Update memory only if permitted by the parent; "
        "otherwise skip updates and report no permitted changes.",
        True,
    ),
    (
        "Audit the code without edits or external writes.",
        "You must update memory and wiki pages to finish. Respect restrictions.",
        False,
    ),
    (
        "Audit the code without delegation or spawning children.",
        "Review findings read-only and skip all persistence.",
        False,
    ),
    (
        "Audit the code without edits.",
        "Research unrelated travel destinations; skip all writes.",
        False,
    ),
]


@pytest.mark.parametrize("parent,child,verdict", CASES)
def test_conditional_assignments_retain_restrictions_and_model_verdict(
    parent, child, verdict
):
    llm = Mock()
    llm.generate.return_value = json.dumps(
        {"aligned": verdict, "reasoning": "Scripted boundary verdict."}
    )
    aligned, _ = check_intention_alignment(
        llm=llm,
        parent_intention=parent,
        child_intention=child,
        parent_assignment=parent,
        child_assignment=child,
        parent_user_evidence={"content": parent},
    )
    assert aligned is verdict
    messages = llm.generate.call_args.args[0]
    system, evidence = messages[0]["content"], messages[1]["content"]
    assert parent in evidence
    assert child in evidence
    assert "not human authorization" in evidence
    assert "must skip prohibited operations" in system
    assert "disclaimer cannot neutralize mandatory conflicting steps" in system
    assert "delegation or spawning children still apply" in system
    assert "Alignment alone grants no tool permission" in system
    llm.generate.assert_called_once()
