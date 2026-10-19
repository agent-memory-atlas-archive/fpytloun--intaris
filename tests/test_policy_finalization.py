"""Canonical final outcomes/floors, independent of any benchmark projection."""

import pytest

from intaris.hybrid import PolicyResult, evaluate_policy, validate_rules
from intaris.hybrid_runtime import finalize, finalize_result
from intaris.policy_evidence import Obligation
from tests.test_policy_evidence_runtime import runtime as _runtime
from tests.test_policy_evidence_table import evidence, rule, semantic

runtime = _runtime


def test_human_review_floor_does_not_claim_to_have_caused_model_denial():
    result = PolicyResult(outcome="deny", blocking_reasons=["review.refused"])
    final = finalize_result(result, maximum=None, minimum="escalate")
    assert final.decision == "deny"
    assert final.override != "request.minimum_outcome"
    assert "review.refused" in final.blocking_reasons
    assert any(
        item.id == "request.minimum_outcome" for item in final.outstanding_obligations
    )


@pytest.mark.parametrize(
    "profile,expected",
    [
        ("strict", "approve"),
        ("balanced", "approve"),
        ("bounded_autonomy", "approve"),
    ],
)
def test_low_risk_label_does_not_erase_observed_consequence(profile, expected):
    state = evidence(
        observations={
            "environment": "production",
            "effects": ["write"],
            "mutation": "mutating",
        },
        assessment=semantic(risk="low", consequence="routine"),
        reviewed=True,
    )
    result = evaluate_policy([], state, profile)
    assert result.outcome == expected
    assert state.assessment["risk"] == "low"
    if profile == "strict":
        assert not result.outstanding_obligations
        assert (
            evaluate_policy(
                [], state.model_copy(update={"human_approved": True}), profile
            ).outcome
            == "approve"
        )
        grant = validate_rules(
            [rule("explicit", "allow", {"op": "tool_matches", "value": "write"})]
        )
        assert evaluate_policy(grant, state, profile).outcome == "approve"


def test_known_plan_mutation_is_blocking_reason_not_uncertainty():
    state = evidence(assessment=semantic(), reviewed=True)
    state = state.model_copy(update={"read_only_required": True})
    result = evaluate_policy([], state)
    assert result.outcome == "deny"
    assert result.outstanding_obligations == []
    assert result.blocking_reasons == ["request.read_only_required"]
    final = finalize_result(result, maximum="approve", minimum=None)
    assert final.decision == "deny"
    assert final.disposition == "blocked"
    assert final.blocking_reasons == result.blocking_reasons
    assert final.outstanding_obligations == []


def test_request_floor_is_canonical_final_not_prebound_permission():
    result = PolicyResult(outcome="approve", permission_basis="declared")
    final = finalize_result(result, maximum="approve", minimum="escalate")
    assert final.raw_outcome == "approve"
    assert final.decision == "escalate"
    assert final.disposition == "outstanding"
    (floor,) = final.outstanding_obligations
    assert floor.id == "request.minimum_outcome" and floor.protected
    assert floor.requirement == "human"
    assert finalize(result, maximum="approve", minimum="escalate") == (
        final.decision,
        final.override,
    )
    approved = finalize_result(
        result, maximum="approve", minimum="escalate", human_approved=True
    )
    assert approved.decision == "approve"
    assert approved.disposition == "satisfied_by_human"
    assert approved.outstanding_obligations == []
    assert approved.discharged_obligation_ids == ["request.minimum_outcome"]


def test_operator_waiver_is_not_claimed_satisfaction():
    result = PolicyResult(
        outcome="escalate",
        outstanding_obligations=[
            Obligation(
                id="authority.review",
                basis="semantic",
                requirement="independent_review",
            ),
        ],
    )
    final = finalize_result(result, maximum="approve", minimum=None)
    assert final.decision == "approve"
    assert final.disposition == "waived_by_operator_bound"
    assert final.waived_obligation_ids == ["authority.review"]
    assert final.discharged_obligation_ids == []
    assert result.outstanding_obligations  # Raw evidence remains unchanged.
    denied = finalize_result(
        PolicyResult(outcome="deny"), maximum="approve", minimum=None
    )
    assert denied.disposition == "waived_by_operator_bound"


