"""Explicit read-only restrictions are independent of the caller's chat mode."""

import json

import pytest

from intaris.config import JudgeConfig
from intaris.hybrid import Assessment
from intaris.judge import JudgeReviewer
from intaris.policy_activation import HybridError
from tests.evidence_mock import EvidenceMock as Mock
from tests.test_hybrid_runtime import stores as _stores

stores = _stores


def answer(mutation):
    return Assessment(
        mutation=mutation,
        operation="read" if mutation == "read_only" else "write",
        effects=["execute"],
        relationship="supports_intent",
        risk="low",
        authorization="implicit",
        authorization_source="session_intention",
        authorization_scope="within_scope",
        environment="development",
        side_effect_scope="bounded",
    ).model_dump_json()


@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "tool,args,mutation",
    [
        (
            "bash",
            {
                "command": "python -c \"from pathlib import Path; print(Path('notes').read_text())\""
            },
            "read_only",
        ),
        (
            "bash",
            {
                "command": "python -c \"from pathlib import Path; Path('notes').write_text('changed')\""
            },
            "mutating",
        ),
        ("bash", {"command": "printf changed > notes"}, "mutating"),
        ("apply_patch", {"patch": "*** Delete File: notes"}, "mutating"),
        ("service:list", {"filter": "active"}, "read_only"),
        ("service:update", {"value": "changed"}, "mutating"),
    ],
)
def test_explicit_read_only_effects_not_capabilities(
    stores, version, profile, tool, args, mutation
):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Investigate and fix local notes",
        policy={
            "evaluation_policy_version": version,
            "profile": profile,
            "maximum_outcome": "approve",
            "allow_tools": [tool],
        },
    )
    llm.generate.return_value = answer(mutation)
    request = dict(
        user_id="owner", session_id="work", agent_id=None, tool=tool, args=args
    )
    plan = evaluator.evaluate(
        **request, context={"chat_mode": "plan", "read_only_required": True}
    )
    # YOLO skips model proof; opaque shell/service calls conservatively
    # deny under an explicit ceiling rather than offering impossible approval.
    opaque = tool == "service:list" or "python -c" in args.get("command", "")
    expected = (
        "deny"
        if version == "hybrid_v1" and opaque
        else "approve"
        if mutation == "read_only"
        else "deny"
    )
    assert plan["decision"] == expected
    assert plan["evaluation_metadata"]["overlay"]["read_only_required"]
    if mutation == "mutating":
        with pytest.raises(HybridError, match="read-only"):
            audit.resolve_escalation(plan["call_id"], "approve", user_id="owner")
    build = evaluator.evaluate(**request, context={"chat_mode": "build"})
    assert build["decision"] == "approve"
    assert evaluator.evaluate(**request)["decision"] == "approve"
    if version == "hybrid_v1":
        llm.generate.assert_not_called()


@pytest.mark.parametrize(
    "context",
    [
        {"chat_mode": "unknown"},
        {"chat_mode": []},
        {"read_only_required": "false"},
        {"read_only_required": 0},
        {"chat_mode_source": False},
    ],
)
def test_malformed_control_fields_never_grant(stores, context):
    _, sessions, _, _, evaluator = stores
    sessions.create(user_id="owner", session_id="work", intention="Read files")
    with pytest.raises(HybridError) as failed:
        evaluator.evaluate(
            user_id="owner",
            session_id="work",
            agent_id=None,
            tool="read",
            args={},
            context=context,
        )
    assert failed.value.status == 422


def test_session_read_only_cannot_be_relaxed_by_request(stores):
    _, sessions, _, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect files",
        policy={"read_only_required": True, "maximum_outcome": "approve"},
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={},
        context={"chat_mode": "build", "read_only_required": False},
    )
    assert result["decision"] == "deny"


