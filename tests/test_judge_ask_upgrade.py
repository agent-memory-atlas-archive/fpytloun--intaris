"""Judge deferral stays deferral even with a low-risk supporting-work basis."""

import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.policy_evidence import from_observations

BASH = {
    "tool": "bash",
    "effects": ["write", "transmit"],
    "mutation": "mutating",
    "external_or_irreversible": True,
}
WRITE = {"tool": "bash", "effects": ["write"], "mutation": "mutating"}


def question_for(
    profile, outcomes=frozenset({"approve", "escalate", "deny"}), facts=WRITE
):
    state = from_observations(facts)
    return judge_simulator.request(
        {"tool": facts["tool"], "args_redacted": {}},
        {"policy": {"profile": profile}, "intention": "Operate homelab alerts"},
        state,
        evaluate_policy([], state, profile),
        {"originals": [], "items": []},
        outcomes=outcomes,
    )


def judged(question, **answer):
    value = dict(
        user_would="ask",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="Proceed?",
        action_summary="Run the scoped maintenance command",
        explanation="The exact routine action is supported by the assignment.",
        restriction_quote="",
    )
    value.update(answer)
    llm = Mock(generate=Mock(return_value=json.dumps(value)))
    return judge_simulator.classify(llm, question)


CAPPED = frozenset({"approve", "escalate"})


@pytest.mark.parametrize(
    "profile,outcomes",
    [
        ("balanced", CAPPED),
        ("balanced", None),
        ("bounded_autonomy", CAPPED),
        ("bounded_autonomy", None),
    ],
)
@pytest.mark.parametrize("basis", ["routine_within_task", "explicit_request"])
def test_common_assessor_preserves_vote_without_basis_upgrade(profile, outcomes, basis):
    question = question_for(profile, *([outcomes] if outcomes else []))
    answer, diagnostics = judged(question, basis=basis)
    assert answer.user_would == "ask"
    assert answer.question == "Proceed?"
    assert diagnostics["upgrade_reason"] is None
    assert diagnostics["model_answer"]["user_would"] == "ask"


def test_bounded_external_ask_with_approving_basis_is_approve():
    answer, _ = judged(question_for("bounded_autonomy", CAPPED, BASH))
    assert answer.user_would == "ask"


def test_balanced_external_routine_approval_needs_no_per_action_cite():
    question = question_for("balanced", CAPPED, BASH)
    assert judged(question)[0].user_would == "ask"
    assert judged(question, cites=[1])[0].user_would == "ask"


def test_strict_ask_is_unchanged():
    answer, diagnostics = judged(question_for("strict"))
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


@pytest.mark.parametrize(
    "answer",
    [
        {"risk": "medium"},
        {"risk": "high"},
        {"basis": "outside_task"},
        {"basis": "restriction_conflict"},
        {"basis": "irreversible_unclear"},
    ],
)
def test_ask_with_other_basis_or_higher_risk_still_asks(answer):
    result, diagnostics = judged(question_for("balanced", CAPPED), **answer)
    assert result.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_unavailable_judge_is_never_upgraded():
    llm = Mock(generate=Mock(side_effect=RuntimeError("down")))
    answer, diagnostics = judge_simulator.classify(
        llm, question_for("balanced", CAPPED)
    )
    assert answer.user_would == "ask"
    assert diagnostics["upgrade_reason"] is None


def test_upgraded_approval_still_yields_to_caps_and_human_checkpoints():
    state = from_observations(BASH).model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
            }
        }
    )
    pending = evaluate_policy([], state, "bounded_autonomy")
    answer, _ = judged(question_for("bounded_autonomy", CAPPED, BASH))
    reviewed = state.model_copy(
        update={"review": judge_simulator.receipt(state, pending, answer)}
    )
    approved = evaluate_policy([], reviewed, "bounded_autonomy", assessed=True)
    assert approved.outcome == "escalate"
    assert any(o.id == "review.deferred" for o in approved.outstanding_obligations)
    floor = finalize_result(approved, maximum=None, minimum="escalate")
    assert floor.decision == "escalate"
    human = finalize_result(
        approved, maximum=None, minimum=None, human_resolution="deny"
    )
    assert human.decision == "deny"


def test_relaxed_contracts_call_own_cleanup_routine():
    assert "Distinguish harmless goal deviation" in judge_simulator.SYSTEM
    assert "policy applies outcomes" in judge_simulator.SYSTEM