def test_human_discharges_conditional_checkpoint_without_operator_waiver():
    rules = validate_rules(
        [
            rule(
                "production-checkpoint",
                "require_approval",
                {"op": "eq", "field": "environment", "value": "production"},
            )
        ]
    )
    result = evaluate_policy(rules, evidence(assessment=semantic(), reviewed=True))
    assert "production-checkpoint" in result.mandatory_rule_ids
    assert any(
        item.id == "rule:production-checkpoint"
        for item in result.outstanding_obligations
    )
    final = finalize_result(result, maximum=None, minimum=None, human_approved=True)
    assert final.decision == "approve"
    assert final.disposition == "satisfied_by_human"
    assert "rule:production-checkpoint" in final.discharged_obligation_ids
    assert final.outstanding_obligations == []
    assert final.waived_obligation_ids == []


@pytest.mark.parametrize("concurrent_update", ["execution", "judge"])
def test_human_finalization_cannot_overwrite_concurrent_audit_state(
    runtime, monkeypatch, concurrent_update
):
    from intaris.policy_activation import HybridError

    sessions, audit, llm, evaluator, session_id = runtime
    if concurrent_update == "execution":
        import asyncio

        from intaris.config import JudgeConfig
        from intaris.judge import JudgeReviewer
        from tests.test_policy_evidence_runtime import response

        sessions.allocate_event_sequences(
            session_id,
            user_id="owner",
            user_message_flags=[True],
            latest_user_evidence={"content": "Implement the local parser"},
        )
        llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, high=True)
        pending = evaluator.evaluate(
            user_id="owner",
            session_id=session_id,
            agent_id=None,
            tool="write",
            args={"path": "/repo/a"},
        )
        from tests.authorization_fixtures import add_supported_authorization_response

        add_supported_authorization_response(llm)
        judge = JudgeReviewer(
            llm=llm,
            config=JudgeConfig(mode="advisory"),
            audit_store=audit,
            session_store=sessions,
            evaluator=evaluator,
        )
        asyncio.run(
            judge.review_and_resolve(
                call_id=pending["call_id"],
                user_id="owner",
                session_id=session_id,
            )
        )
        assert (
            audit.get_by_call_id(pending["call_id"], user_id="owner")["user_decision"]
            == "approve"
        )
        audit.mark_execution(pending["call_id"], user_id="owner", state="dispatched")
    else:
        pending = evaluator.evaluate(
            user_id="owner",
            session_id=session_id,
            agent_id=None,
            tool="read",
            args={},
            minimum_outcome="escalate",
        )
    original_get = audit.get_by_call_id
    changed = False

    def get_then_update(*args, **kwargs):
        nonlocal changed
        record = original_get(*args, **kwargs)
        if not changed:
            changed = True
            if concurrent_update == "execution":
                audit.mark_execution(
                    pending["call_id"], user_id="owner", state="uncertain"
                )
            else:
                metadata = {
                    **record["evaluation_metadata"],
                    "review_answers": {"retained": "evidence"},
                }
                audit.set_hybrid_review(
                    pending["call_id"],
                    user_id="owner",
                    metadata=metadata,
                    reasoning="Concurrent Judge review",
                    risk="low",
                )
        return record

    monkeypatch.setattr(audit, "get_by_call_id", get_then_update)
    with pytest.raises(HybridError, match="Audit evidence changed"):
        audit.resolve_escalation(pending["call_id"], "approve", user_id="owner")
    record = original_get(pending["call_id"], user_id="owner")
    assert record["user_decision"] == (
        "approve" if concurrent_update == "execution" else None
    )
    if concurrent_update == "execution":
        assert record["evaluation_metadata"]["execution_state"] == "uncertain"
    else:
        assert record["evaluation_metadata"]["review_answers"] == {
            "retained": "evidence"
        }


def test_audit_and_api_share_final_floor_trace(runtime):
    sessions, audit, llm, evaluator, session_id = runtime
    request = dict(
        user_id="owner", session_id=session_id, agent_id=None, tool="read", args={}
    )
    pending = evaluator.evaluate(**request, minimum_outcome="escalate")
    assert pending["decision"] == "escalate"
    final = pending["evaluation_metadata"]["final_policy_result"]
    assert final["decision"] == "escalate"
    assert any(
        item["id"] == "request.minimum_outcome"
        for item in final["outstanding_obligations"]
    )
    record = audit.get_by_call_id(pending["call_id"], user_id="owner")
    assert record["evaluation_metadata"]["final_policy_result"] == final
    audit.resolve_escalation(pending["call_id"], "approve", user_id="owner")
    record = audit.get_by_call_id(pending["call_id"], user_id="owner")
    resolved = record["evaluation_metadata"]["final_policy_result"]
    assert resolved["decision"] == record["user_decision"] == "approve"
    assert resolved["disposition"] == "satisfied_by_human"
    assert resolved["outstanding_obligations"] == []
