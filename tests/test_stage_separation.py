"""Offline contracts for rich intention -> classification -> policy -> Judge."""

import copy
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from intaris import judge_simulator, policy_assessment
from intaris.api.schemas import EvaluateRequest
from intaris.hybrid import evaluate_policy, validate_rules
from intaris.hybrid_runtime import HybridRuntime
from intaris.policy_evidence import AUTHORITY_FIELDS, CorrectedAssessment, Review
from tests.test_hybrid_runtime import stores as _stores
from tools.l1_projection_sizes import specimen

stores = _stores


@pytest.mark.parametrize("projection", ["lines", "json"])
def test_runtime_classifier_has_goal_full_action_not_source_replay(stores, projection):
    _, sessions, audit, llm, _ = stores
    session = sessions.create(
        user_id="owner", session_id="work", intention="Inspect the inbox"
    )
    artifact, state, result = specimen("inbox-inspection-only")
    artifact["originals"].append(
        {
            "ref": "unused",
            "origin": "client_user_claim",
            "content": "RAW_HISTORY_SENTINEL",
        }
    )
    before = copy.deepcopy(artifact)
    state = state.model_copy(
        update={
            "observations": {
                **state.observations,
                "runtime_context": state.observations["arguments"].model_copy(
                    update={
                        "value": {
                            "loaded_skills": "SKILL_SENTINEL",
                            "recent_outcomes": "OUTCOME_SENTINEL",
                        }
                    }
                ),
            }
        }
    )
    runtime = HybridRuntime(
        llm=llm,
        jev=None,
        audit=audit,
        sessions=sessions,
        l1_input_projection=projection,
    )
    q = runtime._l1_request(
        state,
        result,
        session=session,
        tool=state.value("tool"),
        args=state.value("arguments"),
        context={},
        agent_id=None,
        parent_intention="Read-only investigation",
        artifact=artifact,
        overlay={},
    )
    # The selected task, not a restored lossy session summary, owns the goal.
    assert artifact["items"][0]["quote"] in q["prompt"]
    assert "Inspect the inbox" not in q["prompt"]
    assert "Read-only investigation" in q["prompt"]
    assert "msg-731" in q["prompt"] and "msg-732" in q["prompt"]
    assert "Do not delete messages." in q["prompt"]
    assert not AUTHORITY_FIELDS & set(q["fields"])
    for sentinel in ("RAW_HISTORY_SENTINEL", "OUTCOME_SENTINEL"):
        assert sentinel not in q["prompt"]
    assert "SKILL_SENTINEL" in q["prompt"]
    assert artifact == before


@pytest.mark.parametrize("effect", ["deny", "require_approval"])
@pytest.mark.parametrize("mixed", [False, True])
def test_authority_rule_routes_to_review_and_recomputes_validated_correction(
    effect, mixed
):
    _, state, _ = specimen("inbox-inspection-only")
    predicate = {"op": "eq", "field": "authorization", "value": "absent"}
    if mixed:
        predicate = {
            "op": "all",
            "children": [
                {"op": "eq", "field": "relationship", "value": "supports_intent"},
                predicate,
            ],
        }
    rules = validate_rules(
        [{"id": "authority-check", "effect": effect, "when": predicate}]
    )
    state = state.model_copy(
        update={"assessment": {"relationship": "supports_intent", "risk": "low"}}
    )
    result = evaluate_policy(rules, state, assessed=True)
    obligation = next(
        o for o in result.outstanding_obligations if o.id == "rule:authority-check"
    )
    assert obligation.requirement == "independent_review"
    assert set(obligation.fields) <= AUTHORITY_FIELDS
    q = policy_assessment.request(state, result, "")
    assert not AUTHORITY_FIELDS & set(q["fields"])
    reviewed = state.model_copy(
        update={
            "review": Review(
                obligation_ids=["rule:authority-check"],
                evidence_refs=[],
                user_would="approve",
                risk="low",
                corrected_assessment=CorrectedAssessment(authorization="absent"),
            )
        }
    )
    outcome = evaluate_policy(rules, reviewed, assessed=True)
    assert outcome.outcome == ("deny" if effect == "deny" else "escalate")
    assert "authority-check" in outcome.matched_rules


def test_unresolved_supplied_prohibition_is_not_absent_permission():
    _, state, _ = specimen("inbox-inspection-only")
    sources = copy.deepcopy(state.sources)
    sources["latest_user_message"]["constraint_kinds"] = ["restriction"]
    state = state.model_copy(
        update={
            "sources": sources,
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
                "contextual_restrictions": "unresolved",
                "target_scope": "consistent",
            },
        }
    )
    pending = evaluate_policy([], state, "strict", assessed=True)
    assert pending.outcome == "escalate"
    assert pending.outstanding_obligations[0].id == "intent.scope"
    assert pending.outstanding_obligations[0].requirement == "independent_review"
    no_prohibition = state.model_copy(update={"sources": {}})
    for profile in ("balanced", "bounded_autonomy"):
        assert evaluate_policy([], state, profile, assessed=True).outcome == "approve"
        assert (
            evaluate_policy([], no_prohibition, profile, assessed=True).outcome
            == "approve"
        )
    state = state.model_copy(
        update={
            "review": Review(
                obligation_ids=["intent.scope"],
                evidence_refs=["latest_user_message"],
                user_would="approve",
                risk="low",
                corrected_assessment=CorrectedAssessment(
                    contextual_restrictions="consistent",
                    restriction_reference="latest_user_message",
                ),
            )
        }
    )
    assert evaluate_policy([], state, assessed=True).outcome == "approve"


