"""Owning-boundary acceptance evidence, not compiler semantic accuracy."""

import asyncio
import json
import threading

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig, JudgeConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.hybrid import Assessment
from intaris.judge import JudgeReviewer
from intaris.policy_activation import HybridError, PolicyCompiler
from intaris.session import SessionStore
from tests.evidence_mock import EvidenceMock as Mock
from tests.test_hybrid_races import race_store as _race_store
from tests.test_hybrid_runtime import stores as _stores

stores = _stores
race_store = _race_store


def compiled():
    return json.dumps(
        {
            "supported": True,
            "reason": "",
            "rules": [
                {
                    "id": "deny_policies.0",
                    "effect": "deny",
                    "when": {"op": "tool_matches", "value": "write"},
                }
            ],
        }
    )


def test_compiled_restart_cache_and_unsupported_update(stores, tmp_path, monkeypatch):
    db, sessions, _, llm, _ = stores
    compiler_llm = Mock()
    compiler_llm.generate.return_value = compiled()
    compiler = PolicyCompiler(compiler_llm)
    monkeypatch.setattr("intaris.policy_activation._compiler", compiler)
    policy = {"deny_policies": ["Deny tools matching write"]}
    original = sessions.create(
        user_id="owner", session_id="work", intention="Read tests", policy=policy
    )
    sessions.create(
        user_id="owner", session_id="same", intention="Read tests", policy=policy
    )
    assert compiler_llm.generate.call_count == 1
    from intaris.policy_coverage import source_constraint

    for negation in ("", "not "):
        source = f"Deny deletion of paths {negation}matching /repo/cache/*"
        compiler_llm.generate.return_value = json.dumps(
            {
                "supported": True,
                "reason": "",
                "rules": [
                    {
                        "id": "deny_policies.0",
                        "effect": "deny",
                        "when": source_constraint(source, "deny"),
                    }
                ],
            }
        )
        sessions.create(
            user_id="owner",
            session_id="negative" if negation else "positive",
            intention="Read tests",
            policy={"deny_policies": [source]},
        )
    assert compiler_llm.generate.call_count == 3
    compiler_llm.generate.return_value = json.dumps(
        {"supported": False, "reason": "Unsupported clause", "rules": []}
    )
    sessions.update_session(
        "same",
        user_id="owner",
        policy={"deny_policies": ["Use unspecified runbook"]},
    )
    current = sessions.get("same", user_id="owner")
    assert current["active_policy"]["rules"] == []
    assert (
        current["active_policy"]["advisory_clauses"][0]["text"]
        == "Use unspecified runbook"
    )
    assert current["policy_revision"] == original["policy_revision"] + 1
    assert (
        compiler_llm.generate.call_count == 3
    )  # Unsupported grammar needs no compiler.
    assert (
        sessions.get("work", user_id="owner")["active_policy"]
        == original["active_policy"]
    )
    db.close()
    fresh_compiler = PolicyCompiler(Mock())
    monkeypatch.setattr("intaris.policy_activation._compiler", fresh_compiler)
    monkeypatch.setenv("POLICY_COMPILER_LLM_MODEL", "changed-after-restart")
    reopened = Database(DBConfig(backend="sqlite", path=str(tmp_path / "hybrid.db")))
    try:
        restored = SessionStore(reopened)
        evaluator = Evaluator(
            llm=llm, session_store=restored, audit_store=AuditStore(reopened)
        )
        assert (
            restored.get("work", user_id="owner")["active_policy"]
            == original["active_policy"]
        )
        assert (
            evaluator.evaluate(
                user_id="owner", session_id="work", agent_id=None, tool="write", args={}
            )["decision"]
            == "deny"
        )
        assert not fresh_compiler.llm.generate.called
        assert (
            restored.get("same", user_id="owner")["active_policy"]
            == current["active_policy"]
        )
    finally:
        reopened.close()


def test_policy_change_after_admission(race_store):
    sessions, audit, _, evaluator, sid = race_store
    request = dict(
        user_id="race-owner", session_id=sid, agent_id=None, tool="read", args={}
    )
    admitted = evaluator.evaluate(**request)
    updated = sessions.update_session(
        sid, user_id="race-owner", policy={"deny_tools": ["read"]}
    )
    assert (
        updated["policy_revision"] > admitted["evaluation_metadata"]["policy_revision"]
    )
    assert (
        audit.mark_execution(
            admitted["call_id"], user_id="race-owner", state="dispatched"
        )["execution_state"]
        == "dispatched"
    )
    assert evaluator.evaluate(**request)["decision"] == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["policy", "message"])
