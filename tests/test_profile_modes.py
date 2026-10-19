"""Owner-decided profile semantics: strict cites, balanced intent, bounded autonomy."""

import json
from unittest.mock import Mock

import httpx
import pytest
from openai import UnprocessableEntityError

from intaris import judge_simulator
from intaris.classifier import observe_hybrid
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.intention_profile import validate_items
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime_fixture

runtime = runtime_fixture
RELAXED = ["balanced", "bounded_autonomy"]
SOURCES = {"session_intention": {"kind": "assignment", "independent_grant": True}}


def assessed(facts, profile, **answers):
    values = dict(
        relationship="supports_intent",
        risk="low",
        contextual_restrictions="unresolved",
        restriction_reference="none",
        target_scope="unresolved",
        target_reference="none",
        authorization="absent",
        authorization_source="none",
        authorization_scope="unclear",
    )
    values.update(answers)
    state = from_observations(facts, sources=SOURCES).model_copy(
        update={"assessment": values}
    )
    return evaluate_policy([], state, profile, assessed=True), state


EDIT = {"tool": "edit", "effects": ["write"], "mutation": "mutating"}
GMAIL = {
    "tool": "mcp_gmail__send_email",
    "effects": ["transmit"],
    "mutation": "mutating",
    "external_or_irreversible": True,
}
EXTERNAL_HIGH = {"tool": "bash", "effects": ["delete"], "mutation": "mutating"}
EXPLICIT = dict(
    authorization="explicit",
    authorization_source="session_intention",
    authorization_scope="within_scope",
)


@pytest.mark.parametrize("profile", ["strict", *RELAXED])
def test_requested_session_data_audit_read(runtime, profile):  # noqa: F811
    """Production example: the user asked to audit session data; the agent reads it."""
    sessions, _, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})

    def generate(*args, **kwargs):
        answer = json.loads(response(kwargs))
        props = kwargs["json_schema"]["schema"]["properties"]
        answer.update(
            {
                k: v
                for k, v in {
                    "mutation": "read_only",
                    "effects": ["read"],
                    "operation": "read",
                    "relationship": "directly_serves_intent",
                    "authorization": "absent",
                    "authorization_scope": "unclear",
                    "authorization_source": "none",
                    "consequence": "routine",
                }.items()
                if k in props
            }
        )
        return json.dumps(answer)

    llm.generate.side_effect = generate
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="mcp_intaris__get_session_audit",
        args={"session_id": "sess-under-audit"},
    )
    metadata = result["evaluation_metadata"]
    assert result["decision"] == "approve"
    assert not metadata["judge_required"]
    assert metadata["policy_result"]["permission_basis"] == "intent_aligned"
    assert llm.generate.call_count == 1  # L1 only; no Judge call.


def test_balanced_unclear_mutation_does_not_require_permission_proof():
    result, _ = assessed(EDIT, "balanced", relationship="unclear")
    assert result.outcome == "approve"
    assert not result.outstanding_obligations


@pytest.mark.parametrize(
    "answers,reason",
    [
        ({"relationship": "unrelated", "risk": "medium"}, "intent.unrelated"),
        ({"relationship": "conflicts_with_intent"}, "model.denial"),
        ({"risk": "high"}, "risk.high"),
        (
            {
                "contextual_restrictions": "conflicts",
                "restriction_reference": "session_intention",
            },
            "model.denial",
        ),
        ({"target_scope": "conflicts", "target_reference": "action"}, "model.denial"),
    ],
)
def test_balanced_escalates_only_for_listed_reasons(answers, reason):
    result, _ = assessed(EDIT, "balanced", **answers)
    assert result.outcome == "escalate"
    assert reason in {item.id for item in result.outstanding_obligations}


def test_gmail_send_explicit_authorization_and_profiles():
    for profile in ("balanced", "bounded_autonomy"):
        for answers in ({}, EXPLICIT):
            initial, state = assessed(GMAIL, profile, **answers)
            assert initial.outcome == "approve"


def test_force_push_severity_not_external_label_owns_review():
    args = {"command": "git push --force origin main"}
    facts = observe_hybrid("bash", args, policy={}, working_directory="/repo")
    balanced, _ = assessed(facts, "balanced", risk="high")
    assert balanced.outcome == "escalate"
    assert "risk.high" in {o.id for o in balanced.outstanding_obligations}
    medium, _ = assessed(facts, "bounded_autonomy", risk="medium")
    assert medium.outcome == "approve"
    high, state = assessed(facts, "bounded_autonomy", risk="high")
    assert high.outcome == "escalate"
    assert {o.id for o in high.outstanding_obligations} == {
        "risk.high",
    }
    refused = state.model_copy(
        update={
            "review": judge_simulator.receipt(
                state,
                high,
                judge_simulator.Answer(
                    user_would="refuse",
                    risk="high",
                    confidence="high",
                    basis="irreversible_unclear",
                    cites=[],
                    question="",
                    corrected_assessment={"relationship": "unrelated"},
                ),
            )
        }
    )
    assert evaluate_policy([], refused, "bounded_autonomy", assessed=True).outcome == (
        "deny"
    )


