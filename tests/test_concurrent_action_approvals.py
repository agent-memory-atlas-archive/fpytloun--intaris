"""Action-local receipts must not erase authorization freshness boundaries."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.policy_activation import HybridError
from intaris.session import SessionStore
from intaris.task_context import TaskContext, decision_watermark
from tests.test_policy_evidence_runtime import response


@pytest.fixture
def runtime(tmp_path):
    db = Database(DBConfig(path=str(tmp_path / "state.db")))
    sessions, audit = SessionStore(db), AuditStore(db)
    sessions.create(
        user_id="owner", session_id="parallel", intention="Implement the local parser"
    )
    llm = Mock()
    llm.generate.side_effect = lambda *a, **kw: response(kw, high=True)
    context = TaskContext(
        llm=llm,
        sessions=sessions,
        audit=audit,
        extractor_identity={"model": "test-extractor"},
        generation="single",
    )
    evaluator = Evaluator(
        llm=llm,
        session_store=sessions,
        audit_store=audit,
        task_context=context,
    )
    yield db, sessions, audit, evaluator
    db.close()


def evaluate(evaluator, index, **options):
    return evaluator.evaluate(
        user_id="owner",
        session_id="parallel",
        agent_id=None,
        tool="write",
        args={"file_path": f"/repo/parser-{index}.py"},
        **options,
    )


def test_parallel_no_note_approvals_keep_each_exact_receipt_current(runtime):
    db, sessions, audit, evaluator = runtime
    calls = [evaluate(evaluator, i) for i in range(2)]
    assert all(call["decision"] == "escalate" for call in calls)
    revision = sessions.get("parallel", user_id="owner")["state_revision"]
    with db.cursor() as cur:
        watermark = decision_watermark(cur, "owner", "parallel")
    for call in calls:
        audit.resolve_escalation(call["call_id"], "approve", None, user_id="owner")
    assert sessions.get("parallel", user_id="owner")["state_revision"] == revision
    assert audit.get_user_decisions("parallel", user_id="owner") == []
    with db.cursor() as cur:
        assert decision_watermark(cur, "owner", "parallel") == watermark
        cur.execute(
            "SELECT resolution_scope FROM audit_log WHERE user_decision = 'approve'"
        )
        assert [row["resolution_scope"] for row in cur.fetchall()] == [
            "action",
            "action",
        ]
    for i, call in enumerate(calls):
        assert (
            evaluate(evaluator, i, approval_call_id=call["call_id"])["decision"]
            == "approve"
        )
    with pytest.raises(HybridError, match="Approval"):
        evaluate(evaluator, 7, approval_call_id=calls[0]["call_id"])


def test_identical_executor_policy_refresh_does_not_expire_exact_approval(runtime):
    _, sessions, audit, evaluator = runtime
    call = evaluate(evaluator, 0)
    audit.resolve_escalation(call["call_id"], "approve", user_id="owner")
    before = sessions.get("parallel", user_id="owner")
    sessions.update_session(
        "parallel",
        user_id="owner",
        policy=before["policy"],
        details=before["details"],
    )
    after = sessions.get("parallel", user_id="owner")
    assert after["state_revision"] == before["state_revision"]
    assert after["policy_revision"] == before["policy_revision"]
    assert (
        evaluate(evaluator, 0, approval_call_id=call["call_id"])["decision"]
        == "approve"
    )
    # A genuine target/scope change must continue to expire the receipt.
    sessions.update_session(
        "parallel", user_id="owner", details={"working_directory": "/different-repo"}
    )
    with pytest.raises(HybridError, match="Approval"):
        evaluate(evaluator, 0, approval_call_id=call["call_id"])


def test_human_approval_after_judge_deferral_is_still_action_local(runtime):
    db, sessions, audit, evaluator = runtime
    calls = [evaluate(evaluator, i) for i in range(2)]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE audit_log SET resolved_by = 'judge', user_decision = 'escalate', "
            "judge_decision = 'defer' WHERE call_id = ?",
            (calls[0]["call_id"],),
        )
        watermark = decision_watermark(cur, "owner", "parallel")
    revision = sessions.get("parallel", user_id="owner")["state_revision"]
    for call in calls:
        audit.resolve_escalation(call["call_id"], "approve", None, user_id="owner")
    assert sessions.get("parallel", user_id="owner")["state_revision"] == revision
    with db.cursor() as cur:
        assert decision_watermark(cur, "owner", "parallel") == watermark
    for i, call in enumerate(calls):
        assert (
            evaluate(evaluator, i, approval_call_id=call["call_id"])["decision"]
            == "approve"
        )


def test_simultaneous_exact_approvals_preserve_both_admissions(runtime):
    _, sessions, audit, evaluator = runtime
    calls = [evaluate(evaluator, i) for i in range(2)]
    revision = sessions.get("parallel", user_id="owner")["state_revision"]

    def approve(call):
        audit.resolve_escalation(call["call_id"], "approve", None, user_id="owner")

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(approve, calls))
    assert sessions.get("parallel", user_id="owner")["state_revision"] == revision
    for i, call in enumerate(calls):
        assert (
            evaluate(evaluator, i, approval_call_id=call["call_id"])["decision"]
            == "approve"
        )


@pytest.mark.parametrize(
    "decision,note", [("deny", None), ("approve", "Do not modify other files.")]
)
def test_scope_relevant_decisions_still_invalidate_siblings(runtime, decision, note):
    db, sessions, audit, evaluator = runtime
    calls = [evaluate(evaluator, i) for i in range(2)]
    revision = sessions.get("parallel", user_id="owner")["state_revision"]
    with db.cursor() as cur:
        watermark = decision_watermark(cur, "owner", "parallel")
    audit.resolve_escalation(calls[0]["call_id"], decision, note, user_id="owner")
    assert sessions.get("parallel", user_id="owner")["state_revision"] == revision + 1
    assert len(audit.get_user_decisions("parallel", user_id="owner")) == 1
    with db.cursor() as cur:
        assert decision_watermark(cur, "owner", "parallel") != watermark
    with pytest.raises(HybridError) as error:
        audit.resolve_escalation(calls[1]["call_id"], "approve", None, user_id="owner")
    assert error.value.code == "stale_resolution"


@pytest.mark.parametrize("change", ["intention", "policy", "status"])
def test_real_scope_or_lifecycle_changes_still_reject_receipt(runtime, change):
    _, sessions, audit, evaluator = runtime
    call = evaluate(evaluator, 0)
    audit.resolve_escalation(call["call_id"], "approve", None, user_id="owner")
    if change == "status":
        sessions.update_status("parallel", "suspended", user_id="owner")
    else:
        sessions.update_session(
            "parallel",
            user_id="owner",
            **(
                {"intention": "Read only, do not modify files."}
                if change == "intention"
                else {"policy": {"profile": "strict"}}
            ),
        )
    if change == "status":
        assert (
            evaluate(evaluator, 0, approval_call_id=call["call_id"])["decision"]
            != "approve"
        )
    else:
        with pytest.raises(HybridError):
            evaluate(evaluator, 0, approval_call_id=call["call_id"])