async def test_stale_judge_cannot_resolve(race_store, mutation):
    sessions, audit, llm, evaluator, sid = race_store
    llm.generate.return_value = Assessment(
        relationship="supports_intent", risk="high"
    ).model_dump_json()
    result = evaluator.evaluate(
        user_id="race-owner", session_id=sid, agent_id=None, tool="write", args={}
    )
    started, release = threading.Event(), threading.Event()

    def judge_output(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return Assessment(relationship="supports_intent", risk="low").model_dump_json()

    judge_llm = Mock()
    judge_llm.generate.side_effect = judge_output
    judge = JudgeReviewer(
        llm=judge_llm,
        config=JudgeConfig(mode="auto"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    pending = asyncio.create_task(
        judge.review_and_resolve(
            call_id=result["call_id"],
            user_id="race-owner",
            session_id=sid,
            agent_id=None,
        )
    )
    assert await asyncio.to_thread(started.wait, 10)
    try:
        if mutation == "policy":
            sessions.update_session(
                sid, user_id="race-owner", policy={"deny_tools": ["write"]}
            )
        else:
            sessions.allocate_event_sequences(
                sid,
                user_id="race-owner",
                user_message_flags=[True],
                latest_user_evidence={"content": "Do not write"},
            )
    finally:
        release.set()
    await pending  # Fire-and-forget owner catches errors; persistence is the contract.
    record = audit.get_by_call_id(result["call_id"], user_id="race-owner")
    assert record["user_decision"] is None
    assert record["evaluation_metadata"]["admission"] is None


@pytest.mark.parametrize("scope", ["session", "tool"])
def test_approval_cannot_cross_scope(stores, scope):
    _, sessions, audit, _, evaluator = stores
    for sid in ("first", "second"):
        sessions.create(user_id="owner", session_id=sid, intention="Inspect tests")
    request = dict(
        user_id="owner",
        session_id="first",
        agent_id=None,
        tool="read",
        args={},
        minimum_outcome="escalate",
    )
    result = evaluator.evaluate(**request)
    audit.resolve_escalation(result["call_id"], "approve", user_id="owner")
    request["session_id" if scope == "session" else "tool"] = (
        "second" if scope == "session" else "glob"
    )
    with pytest.raises(HybridError, match="Approval does not match"):
        evaluator.evaluate(**request, approval_call_id=result["call_id"])


@pytest.mark.parametrize("stage", ["l1", "judge"])
@pytest.mark.parametrize("cap", [None, "approve", "escalate", "deny"])
@pytest.mark.parametrize("failure", ["json", "schema", "provider", "executor"])
@pytest.mark.asyncio
async def test_model_failure_blocks_unless_operator_skips_assessment(
    stores, stage, cap, failure
):
    _, sessions, audit, llm, evaluator = stores
    policy = {"maximum_outcome": cap} if cap else {}
    if stage == "judge":
        policy["rules"] = [
            {
                "id": "review-write",
                "effect": "require_approval",
                "when": {"op": "tool_matches", "value": "write"},
            }
        ]
    sessions.create(
        user_id="owner", session_id="work", intention="Fix tests", policy=policy
    )
    request = dict(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="write",
        args={},
        controller_context={
            "executor_id": "authorized",
            "governing_execution_id": "run",
            "governing_snapshot_id": "snapshot",
        },
    )
    call = None
    if stage == "judge":
        # A policy floor permits classification review; request floors deliberately
        # skip Judge entirely and would not exercise malformed-output handling.
        call = evaluator.evaluate(**request)
        llm.generate.reset_mock()
    if failure == "provider":
        llm.generate.side_effect = RuntimeError("Provider unavailable")
    else:
        llm.generate.return_value = {
            "json": "{",
            "schema": '{"decision":"approve"}',
            "executor": json.dumps(
                {
                    **Assessment(
                        relationship="supports_intent", risk="low"
                    ).model_dump(),
                    "executor_id": "forged",
                }
            ),
        }[failure]
    if stage == "l1":
        if cap == "approve":
            result = evaluator.evaluate(**request)
            assert result["decision"] == "approve"
            assert result["evaluation_metadata"]["operator_bound"]
            llm.generate.assert_not_called()
            assert audit.get_by_call_id(result["call_id"], user_id="owner")
            return
        with pytest.raises(HybridError):
            evaluator.evaluate(**request)
        assert not audit.get_recent("work", user_id="owner", limit=10)
    else:
        judge = JudgeReviewer(
            llm=llm,
            config=JudgeConfig(mode="auto"),
            audit_store=audit,
            session_store=sessions,
            evaluator=evaluator,
        )
        await judge.review_and_resolve(
            call_id=call["call_id"],
            user_id="owner",
            session_id="work",
            agent_id=None,
        )
        assert llm.generate.called == (cap != "approve")
        record = audit.get_by_call_id(call["call_id"], user_id="owner")
        assert record["user_decision"] is None
        assert record["evaluation_metadata"]["admission"] is None
        assert (
            record["evaluation_metadata"]["controller_context"]["executor_id"]
            == "authorized"
        )


@pytest.mark.parametrize(
    "predicate",
    [
        {"op": "execute_python", "value": "return True"},
        {"op": "contains", "field": "effects", "value": "unregistered_effect"},
    ],
)
def test_unsupported_rules_rejected_at_activation(stores, predicate):
    _, sessions, _, _, _ = stores
    original = sessions.create(
        user_id="owner", session_id="work", intention="Fix tests"
    )
    with pytest.raises(HybridError):
        sessions.update_session(
            "work",
            user_id="owner",
            policy={
                "rules": [
                    {
                        "id": "bad",
                        "effect": "allow",
                        "when": predicate,
                    }
                ]
            },
        )
    assert (
        sessions.get("work", user_id="owner")["active_policy"]
        == original["active_policy"]
    )


def test_recording_keep_gate_retains_new_restriction(stores):
    from intaris.intention import generate_intention

    db, sessions, audit, llm, evaluator = stores
    original = sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write development tests",
        policy={"intention_gate": "jev", "allow_tools": ["write"]},
    )
    audit.insert(
        call_id="new-user-restriction",
        user_id="owner",
        session_id="work",
        agent_id=None,
        record_type="reasoning",
        content="User message: Do not write any files now.",
        tool=None,
        args_redacted=None,
        classification=None,
        evaluation_path="reasoning",
        decision="approve",
        risk=None,
        reasoning=None,
        latency_ms=0,
    )
    gate = Mock()
    gate.intention_gate.return_value = "keep"
    generate_intention(
        user_id="owner",
        session_id="work",
        llm=llm,
        session_store=sessions,
        db=db,
        intention_source="user",
        jev=gate,
    )
    assert gate.intention_gate.call_count == 1
    assert not llm.generate.called
    assert sessions.get("work", user_id="owner")["intention"] == original["intention"]
    llm.generate.return_value = Assessment(
        relationship="conflicts_with_intent", risk="low"
    ).model_dump_json()
    result = evaluator.evaluate(
        user_id="owner", session_id="work", agent_id=None, tool="write", args={}
    )
    assert result["decision"] == "escalate"
    assert any(
        o["id"] == "model.denial"
        for o in result["evaluation_metadata"]["outstanding_obligations"]
    )
    assert "Do not write any files now." not in str(llm.generate.call_args)
    assert (
        "Do not write any files now."
        in audit.get_by_call_id("new-user-restriction", user_id="owner")["content"]
    )


@pytest.mark.parametrize("cap", [None, "approve", "escalate", "deny"])
@pytest.mark.parametrize("failure", ["malformed", "provider"])
def test_jev_failure_blocks_unless_operator_skips_assessment(
    stores, monkeypatch, cap, failure
):
    from intaris.config import JevConfig
    from intaris.jev import JevClient

    _, sessions, audit, llm, _ = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Fix tests",
        policy={"maximum_outcome": cap} if cap else {},
    )
    jev = JevClient(JevConfig(api_key="test-key"))
    transport = Mock(return_value={"answers": {}})
    if failure == "provider":
        transport.side_effect = RuntimeError("Provider unavailable")
    monkeypatch.setattr(jev, "_request", transport)
    evaluator = Evaluator(llm=llm, jev=jev, session_store=sessions, audit_store=audit)
    try:
        if cap == "approve":
            result = evaluator.evaluate(
                user_id="owner", session_id="work", agent_id=None, tool="write", args={}
            )
            assert result["decision"] == "approve"
            transport.assert_not_called()
            assert audit.get_by_call_id(result["call_id"], user_id="owner")
        elif failure == "malformed":
            result = evaluator.evaluate(
                user_id="owner", session_id="work", agent_id=None, tool="write", args={}
            )
            assert result["decision"] == "escalate"
            assert result["outcome_override"] == "assessment.invalid_response"
            assert result["evaluation_metadata"]["admission"] is None
            assert "validation failed" in result["reasoning"]
            assert audit.get_recent("work", user_id="owner", limit=10)
        else:
            with pytest.raises(HybridError) as result:
                evaluator.evaluate(
                    user_id="owner",
                    session_id="work",
                    agent_id=None,
                    tool="write",
                    args={},
                )
            assert result.value.status == 503
            assert not audit.get_recent("work", user_id="owner", limit=10)
        assert not llm.generate.called
    finally:
        jev.close()


@pytest.mark.parametrize(
    "effects,decision",
    [
        (["unknown"], "escalate"),
        (["write"], "approve"),
        (["delete"], "deny"),
    ],
)
def test_effect_membership_unknown_does_not_establish_permit(stores, effects, decision):
    _, sessions, _, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write development tests",
        policy={
            "rules": [
                {
                    "id": "permit",
                    "effect": "allow",
                    "when": {"op": "tool_matches", "value": "opaque"},
                },
                {
                    "id": "protect",
                    "effect": "deny",
                    "when": {"op": "contains", "field": "effects", "value": "delete"},
                },
            ],
        },
    )
    llm.generate.return_value = Assessment(
        relationship="supports_intent",
        risk="low",
        effects=effects,
    ).model_dump_json()
    result = evaluator.evaluate(
        user_id="owner", session_id="work", agent_id=None, tool="opaque", args={}
    )
    assert llm.generate.call_count == 1
    assert result["decision"] == decision
    if decision != "approve":
        assert result["evaluation_metadata"]["admission"] is None