@pytest.mark.parametrize("profile", ["strict", *RELAXED])
def test_deny_rules_human_denials_and_floors_still_win(profile):
    denied, _ = assessed({**EDIT, "configured_deny": True}, profile)
    assert denied.outcome == "deny"
    approved, _ = assessed(
        EDIT,
        "balanced",
        relationship="supports_intent",
        target_scope="consistent",
        contextual_restrictions="consistent",
    )
    assert approved.outcome == "approve"
    human = finalize_result(
        approved, maximum=None, minimum=None, human_resolution="deny"
    )
    assert human.decision == "deny"
    floor = finalize_result(approved, maximum=None, minimum="deny")
    assert floor.decision == "deny"
    escalate = finalize_result(approved, maximum=None, minimum="escalate")
    assert escalate.decision == "escalate"


def question_for(profile, facts=EDIT):
    state = from_observations(facts)
    return judge_simulator.request(
        {"tool": facts["tool"], "args_redacted": {}},
        {"policy": {"profile": profile}, "intention": "Implement the parser"},
        state,
        evaluate_policy([], state, profile),
        {"originals": [], "items": []},
    )


def judged(question, **answer):
    value = dict(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="",
        action_summary="Perform the proposed action",
        explanation="Assess its concrete effect against the current task and policy.",
        restriction_quote="",
    )
    value.update(answer)
    llm = Mock(generate=Mock(return_value=json.dumps(value)))
    return judge_simulator.classify(llm, question)


def test_judge_contract_per_profile():
    strict = question_for("strict")
    assert judged(strict)[0].user_would == "approve"
    balanced = question_for("balanced")
    assert balanced["messages"][0] != strict["messages"][0]
    answer, diagnostics = judged(balanced)
    assert answer.user_would == "approve"
    assert diagnostics["downgrade_reason"] is None
    # Relaxed routine approvals do not manufacture per-action citation requirements.
    assert judged(question_for("balanced", GMAIL))[0].user_would == "approve"
    assert judged(question_for("balanced", GMAIL), cites=[1])[0].user_would == (
        "approve"
    )
    bounded = question_for("bounded_autonomy", GMAIL)
    enum = bounded["schema"]["schema"]["properties"]["user_would"]["enum"]
    assert enum == ["approve", "refuse"]
    assert bounded["messages"][0] != strict["messages"][0]
    assert judged(bounded)[0].user_would == "approve"


def test_bounded_denial_names_the_specific_reason():
    answer = judge_simulator.Answer(
        user_would="refuse",
        risk="high",
        confidence="high",
        basis="restriction_conflict",
        cites=[],
        question="",
    )
    assert "conflicts with a task restriction" in judge_simulator.user_explanation(
        answer, {}, "deny"
    )


def test_judge_retries_provider_json_validation_422():
    request = httpx.Request("POST", "http://managed/v1/chat/completions")
    error = UnprocessableEntityError(
        "invalid json",
        response=httpx.Response(422, request=request),
        body={"error": {"code": "upstream_json_validate_failed"}},
    )
    valid = json.dumps(
        {
            "user_would": "approve",
            "risk": "low",
            "confidence": "high",
            "basis": "routine_within_task",
            "cites": [],
            "question": "",
            "action_summary": "Perform the proposed action",
            "explanation": "The action supports the current task.",
            "restriction_quote": "",
        }
    )
    llm = Mock(
        judge_response_mode="json_object", generate=Mock(side_effect=[error, valid])
    )
    answer, diagnostics = judge_simulator.classify(llm, question_for("balanced"))
    assert diagnostics["response_origin"] == "model"
    assert diagnostics["validation_errors"] == ["UnprocessableEntityError"]
    assert answer.user_would == "approve"
    assert llm.generate.call_count == 2


def test_quote_longer_than_240_is_kept_verbatim():
    content = "Please refactor the parser " + "carefully " * 60 + "and keep its API."
    sources = [{"ref": "message:1", "origin": "client_user_claim", "content": content}]
    drops = []
    accepted, dropped = validate_items(
        [
            {
                "kind": "request",
                "msg": 1,
                "quote": content,
                "status": "current",
                "confirmation": None,
            }
        ],
        sources,
        drop_reasons=drops,
    )
    assert len(content) > 240
    assert (dropped, drops) == (0, [])
    assert accepted[0]["quote"] == content


def test_allowed_outcomes_follow_session_caps():
    assert judge_simulator.allowed_outcomes() == {"approve", "escalate", "deny"}
    assert judge_simulator.allowed_outcomes("escalate") == {"approve", "escalate"}
    assert judge_simulator.allowed_outcomes("approve") == {
        "approve",
        "escalate",
        "deny",
    }


