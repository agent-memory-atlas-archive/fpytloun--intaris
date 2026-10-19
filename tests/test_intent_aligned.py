"""All profiles follow intention without per-call permission walls."""

import json
from unittest.mock import Mock

import pytest

from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import classify, request
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime


def state(**overrides):
    values = dict(
        relationship="directly_serves_intent",
        risk="low",
        mutation="mutating",
        effects=["write"],
        consequence="consequential",
        contextual_restrictions="consistent",
        restriction_reference="session_intention",
        target_scope="consistent",
        target_reference="action",
        authorization="absent",
        authorization_scope="unclear",
        authorization_source="none",
    )
    values.update(overrides)
    return from_observations(
        {"tool": "edit"},
        sources={
            "session_intention": {"kind": "assignment", "independent_grant": True},
        },
    ).model_copy(update={"assessment": values})


@pytest.mark.parametrize(
    "profile,risk,approved",
    [
        ("balanced", "low", True),
        ("balanced", "medium", True),
        ("balanced", "high", False),
        ("bounded_autonomy", "medium", True),
        ("strict", "low", True),
    ],
)
def test_intent_permission_without_per_action_grant(profile, risk, approved):
    result = evaluate_policy([], state(risk=risk), profile, assessed=True)
    assert (result.permission_basis == "intent_aligned") is approved


@pytest.mark.parametrize(
    "override",
    [
        {"contextual_restrictions": "conflicts"},
        {"target_scope": "conflicts"},
        {"risk": "high"},
        {"relationship": "unrelated", "risk": "medium"},
    ],
)
def test_intent_grant_does_not_cover_risky_or_conflicting_action(override):
    assert (
        evaluate_policy(
            [], state(**override), "balanced", assessed=True
        ).permission_basis
        != "intent_aligned"
    )


def test_operator_advisory_blocks_only_through_l1_conflict_and_disclosure_blocks():
    clause = {"id": "deny_policies.0", "effect": "deny", "text": "Do not edit files"}
    s = state().model_copy(update={"advisory_clauses": [clause]})
    assert evaluate_policy([], s, "balanced").permission_basis == "intent_aligned"
    s = state(
        contextual_restrictions="conflicts", restriction_reference="deny_policies.0"
    ).model_copy(update={"advisory_clauses": [clause]})
    assert evaluate_policy([], s, "balanced").permission_basis != "intent_aligned"
    for facts in (
        {"configured_deny": True},
        {"secret_argument": True, "effects": ["disclose"], "risk": "high"},
    ):
        s = from_observations(facts).model_copy(
            update={"assessment": state().assessment}
        )
        assert evaluate_policy([], s, "balanced").permission_basis != "intent_aligned"


@pytest.mark.parametrize("reference", ["none", "unavailable-reference"])
def test_unresolved_target_reference_is_not_a_balanced_escalation(reference):
    result = evaluate_policy(
        [], state(target_reference=reference), "balanced", assessed=True
    )
    assert result.permission_basis == "intent_aligned"
    strict = evaluate_policy(
        [], state(target_reference=reference), "strict", assessed=True
    )
    assert strict.outcome == "approve"


@pytest.mark.parametrize("path", [".ssh/authorized_keys", ".aws/credentials"])
def test_patch_credential_changes_are_excluded_from_approved_edit_cache(path):
    from intaris.classifier import observe_hybrid
    from intaris.effect_scope import requires_explicit_grant

    facts = observe_hybrid(
        "apply_patch",
        {
            "patchText": f"*** Begin Patch\n*** Update File: /repo/{path}\n@@\n-old\n+new\n*** End Patch\n"
        },
        policy={},
        working_directory="/repo",
    )
    assert facts["external_or_irreversible"]
    s = from_observations(facts).model_copy(update={"assessment": state().assessment})
    assert requires_explicit_grant(s)
    assert evaluate_policy([], s, "balanced", assessed=True).outcome == "approve"
    high = s.model_copy(update={"assessment": state(risk="high").assessment})
    assert evaluate_policy([], high, "balanced", assessed=True).outcome == "escalate"


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "tool,args,mutation,effects",
    [
        ("bash", {"command": "kubectl get pods -o json"}, "read_only", ["read"]),
        ("edit", {"file_path": "/repo/parser.py"}, "mutating", ["write"]),
    ],
)
def test_aligned_audit_read_and_unattended_local_write(
    runtime, profile, tool, args, mutation, effects
):
    sessions, _, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})

    def answer(*a, **kw):
        result = json.loads(response(kw))
        if "relationship" in result:
            result.update(
                authorization="absent",
                authorization_source="none",
                relationship="directly_serves_intent",
                consequence="consequential",
                mutation=mutation,
                effects=effects,
            )
        return json.dumps(
            {
                k: v
                for k, v in result.items()
                if k in kw["json_schema"]["schema"]["properties"]
            }
        )

    llm.generate.side_effect = answer
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool=tool,
        args=args,
        judge_unattended=True,
    )
    assert result["decision"] == "approve"
    assert (
        result["evaluation_metadata"]["policy_result"]["permission_basis"]
        == "intent_aligned"
    )
    assert not result["evaluation_metadata"]["judge_required"]
    assert llm.generate.call_count == 1