def test_build_human_approval_cannot_be_borrowed_in_plan(stores):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Edit notes",
        policy={
            "rules": [
                {
                    "id": "review",
                    "effect": "require_approval",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ]
        },
    )
    llm.generate.return_value = answer("mutating")
    request = dict(
        user_id="owner", session_id="work", agent_id=None, tool="write", args={}
    )
    build = evaluator.evaluate(**request, context={"chat_mode": "build"})
    audit.resolve_escalation(build["call_id"], "approve", user_id="owner")
    with pytest.raises(HybridError, match="Approval"):
        evaluator.evaluate(
            **request, context={"chat_mode": "plan"}, approval_call_id=build["call_id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected",
    [("read_only", None), ("mutating", None), ("unclear", None)],
)
async def test_judge_cannot_invent_effect_witnesses_or_override_ceiling(
    stores, mutation, expected
):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect local configuration",
        policy={"maximum_outcome": "approve"},
    )
    llm.generate.return_value = answer("unclear")
    pending = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="service:query",
        args={},
        context={"read_only_required": True},
    )
    assert pending["decision"] == "deny"
    llm.generate.return_value = answer(mutation)
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=pending["call_id"], user_id="owner", session_id="work", agent_id=None
    )
    record = audit.get_by_call_id(pending["call_id"], user_id="owner")
    assert record["user_decision"] == expected


@pytest.mark.asyncio
async def test_known_mutation_survives_forged_judge_read_label(stores):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect files",
        policy={"maximum_outcome": "approve"},
    )
    denied = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="apply_patch",
        args={},
        context={"chat_mode": "plan", "read_only_required": True},
    )
    llm.generate.return_value = answer("read_only")
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="auto"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=denied["call_id"], user_id="owner", session_id="work", agent_id=None
    )
    record = audit.get_by_call_id(denied["call_id"], user_id="owner")
    assert record.get("user_decision") != "approve"


@pytest.mark.asyncio
async def test_late_build_judge_result_never_supplies_plan_evidence(stores):
    import asyncio
    import threading

    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner", session_id="work", intention="Maintain local service"
    )
    sessions.allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": "Maintain the local service."},
    )
    request = dict(
        user_id="owner", session_id="work", agent_id=None, tool="service:run", args={}
    )
    uncertain = json.loads(answer("unclear"))
    uncertain["risk"] = "high"
    llm.generate.return_value = json.dumps(uncertain)
    build = evaluator.evaluate(**request, context={"chat_mode": "build"})
    assert build["decision"] == "escalate"
    entered, release = threading.Event(), threading.Event()
    judge_llm = Mock()

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        from intaris.judge_simulator import Answer

        return Answer(
            user_would="approve",
            risk="low",
            confidence="high",
            basis="explicit_request",
            cites=[1],
            question="",
            action_summary="Edit the authorized file in build mode",
            explanation="This edit is covered by the original implementation request.",
            restriction_quote="",
        ).model_dump_json()

    judge_llm.generate.side_effect = delayed
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(judge_llm)
    judge = JudgeReviewer(
        llm=judge_llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    task = asyncio.create_task(
        judge.review_and_resolve(
            call_id=build["call_id"], user_id="owner", session_id="work"
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        llm.generate.return_value = answer("mutating")
        assert (
            evaluator.evaluate(
                **request, context={"chat_mode": "plan", "read_only_required": True}
            )["decision"]
            == "deny"
        )
    finally:
        release.set()
    await task
    assert (
        audit.get_by_call_id(build["call_id"], user_id="owner")["user_decision"]
        == "approve"
    )
    assert (
        evaluator.evaluate(
            **request, context={"chat_mode": "plan", "read_only_required": True}
        )["decision"]
        == "deny"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected",
    [("read_only", None), ("mutating", None), ("unclear", None)],
)
async def test_legacy_judge_preserves_read_only_ceiling(stores, mutation, expected):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect local configuration",
        policy={
            "evaluation_policy_version": "legacy",
            "maximum_outcome": "approve",
            "allow_tools": ["service:query"],
        },
    )
    llm.generate.return_value = answer("unclear")
    pending = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="service:query",
        args={},
        context={"chat_mode": "plan", "read_only_required": True},
    )
    assert pending["decision"] == "deny"
    llm.generate.side_effect = [
        answer(mutation),
        json.dumps(
            {
                "decision": "approve",
                "confidence": "high",
                "risk": "low",
                "reasoning": "Read-only diagnostic",
            }
        ),
    ]
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=pending["call_id"], user_id="owner", session_id="work"
    )
    assert (
        audit.get_by_call_id(pending["call_id"], user_id="owner")["user_decision"]
        == expected
    )


