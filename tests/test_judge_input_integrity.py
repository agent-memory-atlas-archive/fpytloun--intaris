"""Independent review uses source instructions, not disputed derived labels."""

import json
from unittest.mock import Mock

import pytest

from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import Answer, classify, request
from intaris.policy_evidence import from_observations


def packet(*, original=True, truncated=False, missing=False, omitted=False):
    sources = [
        {
            "ref": "session_intention",
            "origin": "generated_summary",
            "content": "Read-only phase: no changes of any kind.",
        }
    ]
    if original:
        sources.append(
            {
                "ref": "build-approval",
                "origin": "client_user_claim",
                "content": "Plan approved. Build in the task worktree. Do not push.",
                "truncated": truncated,
            }
        )
    if omitted:
        sources.append(
            {
                "ref": "older-user",
                "origin": "client_user_claim",
                "content": "",
                "truncated": True,
                "render_truncated": True,
            }
        )
    artifact = {
        "coverage": {"missing": ["original_assignment_unavailable"] if missing else []},
        "originals": sources,
        "items": [
            {
                "kind": "restriction",
                "status": "current",
                "quote": "no changes of any kind",
                "source_ref": "session_intention",
                "current": True,
            }
        ],
    }
    state = from_observations(
        {"tool": "apply_patch", "arguments": {"patch": "task edit"}}
    )
    return request(
        {"tool": "apply_patch"},
        {"intention": sources[0]["content"], "policy": {"profile": "balanced"}},
        state,
        evaluate_policy([], state),
        artifact,
    )


def test_original_authority_is_not_duplicated_as_generated_restriction():
    q = packet()
    body = json.loads(q["messages"][1]["content"])
    assert "intention_items" not in body
    assert "no changes of any kind" not in q["messages"][1]["content"]
    assert "Do not push." in q["messages"][1]["content"]
    assert any(row["ref"] == "build-approval" for row in q["citation_sources"])
    assert not any(row["ref"] == "session_intention" for row in q["citation_sources"])
    summary = next(
        row
        for row in body["raw_task_context"]["sources"]
        if row["ref"] == "session_intention"
    )
    assert summary["body_availability"] == "omitted_generated_interpretation"


@pytest.mark.parametrize("original,truncated", [(False, False), (True, True)])
def test_summary_fallback_remains_when_original_instructions_are_incomplete(
    original, truncated
):
    q = packet(original=original, truncated=truncated)
    assert "no changes of any kind" in q["messages"][1]["content"]


@pytest.mark.parametrize("options", [{"missing": True}, {"omitted": True}])
def test_partial_originals_retain_remaining_summary_context(options):
    q = packet(**options)
    assert "no changes of any kind" in q["messages"][1]["content"]


@pytest.mark.parametrize("vote", ["approve", "ask", "refuse"])
def test_long_clarification_does_not_corrupt_decisive_fields(vote):
    answer = Answer(
        user_would=vote,
        risk="low",
        confidence="high",
        basis="irreversible_unclear" if vote == "ask" else "routine_within_task",
        cites=[],
        question="Which exact report and destination should I use? " * 6,
        action_summary="Handle task evidence.",
        explanation="A contextual assessment.",
    )
    llm = Mock()
    llm.generate.return_value = answer.model_dump_json()
    actual, diagnostics = classify(llm, packet())
    assert actual.user_would == vote
    assert actual.question == (answer.question if vote == "ask" else "")
    assert diagnostics["response_origin"] == "model"
    llm.generate.assert_called_once()
