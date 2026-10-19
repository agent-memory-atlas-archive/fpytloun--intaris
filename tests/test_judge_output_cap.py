"""Judge output cap and failure wording.

Regression based on a captured failure with identifiers removed. The Judge was
called with max_tokens=500; gpt-oss reasoning tokens consumed the cap, the JSON
was truncated and the managed route returned 422 twice. The card then showed a
missing_permission fallback with "Retry or request human review".
"""

import json
from unittest.mock import Mock

import httpx
import pytest
from openai import UnprocessableEntityError

from intaris import judge_simulator as simulator
from intaris import policy_assessment
from intaris.config import JudgeConfig
from intaris.judge import JudgeReviewer
from intaris.llm import STRUCTURED_OUTPUT_MAX_TOKENS
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime
from tests.test_rc5b_fixes import grep_state, judge_question
from tools.l1_projection_sizes import specimen, valid_answer

L1_REASON = (
    "The command could not be verified as read-only and the evaluator "
    "flagged it as modifying files."
)


def json_422():
    return UnprocessableEntityError(
        "invalid json",
        response=httpx.Response(
            422, request=httpx.Request("POST", "http://managed/v1/chat/completions")
        ),
        body={"error": {"code": "upstream_json_validate_failed"}},
    )


TRUNCATED = '{"user_would": "approve", "risk": "low", "confid'


@pytest.mark.parametrize("failure", ["422", "truncated"])
@pytest.mark.parametrize("profile", ["strict", "balanced"])
def test_failed_judge_states_failure_without_missing_permission(failure, profile):
    error = json_422() if failure == "422" else TRUNCATED
    llm = Mock(generate=Mock(side_effect=[error, error]))
    answer, diagnostics = simulator.classify(llm, judge_question(grep_state(), profile))
    assert diagnostics["response_origin"] == "assessment_failed"
    assert diagnostics["model_answer"] is None
    assert answer.user_would == "ask"  # still escalated to the human
    assert answer.basis == simulator.REVIEW_UNAVAILABLE
    text = simulator.user_explanation(
        answer,
        {"status": "ready"},
        "escalate",
        {"tool": "edit", "reasoning": L1_REASON},
    )
    assert text == (
        "Intaris's automated reviewer couldn't complete its review. "
        f"{L1_REASON} Approve edit for this specific action?"
    )
    for banned in (
        "Retry or request human review",
        "missing_permission",
        "Permission for this action has not been verified",
    ):
        assert banned not in text


def test_failed_judge_without_l1_reason_and_on_rereview():
    answer = simulator.Answer(
        user_would="ask",
        risk="medium",
        confidence="low",
        basis=simulator.REVIEW_UNAVAILABLE,
        cites=[],
        question="Please confirm this action.",
    )
    expected = (
        "Intaris's automated reviewer couldn't complete its review. "
        "Approve bash for this specific action?"
    )
    assert simulator.user_explanation(answer, {}, "escalate", {"tool": "bash"}) == (
        expected
    )
    # A previous failure text is not repeated.
    assert (
        simulator.user_explanation(
            answer, {}, "escalate", {"tool": "bash", "reasoning": expected}
        )
        == expected
    )
    # Unavailable task context does not mask the review failure.
    assert simulator.user_explanation(
        answer, {"status": "unavailable"}, "escalate", {"tool": "bash"}
    ).startswith("Intaris's automated reviewer couldn't complete its review.")


def test_review_unavailable_is_never_offered_to_the_model():
    for profile in ("strict", "balanced", "bounded_autonomy"):
        question = judge_question(grep_state(), profile)
        basis = question["schema"]["schema"]["properties"]["basis"]["enum"]
        assert simulator.REVIEW_UNAVAILABLE not in basis


def test_judge_cap_is_used_on_every_attempt_including_repairs():
    assert simulator.JUDGE_OUTPUT_MAX_TOKENS >= 4096
    llm = Mock(generate=Mock(side_effect=[json_422(), TRUNCATED]))
    simulator.classify(llm, judge_question(grep_state()))
    assert llm.generate.call_count == 2
    assert {c.kwargs["max_tokens"] for c in llm.generate.call_args_list} == {
        simulator.JUDGE_OUTPUT_MAX_TOKENS
    }


@pytest.mark.parametrize(
    "judge,expected",
    [
        (True, simulator.JUDGE_OUTPUT_MAX_TOKENS),
        (False, STRUCTURED_OUTPUT_MAX_TOKENS),
    ],
)
def test_obligation_review_and_l1_caps(judge, expected):
    artifact, state, result = specimen("inbox-inspection-only")
    question = policy_assessment.request(state, result, "", task_context=artifact)
    question["judge"] = judge
    llm = Mock()
    llm.generate.return_value = json.dumps(valid_answer(question["schema"]))
    try:
        policy_assessment.classify(llm, None, question)
    except KeyError:
        pass  # Judge-mode post-processing expects a review schema; cap is set.
    assert llm.generate.call_args.kwargs["max_tokens"] == expected


@pytest.mark.asyncio
async def test_runtime_failed_judge_card(runtime):  # noqa: F811
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.update_session(
        session_id,
        user_id="owner",
        policy={"profile": "balanced", "maximum_outcome": "escalate"},
    )

    def answer(*args, **kwargs):
        if "user_would" in kwargs["json_schema"]["schema"]["properties"]:
            assert kwargs["max_tokens"] == simulator.JUDGE_OUTPUT_MAX_TOKENS
            raise json_422()
        return response(kwargs, high=True)

    llm.generate.side_effect = answer
    result = evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args={"command": 'grep -E "a(b)" /tmp/x.log'},
    )
    assert result["decision"] == "escalate"
    l1_reason = audit.get_by_call_id(result["call_id"], user_id="owner")["reasoning"]
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id=session_id
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] is None
    assert record["decision"] == "escalate"
    stage = record["evaluation_metadata"]["stages"][-1]
    assert stage["response_origin"] == "assessment_failed"
    assert stage["classification"]["basis"] == simulator.REVIEW_UNAVAILABLE
    card = record["judge_reasoning"]
    assert card.startswith(
        "Intaris's automated reviewer couldn't complete its review. " + l1_reason
    )
    assert "Retry or request human review" not in card
    assert "missing_permission" not in card
