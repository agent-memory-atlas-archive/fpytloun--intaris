import pytest

from intaris.hybrid import digest, evaluate_policy
from intaris.policy_activation import HybridError
from intaris.policy_evidence import Evidence
from intaris.process_scope import ProcessScope, verify_declaration, verify_spawn


def declaration(operation="spawn"):
    args = {"command": "sleep 30", "run_in_background": True}
    body = dict(
        version=1,
        operation=operation,
        session_id="session-current",
        conversation_id="conversation-owner",
        execution_scope="task-run",
        logical_call_id="call-start",
        executor_id="executor-primary",
        tool="bash",
        evaluated_args_digest=digest(args),
    )
    if operation == "terminate":
        args = {"shell_id": "shell-owned"}
        body.update(
            tool="bash_kill",
            logical_call_id="call-stop",
            evaluated_args_digest=digest(args),
            shell_id="shell-owned",
            incarnation="process-incarnation",
            spawn_call_id="audit-start",
            spawn_session_id="session-current",
            spawn_logical_call_id="call-start",
        )
    scope = ProcessScope(**body)
    claims = dict(
        sub="owner",
        agent_id="agent",
        session_id=scope.session_id,
        capabilities=["intaris:process_lifecycle"],
        process_scope_digest=digest(scope.model_dump()),
    )
    return scope, args, claims


def admitted_spawn():
    scope, _, _ = declaration()
    return {
        "call_id": "audit-start",
        "session_id": scope.session_id,
        "tool": "bash",
        "evaluation_metadata": {
            "final_policy_result": {"decision": "approve"},
            "process_lifecycle": {
                "authenticated": True,
                "declaration": scope.model_dump(),
            },
        },
    }


@pytest.mark.parametrize("operation", ["spawn", "terminate"])
def test_authenticated_exact_action(operation):
    scope, args, claims = declaration(operation)
    assert (
        verify_declaration(
            scope.model_dump(),
            claims=claims,
            user_id="owner",
            agent_id="agent",
            session_id=scope.session_id,
            tool=scope.tool,
            args=args,
        )
        == scope
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("sub", "other-owner"),
        ("agent_id", "other-agent"),
        ("session_id", "other-session"),
        ("process_scope_digest", "0" * 64),
        ("capabilities", []),
    ],
)
def test_claim_mismatch_never_establishes_authority(field, value):
    scope, args, claims = declaration()
    claims[field] = value
    with pytest.raises(HybridError):
        verify_declaration(
            scope.model_dump(),
            claims=claims,
            user_id="owner",
            agent_id="agent",
            session_id=scope.session_id,
            tool=scope.tool,
            args=args,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("executor_id", "other-executor"),
        ("execution_scope", "other-task"),
        ("conversation_id", "other-conversation"),
        ("spawn_call_id", "other-admission"),
        ("spawn_logical_call_id", "other-call"),
    ],
)
def test_registration_must_match_admitted_spawn(field, value):
    scope, _, _ = declaration("terminate")
    with pytest.raises(HybridError):
        verify_spawn(scope.model_copy(update={field: value}), admitted_spawn())


def test_failed_or_legacy_spawn_cannot_grant_cleanup():
    scope, _, _ = declaration("terminate")
    record = admitted_spawn()
    record["evaluation_metadata"]["final_policy_result"]["decision"] = "deny"
    with pytest.raises(HybridError):
        verify_spawn(scope, record)
    with pytest.raises(HybridError):
        verify_spawn(scope, {"evaluation_metadata": {}})


def lifecycle_evidence():
    scope, _, _ = declaration("terminate")
    return Evidence(
        observations={},
        process_lifecycle=verify_spawn(scope, admitted_spawn()),
        assessment={
            "relationship": "unclear",
            "risk": "medium",
            "effects": ["execute"],
            "mutation": "mutating",
            "target_scope": "consistent",
            "contextual_restrictions": "consistent",
        },
    )


