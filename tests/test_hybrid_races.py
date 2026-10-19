"""The same admission invariants on SQLite and an optional isolated PostgreSQL."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.hybrid import Assessment
from intaris.policy_activation import HybridError
from intaris.session import SessionStore
from tests.evidence_mock import EvidenceMock as Mock


@pytest.fixture(params=["sqlite", "postgresql"])
def race_store(request, tmp_path, monkeypatch):
    monkeypatch.setenv("EVALUATION_POLICY_VERSION", "hybrid_v1")
    url = os.environ.get("TEST_HYBRID_POSTGRES_URL")
    if request.param == "postgresql" and not url:
        pytest.skip("TEST_HYBRID_POSTGRES_URL requires an isolated test database")
    db = Database(
        DBConfig(
            backend=request.param,
            path=str(tmp_path / "race.db"),
            database_url=url or "",
        )
    )
    sessions, audit = SessionStore(db), AuditStore(db)
    session_id = str(uuid.uuid4())
    sessions.create(
        user_id="race-owner", session_id=session_id, intention="Fix development tests"
    )
    llm = Mock()
    llm.generate.return_value = Assessment(
        relationship="supports_intent",
        risk="medium",
        authorization="explicit",
        operation="write",
        effects=["write"],
        environment="development",
    ).model_dump_json()
    evaluator = Evaluator(llm=llm, session_store=sessions, audit_store=audit)
    yield sessions, audit, llm, evaluator, session_id
    db.close()


@pytest.mark.parametrize("mutation", ["policy", "status", "message", "alignment"])
def test_update_before_admission_wins(race_store, mutation):
    sessions, audit, llm, evaluator, session_id = race_store
    entered, release = threading.Event(), threading.Event()
    answer = llm.generate.return_value

    def assess(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return answer

    llm.generate.side_effect = assess
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            evaluator.evaluate,
            user_id="race-owner",
            session_id=session_id,
            agent_id=None,
            tool="write",
            args={"file_path": "/repo/test.py"},
        )
        assert entered.wait(10)
        try:
            if mutation == "policy":
                sessions.update_session(
                    session_id, user_id="race-owner", policy={"deny_tools": ["write"]}
                )
            elif mutation == "status":
                sessions.update_status(session_id, "suspended", user_id="race-owner")
            elif mutation == "message":
                sessions.allocate_event_sequences(
                    session_id,
                    user_id="race-owner",
                    user_message_flags=[True],
                    latest_user_evidence={"content": "Stop writing"},
                )
            else:
                sessions.set_alignment_state(
                    session_id, user_id="race-owner", state="pending"
                )
        finally:
            release.set()
        with pytest.raises(HybridError, match="changed during evaluation"):
            pending.result(timeout=10)
    assert not audit.get_recent(session_id, user_id="race-owner", limit=10)


def test_admission_before_update_is_bounded_but_not_retroactively_revoked(race_store):
    sessions, audit, _, evaluator, session_id = race_store
    result = evaluator.evaluate(
        user_id="race-owner",
        session_id=session_id,
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
    )
    sessions.update_status(session_id, "suspended", user_id="race-owner")
    metadata = audit.mark_execution(
        result["call_id"], user_id="race-owner", state="dispatched"
    )
    assert metadata["execution_state"] == "dispatched"
    with pytest.raises(HybridError):
        audit.mark_execution(
            result["call_id"], user_id="race-owner", state="dispatched"
        )
    assert (
        evaluator.evaluate(
            user_id="race-owner",
            session_id=session_id,
            agent_id=None,
            tool="read",
            args={"file_path": "/repo/test.py"},
        )["decision"]
        == "deny"
    )


def test_late_human_resolution_cannot_approve_changed_session(race_store):
    sessions, audit, _, evaluator, session_id = race_store
    first = evaluator.evaluate(
        user_id="race-owner",
        session_id=session_id,
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
        minimum_outcome="escalate",
    )
    sessions.update_session(
        session_id, user_id="race-owner", policy={"deny_tools": ["read"]}
    )
    with pytest.raises(HybridError, match="changed before resolution"):
        audit.resolve_escalation(first["call_id"], "approve", user_id="race-owner")
    assert (
        audit.get_by_call_id(first["call_id"], user_id="race-owner")["user_decision"]
        is None
    )


def activity(race_store, kind):
    sessions, audit, _, _, session_id = race_store
    if kind == "summary":
        from intaris.analyzer import _store_summary

        _store_summary(
            sessions._db,
            user_id="race-owner",
            session_id=session_id,
            window_start="2026-10-08T10:00:00+00:00",
            window_end="2026-10-08T10:01:00+00:00",
            trigger="manual",
            summary_type="window",
            result={"summary": "Background activity", "intent_alignment": "aligned"},
            stats={},
        )
    else:
        audit.insert(
            call_id=str(uuid.uuid4()),
            user_id="race-owner",
            session_id=session_id,
            agent_id=None,
            tool=None,
            args_redacted={"context": {"role": kind}},
            classification=None,
            evaluation_path="reasoning",
            decision="approve",
            risk=None,
            reasoning=None,
            latency_ms=0,
            record_type="reasoning",
            content="User message: investigate the repository",
        )


def evaluate(race_store, **kwargs):
    return race_store[3].evaluate(
        user_id="race-owner",
        session_id=race_store[4],
        agent_id=None,
        tool="read",
        args={"file_path": "/repo/test.py"},
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["summary", "assistant", "tool", "system"])
def test_activity_during_assessment_does_not_invalidate_admission(race_store, kind):
    sessions, audit, llm, evaluator, session_id = race_store
    entered, release = threading.Event(), threading.Event()
    answer = llm.generate.return_value
    revision = sessions.get(session_id, user_id="race-owner")["state_revision"]

    def assess(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return answer

    llm.generate.side_effect = assess
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            evaluator.evaluate,
            user_id="race-owner",
            session_id=session_id,
            agent_id=None,
            tool="write",
            args={"file_path": "/repo/test.py"},
        )
        assert entered.wait(10)
        try:
            activity(race_store, kind)
        finally:
            release.set()
        result = pending.result(timeout=10)
    assert sessions.get(session_id, user_id="race-owner")["state_revision"] == revision
    assert audit.get_by_call_id(result["call_id"], user_id="race-owner")


@pytest.mark.parametrize("explicit", [False, True])
def test_exact_human_receipt_survives_non_authorization_activity(race_store, explicit):
    sessions, audit, _, _, session_id = race_store
    first = evaluate(race_store, minimum_outcome="escalate")
    # Activity must not stale the pending decision either.
    activity(race_store, "summary")
    audit.resolve_escalation(first["call_id"], "approve", user_id="race-owner")
    revision = sessions.get(session_id, user_id="race-owner")["state_revision"]
    activity(race_store, "summary")
    activity(race_store, "assistant")
    assert sessions.get(session_id, user_id="race-owner")["state_revision"] == revision
    retry = evaluate(
        race_store,
        minimum_outcome="escalate",
        **({"approval_call_id": first["call_id"]} if explicit else {}),
    )
    assert retry["decision"] == "approve"
    assert retry["evaluation_metadata"]["approval_reference"] == first["call_id"]


@pytest.mark.parametrize("change", ["policy", "suspended", "terminated", "user"])
def test_receipt_invalidated_by_authorization_changes(race_store, change):
    sessions, audit, _, _, session_id = race_store
    first = evaluate(race_store, minimum_outcome="escalate")
    audit.resolve_escalation(first["call_id"], "approve", user_id="race-owner")
    if change == "policy":
        sessions.update_session(
            session_id, user_id="race-owner", policy={"deny_tools": ["read"]}
        )
    elif change == "user":
        activity(race_store, "user")
    else:
        sessions.update_status(session_id, change, user_id="race-owner")
        assert (
            evaluate(race_store, approval_call_id=first["call_id"])["decision"]
            == "deny"
        )
        sessions.update_status(session_id, "active", user_id="race-owner")
    with pytest.raises(HybridError) as error:
        evaluate(race_store, approval_call_id=first["call_id"])
    assert error.value.code == "stale_approval"


@pytest.mark.parametrize("minimum,decision", [(None, "approve"), ("deny", "deny")])
def test_yolo_skips_failed_model_and_accepts_concurrent_revision(
    race_store, monkeypatch, minimum, decision
):
    sessions, audit, llm, _, session_id = race_store
    sessions.update_session(
        session_id, user_id="race-owner", policy={"maximum_outcome": "approve"}
    )
    sessions.set_alignment_state(session_id, user_id="race-owner", state="pending")
    llm.generate.side_effect = AssertionError("L1 must not run")
    extractor = Mock()
    extractor.capture.side_effect = AssertionError("Task extraction must not run")
    race_store[3]._task_context = extractor
    insert = audit.insert

    def concurrent_insert(**kwargs):
        activity(race_store, "user") if kwargs.get("admission_snapshot") else None
        return insert(**kwargs)

    monkeypatch.setattr(audit, "insert", concurrent_insert)
    result = evaluate(race_store, minimum_outcome=minimum)
    assert result["decision"] == decision
    llm.generate.assert_not_called()
    extractor.capture.assert_not_called()
    assert (
        audit.get_by_call_id(result["call_id"], user_id="race-owner")["decision"]
        == decision
    )


def test_yolo_human_denial_wins(race_store):
    sessions, audit, _, _, session_id = race_store
    sessions.update_session(
        session_id, user_id="race-owner", policy={"maximum_outcome": "approve"}
    )
    first = evaluate(race_store, minimum_outcome="escalate")
    audit.resolve_escalation(first["call_id"], "deny", user_id="race-owner")
    activity(race_store, "user")
    result = evaluate(race_store)
    assert result["decision"] == "deny"
    assert result["outcome_override"] == "human_resolution"
    assert result["evaluation_metadata"]["admission"] is None


def test_switching_to_yolo_cannot_clear_human_denial(race_store):
    sessions, audit, _, _, session_id = race_store
    first = evaluate(race_store, minimum_outcome="escalate")
    audit.resolve_escalation(first["call_id"], "deny", user_id="race-owner")
    sessions.update_session(
        session_id, user_id="race-owner", policy={"maximum_outcome": "approve"}
    )
    blocked = evaluate(race_store)
    assert blocked["decision"] == "deny"
    audit.resolve_escalation(blocked["call_id"], "approve", user_id="race-owner")
    assert evaluate(race_store)["decision"] == "approve"


@pytest.mark.parametrize("change", ["suspended", "policy"])
def test_yolo_atomic_admission_keeps_lifecycle_and_policy_guards(
    race_store, monkeypatch, change
):
    sessions, audit, _, _, session_id = race_store
    sessions.update_session(
        session_id, user_id="race-owner", policy={"maximum_outcome": "approve"}
    )
    insert = audit.insert

    def concurrent_insert(**kwargs):
        if change == "policy":
            sessions.update_session(
                session_id, user_id="race-owner", policy={"maximum_outcome": "escalate"}
            )
        else:
            sessions.update_status(session_id, change, user_id="race-owner")
        return insert(**kwargs)

    monkeypatch.setattr(audit, "insert", concurrent_insert)
    if change == "policy":
        with pytest.raises(HybridError) as error:
            evaluate(race_store)
        assert error.value.code == "stale_policy_revision"
    else:
        result = evaluate(race_store)
        assert result["decision"] == "deny"
        assert result["evaluation_metadata"]["admission"] is None
        assert (
            audit.get_by_call_id(result["call_id"], user_id="race-owner")["decision"]
            == "deny"
        )


@pytest.mark.parametrize("state", ["pending", "error"])
def test_alignment_unavailable_is_retryable_without_consent_prompt(race_store, state):
    sessions, audit, llm, _, session_id = race_store
    sessions.set_alignment_state(session_id, user_id="race-owner", state=state)
    llm.generate.side_effect = AssertionError("Recovery must not require a model")
    with pytest.raises(HybridError) as error:
        evaluate(race_store)
    assert error.value.code == "alignment_unavailable"
    assert error.value.detail()["retryable"] is True
    assert error.value.detail()["execution_state"] == "not_dispatched"
    assert sessions.get(session_id, user_id="race-owner")["alignment_state"] == state
    llm.generate.assert_not_called()
    # Recovery is a completed review, not invented human authorization.
    sessions.set_alignment_state(session_id, user_id="race-owner", state="ready")
    llm.generate.side_effect = None
    assert evaluate(race_store)["decision"] == "approve"
