"""Judge corrects fallible classifications, not mechanical authorization facts."""

import json

import pytest

from intaris.classifier import secret_argument_disclosure
from intaris.config import JudgeConfig
from intaris.hybrid import Predicate, Rule, evaluate_policy
from intaris.judge import JudgeReviewer
from intaris.policy_evidence import Review, from_observations
from tests.test_intent_first_defaults import model_response
from tests.test_policy_evidence_runtime import runtime as runtime


@pytest.mark.parametrize(
    "command",
    [
        "git diff HEAD -- passwordless-bridge/auth-proxy.yaml",
        "git archive HEAD tokenization/tests secretariat/notes",
        "cat /repo/passwordless/README.md",
    ],
)
def test_secret_word_substrings_do_not_establish_disclosure(command):
    assert not secret_argument_disclosure(command)


@pytest.mark.parametrize(
    "command",
    [
        'echo "$API_TOKEN"',
        "cat /tmp/secrets.txt",
        "git show HEAD:.env",
        "printenv SERVICE_PASSWORD",
        "cat /tmp/client.pem",
        "cat /tmp/client.p12",
        "cat /tmp/client.key",
        "git config --get remote.origin.token",
    ],
)
def test_secret_operands_still_request_assessment(command):
    assert secret_argument_disclosure(command)


def evidence(risk="high", verdict="approve", **observations):
    state = from_observations(
        {
            "tool": "bash",
            "risk": risk,
            "secret_argument": True,
            "effects": ["disclose"],
            "mutation": "read_only",
            **observations,
        }
    )
    return state.model_copy(
        update={
            "assessment": {
                "risk": risk,
                "relationship": "supports_intent",
                "target_scope": "consistent",
                "contextual_restrictions": "consistent",
            },
            "review": Review(
                obligation_ids=["risk.high", "model.risk.critical"],
                evidence_refs=["action"],
                user_would=verdict,
                risk="medium",
                basis="routine_within_task",
            ),
        }
    )


@pytest.mark.parametrize("risk", ["high", "critical"])
@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
def test_judge_can_correct_static_severity_without_erasing_evidence(risk, profile):
    state = evidence(risk)
    original = state.model_dump()
    assert evaluate_policy([], state, profile, assessed=True).outcome == "approve"
    assert state.model_dump() == original
    assert state.observations["risk"].value == risk
    assert state.assessment["risk"] == risk
    assert state.value("risk") == "medium"


def test_unreviewed_critical_heuristic_routes_to_judge_not_irrevocable_denial():
    state = evidence("critical").model_copy(update={"review": None})
    result = evaluate_policy([], state, assessed=True)
    assert result.outcome == "escalate"
    assert "model.risk.critical" in [o.id for o in result.outstanding_obligations]


def test_judge_risk_recomputes_classification_based_policy_predicates():
    rule = Rule(
        id="danger",
        effect="deny",
        when=Predicate(op="eq", field="risk", value="critical"),
    )
    assert (
        evaluate_policy([rule], evidence("critical"), assessed=True).outcome
        == "approve"
    )


@pytest.mark.parametrize("verdict,outcome", [("ask", "escalate"), ("refuse", "deny")])
def test_judge_question_and_refusal_still_bind(verdict, outcome):
    assert (
        evaluate_policy([], evidence(verdict=verdict), assessed=True).outcome == outcome
    )


def test_exact_deny_checkpoint_and_read_only_constraint_still_bind():
    state = evidence()
    deny = Rule(
        id="no-shell", effect="deny", when=Predicate(op="tool_matches", value="bash")
    )
    checkpoint = deny.model_copy(
        update={"id": "confirm-shell", "effect": "require_approval"}
    )
    assert evaluate_policy([deny], state, assessed=True).outcome == "deny"
    assert evaluate_policy([checkpoint], state, assessed=True).outcome == "escalate"
    assert (
        evaluate_policy([], evidence(configured_deny=True), assessed=True).outcome
        == "deny"
    )
    read_only = evidence(mutation="mutating").model_copy(
        update={"read_only_required": True}
    )
    assert evaluate_policy([], read_only, assessed=True).outcome == "deny"


@pytest.mark.asyncio
async def test_live_judge_corrects_secret_heuristic_without_rewriting_audit(runtime):
    sessions, audit, llm, evaluator, sid = runtime
    llm.generate.side_effect = lambda *a, **kw: model_response(
        kw,
        risk="high",
        effects=["read", "disclose"],
        relationship="supports_intent",
        contextual_restrictions="consistent",
        target_scope="consistent",
    )
    # Inert evaluation only: this fixture filename triggers the secret heuristic.
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="bash",
        args={"command": "git show HEAD:secrets.example"},
    )
    assert result["decision"] == "escalate"
    before = audit.get_by_call_id(result["call_id"], user_id="owner")[
        "evaluation_metadata"
    ]["policy_evidence"]
    assert before["observations"]["secret_argument"]["value"] is True
    assert before["observations"]["risk"]["value"] == "high"
    llm.generate.side_effect = None
    llm.generate.return_value = json.dumps(
        {
            "user_would": "approve",
            "risk": "medium",
            "confidence": "high",
            "basis": "routine_within_task",
            "cites": [],
            "question": "",
            "action_summary": "Inspect the repository's documented example fixture.",
            "explanation": "The named file contains examples, not live credentials.",
        }
    )
    reviewer = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="auto"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await reviewer.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id=sid
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] == "approve"
    after = record["evaluation_metadata"]["policy_evidence"]
    assert after["observations"] == before["observations"]
    assert after["assessment"] == before["assessment"]
    assert after["review"]["risk"] == "medium"
    assert after["human_approved"] is False