@pytest.mark.parametrize(
    "tool,args",
    [
        ("mcp_googleworkspace__send_gmail_message", {"subject": "Reviewed email"}),
        ("bash", {"command": "git push origin main"}),
    ],
)
def test_balanced_aligned_low_risk_transmission_approves_directly(runtime, tool, args):
    sessions, _, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": "balanced"})

    def answer(*a, **kw):
        result = json.loads(response(kw))
        assert "authorization" not in result
        assert "authorization_source" not in result
        if "effects" in result:
            result["effects"] = ["write", "transmit"]
        return json.dumps(result)

    llm.generate.side_effect = answer
    result = evaluator.evaluate(
        user_id="owner", session_id=sid, agent_id=None, tool=tool, args=args
    )
    assert result["decision"] == "approve"
    assert not result["evaluation_metadata"]["judge_required"]


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
def test_judge_question_and_missing_permission_contract(profile):
    s = state(relationship="unclear", contextual_restrictions="unresolved")
    q = request(
        {"tool": "edit"},
        {"policy": {"profile": profile}, "intention": "Implement parser"},
        s,
        evaluate_policy([], s, profile),
        {"originals": [], "items": []},
    )
    # Missing per-action permission is a basis only in strict.
    assert (
        "missing_permission" in q["schema"]["schema"]["properties"]["basis"]["enum"]
    ) is False
    if profile != "strict":
        assert q["citable"]
        llm = Mock(judge_response_mode="json_schema")
        llm.generate.return_value = json.dumps(
            dict(
                user_would="approve",
                risk="low",
                confidence="high",
                basis="routine_within_task",
                cites=[1],
                question="",
                action_summary="Perform the bounded assigned work.",
                explanation="This action supports the recorded task.",
            )
        )
        answer, _ = classify(llm, q)
        assert answer.user_would == "approve"


def test_gmail_routine_approval_does_not_require_per_action_citation():
    s = state(
        effects=["transmit"],
        external_or_irreversible=True,
        authorization="explicit",
        authorization_scope="within_scope",
        authorization_source="session_intention",
    )
    artifact = {
        "originals": [
            {
                "ref": "message:1",
                "content": "Odešli",
                "origin": "client_user_claim",
                "truncated": False,
            }
        ],
        "items": [],
    }
    q = request(
        {"tool": "send_gmail_message"},
        {"policy": {"profile": "balanced"}},
        s,
        evaluate_policy([], s, "balanced"),
        artifact,
    )
    llm = Mock(judge_response_mode="json_schema")
    for basis, cites, expected in [
        ("explicit_request", [1], "approve"),
        ("routine_within_task", [1], "approve"),
        ("routine_within_task", [], "approve"),
    ]:
        llm.generate.return_value = json.dumps(
            dict(
                user_would="approve",
                risk="low",
                confidence="high",
                basis=basis,
                cites=cites,
                question="",
                action_summary="Send the requested email.",
                explanation="The captured request supports this scoped action.",
            )
        )
        assert classify(llm, q)[0].user_would == expected


def test_high_risk_intent_only_cannot_approve_but_existing_explicit_review_is_preserved():
    s = state(risk="high")
    q = request(
        {"tool": "edit"},
        {"policy": {"profile": "balanced"}, "intention": "Implement parser"},
        s,
        evaluate_policy([], s, "balanced"),
        {
            "originals": [
                {
                    "ref": "message:1",
                    "origin": "client_user_claim",
                    "content": "Implement the parser",
                    "truncated": False,
                }
            ],
            "items": [],
        },
    )
    llm = Mock(judge_response_mode="json_schema")
    for basis, expected in [
        ("routine_within_task", "approve"),
        ("explicit_request", "approve"),
    ]:
        llm.generate.return_value = json.dumps(
            dict(
                user_would="approve",
                risk="high",
                confidence="high",
                basis=basis,
                cites=[1],
                question="",
                action_summary="Implement the parser.",
                explanation="The recorded request names the proposed implementation.",
            )
        )
        assert classify(llm, q)[0].user_would == expected