def test_bounded_with_max_escalate_offers_approve_or_ask_and_caps_deny():
    state = from_observations(GMAIL)
    question = judge_simulator.request(
        {"tool": GMAIL["tool"], "args_redacted": {}},
        {"policy": {"profile": "bounded_autonomy"}, "intention": "Email the team"},
        state,
        evaluate_policy([], state, "bounded_autonomy"),
        {
            "originals": [
                {
                    "ref": "original-request",
                    "origin": "recorded_user_event",
                    "content": "Never send email externally.",
                }
            ],
            "items": [],
        },
        outcomes=judge_simulator.allowed_outcomes("escalate"),
    )
    enum = question["schema"]["schema"]["properties"]["user_would"]["enum"]
    assert enum == ["approve", "refuse"]
    system = question["messages"][0]["content"]
    assert "Never ask" not in system
    assert "Allowed user_would answers: approve, refuse." in system
    # A refusal the caps cannot produce is repaired, then falls back to ask.
    answer, diagnostics = judged(
        question,
        user_would="refuse",
        risk="high",
        basis="restriction_conflict",
        cites=[1],
        restriction_quote="Never send email externally.",
    )
    assert answer.user_would == "refuse"
    assert diagnostics["validation_errors"] == []
    # The profile's would-be deny is capped to escalate by finalization.
    _, high = assessed(GMAIL, "bounded_autonomy", risk="high")
    refused = high.model_copy(
        update={
            "review": judge_simulator.receipt(
                high,
                evaluate_policy([], high, "bounded_autonomy", assessed=True),
                judge_simulator.Answer(
                    user_would="refuse",
                    risk="high",
                    confidence="high",
                    basis="restriction_conflict",
                    cites=[],
                    question="",
                    corrected_assessment={
                        "contextual_restrictions": "conflicts",
                        "restriction_reference": "session_intention",
                    },
                ),
            )
        }
    )
    result = evaluate_policy([], refused, "bounded_autonomy", assessed=True)
    assert result.outcome == "deny"
    final = finalize_result(result, maximum="escalate", minimum=None)
    assert final.decision == "escalate"
    assert final.override == "session_policy.maximum_outcome"


@pytest.mark.parametrize("profile", ["strict", *RELAXED])
def test_minimum_deny_wins_over_profile_and_cap(profile):
    approved, _ = assessed(
        EDIT,
        profile,
        **EXPLICIT,
        target_scope="consistent",
        target_reference="action",
        contextual_restrictions="consistent",
        restriction_reference="session_intention",
    )
    final = finalize_result(approved, maximum="escalate", minimum="deny")
    assert final.decision == "deny"
    assert final.override == "request.minimum_outcome"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["strict", *RELAXED])
async def test_minimum_deny_wins_and_spends_no_judge_call(runtime, profile):  # noqa: F811
    from intaris.config import JudgeConfig
    from intaris.judge import JudgeReviewer

    sessions, audit, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})
    llm.generate.side_effect = lambda *a, **kw: response(kw, high=True)
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/parser.py"},
        minimum_outcome="deny",
    )
    assert result["decision"] == "deny"
    calls = llm.generate.call_count
    await JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    ).review_and_resolve(call_id=result["call_id"], user_id="owner", session_id=sid)
    assert llm.generate.call_count == calls
    assert audit.get_by_call_id(result["call_id"], user_id="owner")["decision"] == (
        "deny"
    )


@pytest.mark.parametrize(
    "answers", [{"relationship": "unrelated", "risk": "medium"}, {"risk": "high"}]
)
def test_balanced_reasons_precede_configured_grant(answers):
    result, _ = assessed({**EDIT, "configured_allow": True}, "balanced", **answers)
    assert result.outcome == "escalate"


@pytest.mark.parametrize("profile", ["strict", *RELAXED])
def test_judge_critical_approval_never_approves(profile):
    result, state = assessed(EXTERNAL_HIGH, profile, risk="high")
    reviewed = state.model_copy(
        update={
            "review": judge_simulator.receipt(
                state,
                result,
                judge_simulator.Answer(
                    user_would="approve",
                    risk="critical",
                    confidence="high",
                    basis="routine_within_task",
                    cites=[],
                    question="",
                ),
            )
        }
    )
    outcome = evaluate_policy([], reviewed, profile, assessed=True).outcome
    assert outcome == "deny"


def test_judge_policy_fallback_respects_maximum_escalate():
    result, _ = assessed(GMAIL, "balanced", risk="high")
    final = finalize_result(
        result,
        maximum="escalate",
        minimum=None,
        judge_allowed=frozenset({"approve", "deny"}),
        judge_uncertain="deny",
    )
    assert final.decision == "escalate"