@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
@pytest.mark.parametrize(
    "tool,args,mutation,decision",
    [
        ("bash", {"command": "env touch notes"}, "mutating", "deny"),
        ("mnemory:delete_all_memories", {}, "mutating", "deny"),
        ("bash", {"command": "ls 2>&1"}, "read_only", "approve"),
        ("bash", {"command": "ls > notes"}, "mutating", "deny"),
    ],
)
def test_no_legacy_exemption_is_mutation_proof(
    stores, version, tool, args, mutation, decision
):
    from intaris.read_only import observe

    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Inspect notes",
        policy={"evaluation_policy_version": version, "maximum_outcome": "approve"},
    )
    llm.generate.return_value = answer(mutation)
    if version == "legacy":
        llm.generate.side_effect = lambda *a, **kw: (
            answer(mutation)
            if (kw.get("json_schema") or {}).get("name") == "hybrid_classification"
            else json.dumps(
                {
                    "aligned": True,
                    "decision": "approve",
                    "risk": "low",
                    "reasoning": "Legacy assessment",
                }
            )
        )
    # Merging stderr into stdout writes nothing; the static path proves it.
    static_read = args.get("command") == "ls 2>&1"
    if static_read:
        assert observe(tool, args) == "read_only"
    elif args.get("command") != "ls > notes":
        assert observe(tool, args) == "unclear"
    result = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool=tool,
        args=args,
        context={"chat_mode": "plan", "read_only_required": True},
    )
    if (
        version == "hybrid_v1"
        and tool == "bash"
        and not static_read
        and args.get("command") != "ls > notes"
    ):
        assert result["decision"] == "deny"
        llm.generate.assert_not_called()
    else:
        assert result["decision"] == decision


@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
@pytest.mark.parametrize(
    "tool,mutation", [("write", "mutating"), ("service:diagnose", "unclear")]
)
def test_alignment_escalation_cannot_bypass_explicit_read_only_ceiling(
    stores, version, tool, mutation
):
    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="parent",
        intention="Read notes",
        policy={"evaluation_policy_version": version},
    )
    sessions.create(
        user_id="owner",
        session_id="child",
        intention="Change service",
        parent_session_id="parent",
        policy={"evaluation_policy_version": version},
    )
    if version == "hybrid_v1":
        sessions.set_alignment_state("child", user_id="owner", state="misaligned")
    else:
        barrier = Mock()
        barrier.is_misaligned.return_value = "Child is unrelated"
        evaluator._alignment_barrier = barrier
    response = json.loads(answer(mutation))
    if mutation == "unclear":
        # Low-risk execute with no unknown effects proves read-only in the
        # current evidence model; this case exercises genuinely unproven effects.
        response["effects"] = ["unknown"]
    llm.generate.return_value = json.dumps(response)
    result = evaluator.evaluate(
        user_id="owner",
        session_id="child",
        agent_id=None,
        tool=tool,
        args={},
        context={"chat_mode": "plan", "read_only_required": True},
    )
    assert result["decision"] == "deny"
    assert result["evaluation_metadata"]["overlay"]["read_only_required"]
    with pytest.raises(HybridError, match="read-only"):
        audit.resolve_escalation(result["call_id"], "approve", user_id="owner")