def test_judge_sees_original_refusal_even_if_generated_intention_omitted_it():
    artifact, state, result = specimen("inbox-inspection-only")
    artifact["items"] = []
    artifact["current_request"] = {"item_indices": []}
    artifact["originals"].append(
        {
            "ref": "earlier-refusal",
            "origin": "client_user_claim",
            "content": "Do not delete the archive.",
            "chronology": "0",
            "truncated": False,
        }
    )
    q = judge_simulator.request(
        {"tool": "inbox.delete", "args_redacted": state.value("arguments")},
        {"intention": "Inspect the inbox"},
        state,
        result,
        artifact,
        latest_assistant="May I delete the archive?",
    )
    assert "earlier-refusal" in q["citable"].values()
    packet = json.loads(q["messages"][1]["content"])
    assert q["messages"][1]["content"].count("Do not delete the archive.") == 1
    assert (
        packet["conversation_tail"]["agent_last_message"] == "May I delete the archive?"
    )
    assert packet["original_chronology"]["earlier-refusal"] == "0"
    assert not q["overflow"]


def test_evaluator_custom_authority_rule_never_asks_l1_for_consent(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={
            "rules": [
                {
                    "id": "consent-check",
                    "effect": "deny",
                    "when": {"op": "eq", "field": "authorization", "value": "absent"},
                }
            ]
        },
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/test.py", "content": "new"},
    )
    assert result["decision"] == "escalate"
    properties = llm.generate.call_args.kwargs["json_schema"]["schema"]["properties"]
    assert not AUTHORITY_FIELDS & set(properties)
    assert any(
        item["id"] == "rule:consent-check"
        and item["requirement"] == "independent_review"
        for item in result["evaluation_metadata"]["outstanding_obligations"]
    )


def test_static_read_with_authority_rule_skips_l1_and_routes_to_judge(stores):
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect files",
        policy={
            "rules": [
                {
                    "id": "consent-check",
                    "effect": "deny",
                    "when": {"op": "eq", "field": "authorization", "value": "absent"},
                }
            ]
        },
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/README.md"},
    )
    assert result["decision"] == "escalate"
    llm.generate.assert_not_called()


def test_keep_gate_cannot_skip_refreshing_rich_constraints(stores):
    from intaris.intention import generate_intention
    from tests.test_single_intention import item
    from tests.test_task_context import append_reasoning

    db, sessions, audit, _, _ = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect files",
        policy={"intention_gate": "jev"},
    )
    sessions.update_generated_intention(
        "work",
        user_id="owner",
        intention="Inspect files",
        intention_source="user",
        title="Inspect files",
    )
    append_reasoning(
        audit,
        user_id="owner",
        session_id="work",
        content="User message: Do not deploy.",
        context={"role": "user"},
    )
    gate = Mock()
    gate.should_update_intention.side_effect = AssertionError(
        "Rich generation must not be skipped"
    )
    generator = Mock()
    generator.generate.return_value = json.dumps(
        {
            "title": "Inspect files",
            "intention": "Inspect files. Do not deploy.",
            "items": [item("Do not deploy.", kind="restriction")],
            "current": [],
        }
    )
    result = generate_intention(
        db=db,
        llm=generator,
        session_id="work",
        user_id="owner",
        session_store=sessions,
        intention_source="user",
        jev=gate,
        generation="single",
        context="User message: Do not deploy.",
    )
    assert result is not None and "Do not deploy." in result
    profile = json.loads(sessions.get("work", user_id="owner")["intention_profile"])
    assert profile["items"][0]["kind"] == "restriction"
    generator.generate.assert_called_once()
    gate.should_update_intention.assert_not_called()


@pytest.mark.parametrize("field,limit", [("context", 8192), ("args", 65536)])
def test_payload_size_error_is_field_scoped_exact_and_preserves_boundary(field, limit):
    # Compact ASCII-escaped JSON is the existing contract, including its braces.
    value = {"value": "界" * (limit // 6)}
    size = len(json.dumps(value, separators=(",", ":")))
    assert size > limit
    with pytest.raises(ValidationError) as caught:
        EvaluateRequest(session_id="work", tool="read", **{field: value})
    error = caught.value.errors()[0]
    assert error["loc"] == (field,)
    assert error["type"] == f"{field}_too_large"
    assert error["ctx"] == {"field": field, "size": size, "limit": limit}
    accepted = {"v": "x" * (limit - len('{"v":""}'))}
    EvaluateRequest(session_id="work", tool="read", **{field: accepted})
