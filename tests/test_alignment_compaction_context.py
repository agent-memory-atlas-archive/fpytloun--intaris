"""Compaction summaries do not replace declared task evidence."""

import json
from unittest.mock import Mock

import pytest

from intaris.alignment import check_intention_alignment


@pytest.mark.parametrize("aligned", [True, False])
def test_original_assignments_survive_vague_summaries_without_alignment_bypass(aligned):
    llm = Mock()
    llm.generate.return_value = json.dumps(
        {
            "aligned": aligned,
            "reasoning": "Supporting debrief." if aligned else "Unrelated assignment.",
        }
    )
    parent = "Update the Library controls in the Cognis macOS application."
    child = (
        "Preserve relevant knowledge learned from this development conversation."
        if aligned
        else "Delete an unrelated project's repository."
    )
    result, _ = check_intention_alignment(
        llm=llm,
        parent_intention="Continued conversation",
        child_intention="No new work is requested.",
        parent_assignment=parent,
        child_assignment=child,
        parent_user_evidence={"content": "Keep existing public interfaces unchanged."},
    )
    assert result is aligned
    messages = llm.generate.call_args.args[0]
    assert parent in messages[1]["content"]
    assert child in messages[1]["content"]
    assert "Keep existing public interfaces unchanged." in messages[1]["content"]
    assert "not human authorization" in messages[1]["content"]
    assert "lossy interpretations" in messages[0]["content"]
    assert "not a blanket exception for hooks" in messages[0]["content"]
    llm.generate.assert_called_once()
