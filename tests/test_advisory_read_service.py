"""Advisory operator clauses through real evaluation and audit boundaries."""

import json

import pytest

from intaris.judge import resolve_with_side_effects
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime_fixture

runtime = runtime_fixture

PROFILES = ["strict", "balanced", "bounded_autonomy"]
READS = [
    (
        "Do not access customer records",
        "read",
        {"file_path": "/repo/customers.csv"},
    ),
    (
        "Never open the private secret file",
        "read",
        {"file_path": "/repo/private.txt"},
    ),
]


def configure(runtime, profile, deny, *, conflict=False, cap=None):  # noqa: F811
    sessions, _, llm, _, sid = runtime
    policy = {
        "profile": profile,
        "allow_policies": ["Investigation of this repository is fine"],
        "deny_policies": [deny],
    }
    if cap:
        policy["maximum_outcome"] = cap
    sessions.update_session(sid, user_id="owner", policy=policy)

    def generate(*args, **kwargs):
        props = kwargs["json_schema"]["schema"]["properties"]
        assert "operator_policy" not in props
        answer = json.loads(response(kwargs))
        if "user_would" in props:
            return json.dumps(answer)
        values = {
            "operation": "read",
            "mutation": "read_only",
            "effects": ["read"],
            "consequence": "routine",
            "risk": "low",
        }
        if conflict:
            values.update(
                contextual_restrictions="conflicts",
                restriction_reference="deny_policies.0",
            )
        answer.update({k: v for k, v in values.items() if k in props})
        return json.dumps(answer)

    llm.generate.side_effect = generate
    return runtime


def read(runtime, tool, args, **options):  # noqa: F811
    _, _, _, evaluator, sid = runtime
    return evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool=tool,
        args=args,
        **options,
    )


@pytest.mark.parametrize("profile", ["balanced", "bounded_autonomy"])
@pytest.mark.parametrize("deny,tool,args", READS)
def test_advisory_deny_never_routes_static_read(runtime, profile, deny, tool, args):  # noqa: F811
    configure(runtime, profile, deny)
    result = read(runtime, tool, args)
    runtime[2].generate.assert_not_called()
    assert result["decision"] == "approve"
    metadata = result["evaluation_metadata"]
    assert metadata["policy_result"]["permission_basis"] == "read_only_static"
    assert not metadata["judge_required"]


@pytest.mark.parametrize("deny,tool,args", READS)
def test_strict_l1_conflict_with_advisory_clause_escalates_not_denies(
    runtime,
    deny,
    tool,
    args,  # noqa: F811
):
    configure(runtime, "strict", deny, conflict=True)
    result = read(runtime, tool, args)
    assert runtime[2].generate.call_count == 1
    assert result["decision"] == "escalate"
    metadata = result["evaluation_metadata"]
    assert metadata["policy_evidence"]["assessment"]["restriction_reference"] == (
        "deny_policies.0"
    )
    obligations = {o["id"] for o in metadata["outstanding_obligations"]}
    assert "model.denial" in obligations
    assert not any(item.startswith("operator") for item in obligations)


@pytest.mark.parametrize("profile", PROFILES)
def test_no_advisory_deny_preserves_static_fast_read(runtime, profile):  # noqa: F811
    sessions, _, llm, _, sid = runtime
    sessions.update_session(
        sid,
        user_id="owner",
        policy={
            "profile": profile,
            "allow_policies": ["Read-only investigation is fine"],
        },
    )
    result = read(runtime, "read", {"file_path": "/repo/README.md"})
    assert result["decision"] == "approve"
    # Preserve the profile's existing context checks; bounded reads stay cheap.
    calls = llm.generate.call_count
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})
    llm.generate.reset_mock()
    baseline = read(runtime, "read", {"file_path": "/repo/README.md"})
    assert baseline["decision"] == "approve"
    assert llm.generate.call_count == calls
    if profile == "bounded_autonomy":
        assert calls == 0


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("minimum", [None, "escalate", "deny"])
def test_operator_cap_and_request_floor_on_prohibited_read(runtime, profile, minimum):  # noqa: F811
    configure(runtime, profile, "Do not access customer records", cap="approve")
    result = read(
        runtime,
        "read",
        {"file_path": "/repo/customers.csv"},
        minimum_outcome=minimum,
    )
    # No model is called under YOLO; advisory text adds no obligation to waive.
    expected = minimum or "approve"
    assert result["decision"] == expected
    final = result["evaluation_metadata"]["final_policy_result"]
    assert not any(
        item.startswith("operator") for item in final["waived_obligation_ids"]
    )
    runtime[2].generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_human_resolution_remains_exact_action_bound(runtime, profile, decision):  # noqa: F811
    configure(runtime, profile, "Never open the private secret file")
    initial = read(
        runtime,
        "read",
        {"file_path": "/repo/private.txt"},
        minimum_outcome="escalate",
    )
    assert initial["decision"] == "escalate"
    sessions, audit, _, evaluator, sid = runtime
    await resolve_with_side_effects(
        call_id=initial["call_id"],
        user_decision=decision,
        user_id="owner",
        resolved_by="user",
        audit_store=audit,
        evaluator=evaluator,
    )
    record = audit.get_by_call_id(initial["call_id"], user_id="owner")
    assert record["user_decision"] == decision and record["resolved_by"] == "user"
    if decision == "approve":
        result = read(
            runtime,
            "read",
            {"file_path": "/repo/private.txt"},
            approval_call_id=initial["call_id"],
            minimum_outcome="escalate",
        )
        assert result["decision"] == "approve"
        assert (
            result["evaluation_metadata"]["final_policy_result"]["disposition"]
            == "satisfied_by_human"
        )
        with pytest.raises(ValueError, match="Approval does not match"):
            read(
                runtime,
                "read",
                {"file_path": "/repo/different.txt"},
                approval_call_id=initial["call_id"],
            )


@pytest.mark.parametrize("profile", PROFILES)
def test_read_only_overlay_retains_precedence_over_operator_cap(runtime, profile):  # noqa: F811
    configure(runtime, profile, "Do not access customer records", cap="approve")
    result = read(
        runtime,
        "write",
        {"file_path": "/repo/output.txt", "content": "hello"},
        context={"read_only_required": True},
    )
    assert result["decision"] == "deny"
    assert (
        "request.read_only_required"
        in result["evaluation_metadata"]["final_policy_result"]["blocking_reasons"]
    )
