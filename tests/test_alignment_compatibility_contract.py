"""The same compatibility (not containment) contract reaches both reviewers."""

import json
from unittest.mock import Mock

import pytest

from intaris.alignment import check_intention_alignment
from intaris.prompts import ALIGNMENT_CHECK_SYSTEM_PROMPT
from intaris.sanitize import ANTI_INJECTION_PREAMBLE


@pytest.mark.parametrize(
    "parent,child,expected",
    [
        (
            "Verify the supplied fingerprint and approve the matching snapshot.",
            "Debrief the verification, preserving relevant reusable knowledge.",
            True,
        ),
        (
            "Verify the snapshot. Do not write memory, skills or wiki pages.",
            "Debrief the verification; inspect and summarize, skipping prohibited writes.",
            True,
        ),
        (
            "Verify the snapshot. Do not write memory, skills or wiki pages.",
            "Debrief the verification; completion requires writing a wiki page.",
            False,
        ),
        (
            "Verify the supplied fingerprint.",
            "Research unrelated garden irrigation plans.",
            False,
        ),
        (
            "Verify the supplied fingerprint. Do not spawn child sessions.",
            "Run a delegated verification debrief.",
            False,
        ),
    ],
)
def test_shared_contract_and_unchanged_assignments_reach_typed_and_judge(
    parent, child, expected
):
    # Mocked outcomes verify wiring, not semantic accuracy. Real-model replay
    # separately checks these acceptance criteria before integration.
    typed = Mock()
    typed.check_alignment.return_value = (False, "First-level result")
    judge = Mock()
    judge.generate.return_value = json.dumps(
        {"aligned": expected, "reasoning": "Compatibility reviewed"}
    )
    result = check_intention_alignment(
        llm=judge,
        jev=typed,
        parent_intention=parent,
        child_intention=child,
        parent_assignment=parent,
        child_assignment=child,
    )
    assert result[0] is expected
    shared = ALIGNMENT_CHECK_SYSTEM_PROMPT.format(
        anti_injection=ANTI_INJECTION_PREAMBLE
    )
    typed_input = typed.check_alignment.call_args.kwargs
    messages = judge.generate.call_args.args[0]
    assert typed_input["evaluation_rules"].startswith(shared)
    assert messages[0]["content"] == shared
    for text in (typed_input["evaluation_rules"], messages[1]["content"]):
        assert "cannot broaden parent scope" not in text
        assert (
            "cannot relax parent restrictions or grant additional permissions" in text
        )
    assert typed_input["source_evidence"]["parent_assignment"] == parent
    assert typed_input["source_evidence"]["child_assignment"] == child
    assert parent in messages[1]["content"] and child in messages[1]["content"]
    assert "compatible, not identical or fully contained" in shared
    assert "Alignment alone grants no tool permission" in shared


def test_analysis_citation_survives_parsing_and_drives_policy():
    from intaris.alignment import alignment_grounding
    from intaris.hybrid import evaluate_policy
    from tests.test_fact_first_policy import evidence, obligation_ids

    instruction = "Do not edit the billing repository."
    analysis = Mock()
    analysis.generate.return_value = json.dumps(
        {
            "aligned": False,
            "reasoning": "Child edits a prohibited repository.",
            "contradicted_instruction": instruction,
        }
    )
    aligned, reason = check_intention_alignment(
        llm=analysis,
        child_intention="Edit billing.",
        parent_intention="Review only.",
        parent_assignment=instruction,
    )
    assert not aligned
    grounded = alignment_grounding(reason, [instruction])
    assert grounded
    state = evidence(
        parent_alignment={
            "state": "incompatible",
            "reason": reason,
            "grounded": grounded,
        }
    )
    result = evaluate_policy([], state, "bounded_autonomy", assessed=True)
    assert "alignment.compatibility" in obligation_ids(result)