def test_verified_lifecycle_does_not_need_another_stop_instruction():
    result = evaluate_policy([], lifecycle_evidence())
    assert result.outcome == "approve"
    assert result.permission_basis == "task_process_lifecycle"


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize("vote", ["approve", "ask", "refuse"])
def test_partial_lifecycle_assessment_is_resolved_by_contextual_judge(profile, vote):
    from intaris.hybrid_runtime import finalize_result
    from intaris.judge_simulator import Answer, receipt

    state = lifecycle_evidence().model_copy(
        update={
            "assessment": {"risk": "low", "contextual_restrictions": "unresolved"},
        }
    )
    initial = evaluate_policy([], state, profile, assessed=True)
    if profile in {"balanced", "bounded_autonomy"}:
        assert initial.outcome == "approve"
        assert initial.permission_basis == "task_process_lifecycle"
        return
    assert initial.outcome == "escalate"
    assert {o.id for o in initial.outstanding_obligations} == {
        "process.current_restrictions"
    }
    answer = Answer(
        user_would=vote,
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="",
        action_summary="Stop the task-owned temporary diagnostic helper.",
        explanation="This cleanup supports the admitted task.",
    )
    state = state.model_copy(update={"review": receipt(state, initial, answer)})
    reviewed = evaluate_policy([], state, profile, assessed=True)
    assert (
        reviewed.outcome
        == {
            "approve": "approve",
            "ask": "escalate",
            "refuse": "deny",
        }[vote]
    )
    final = finalize_result(
        reviewed,
        maximum=None,
        minimum=None,
        judge_allowed=frozenset({"approve", "deny"}),
    )
    assert final.decision == ("approve" if vote == "approve" else "deny")


@pytest.mark.parametrize("risk", ["high", "unknown", "critical"])
def test_lifecycle_risk_requires_its_own_review(risk):
    state = lifecycle_evidence().model_copy(
        update={
            "assessment": {"risk": risk, "contextual_restrictions": "consistent"},
        }
    )
    result = evaluate_policy([], state, assessed=True)
    assert result.outcome != "approve"
    assert "intent.assessment" not in {o.id for o in result.outstanding_obligations}


def test_lifecycle_review_cannot_clear_explicit_checkpoint():
    from intaris.hybrid import Rule
    from intaris.judge_simulator import Answer, receipt
    from intaris.policy_evidence import Observation

    state = lifecycle_evidence().model_copy(
        update={
            "observations": {
                "tool": Observation(
                    value="bash_kill", source="controller", coverage="complete"
                )
            },
            "assessment": {"risk": "low", "contextual_restrictions": "unresolved"},
        }
    )
    rules = [
        Rule.model_validate(
            {
                "id": "stop-checkpoint",
                "effect": "require_approval",
                "when": {"op": "tool_matches", "value": "bash_kill"},
            }
        )
    ]
    initial = evaluate_policy(rules, state)
    answer = Answer(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="",
        action_summary="Stop helper.",
        explanation="Task cleanup.",
    )
    state = state.model_copy(update={"review": receipt(state, initial, answer)})
    final = evaluate_policy(rules, state)
    assert final.outcome == "escalate"
    assert "human.checkpoint" in {o.id for o in final.outstanding_obligations}


def test_lifecycle_does_not_override_current_restrictions():
    state = lifecycle_evidence()
    assert (
        evaluate_policy(
            [], state.model_copy(update={"read_only_required": True})
        ).outcome
        == "deny"
    )
    state.sources["keep-running"] = {
        "origin": "recorded_user_event",
        "constraint_kinds": ["restriction"],
    }
    state.assessment.update(
        contextual_restrictions="conflicts", restriction_reference="keep-running"
    )
    assert evaluate_policy([], state).outcome != "approve"


def test_ownership_without_authenticated_lifecycle_cannot_claim_lifecycle_authority():
    state = lifecycle_evidence()
    state = state.model_copy(update={"process_lifecycle": {"matched": True}})
    result = evaluate_policy([], state)
    # Balanced can admit medium-risk task work without a lifecycle shortcut,
    # but an unauthenticated matched flag never establishes lifecycle authority.
    assert result.permission_basis != "task_process_lifecycle"
    assert evaluate_policy([], state, "strict").outcome != "approve"
