"""Admission transport and durable diagnostics; no model-accuracy claims."""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from intaris.alignment import check_intention_alignment
from intaris.audit import AuditStore
from intaris.config import DBConfig
from intaris.db import Database
from intaris.evaluator import Evaluator
from intaris.session import SessionStore
from tests.test_jev import _client


def test_first_classifier_receives_original_evidence_on_real_wire():
    client, requests = _client(
        {
            "model": "jev-1.13.0",
            "answers": {"aligned": {"type": "noul", "noul": 0.95}},
        }
    )
    reviewer = MagicMock()
    evidence = {
        "parent_assignment": "Audit read-only. Do not delegate.",
        "child_assignment": "Debrief; skip prohibited persistence.",
        "parent_user_updates": {"content": "Keep the no-delegation restriction."},
        "child_user_updates": {"content": "This assignment is not human consent."},
    }
    try:
        aligned, _ = check_intention_alignment(
            jev=client,
            llm=reviewer,
            parent_intention="Audit",
            child_intention="Debrief",
            **{
                "parent_assignment": evidence["parent_assignment"],
                "child_assignment": evidence["child_assignment"],
                "parent_user_evidence": evidence["parent_user_updates"],
                "child_user_evidence": evidence["child_user_updates"],
            },
        )
    finally:
        client.close()
    assert aligned is True
    reviewer.generate.assert_not_called()
    payload = json.loads(requests[0].content)
    assert payload["state"]["source_evidence"] == evidence
    assert "not human authorization" in payload["state"]["evaluation_rules"]


def stores(tmp_path, profile="balanced"):
    db = Database(DBConfig(path=str(tmp_path / "admission.db")))
    store = SessionStore(db)
    policy = {
        "evaluation_policy_version": "hybrid_v1",
        "interaction_mode": "none",
        "profile": profile,
    }
    store.create(
        user_id="admission-owner",
        session_id="admission-parent",
        intention="Inspect repository read-only",
        policy=policy,
    )
    store.create(
        user_id="admission-owner",
        session_id="admission-child",
        intention="Summarize findings",
        parent_session_id="admission-parent",
        policy=policy,
    )
    return db, store


@pytest.mark.parametrize("profile", ["balanced", "bounded_autonomy"])
def test_durable_conflict_reason_reaches_policy_after_worker_restart(tmp_path, profile):
    db, store = stores(tmp_path, profile)
    assert store.set_alignment_state(
        "admission-child",
        user_id="admission-owner",
        state="misaligned",
        reason="Original instruction explicitly forbids delegation.",
    )
    # No in-memory barrier: emulate another worker reading the durable result.
    with patch(
        "intaris.hybrid_runtime.HybridRuntime.evaluate",
        return_value={
            "decision": "approve",
            "risk": "low",
            "reasoning": "Policy result",
        },
    ) as runtime:
        result = Evaluator(
            llm=MagicMock(),
            session_store=SessionStore(db),
            audit_store=AuditStore(db),
        ).evaluate(
            user_id="admission-owner",
            session_id="admission-child",
            agent_id=None,
            tool="read",
            args={"file_path": "/repo/findings.md"},
        )
    assert result["decision"] == "approve"
    fact = runtime.call_args.kwargs["parent_alignment"]
    assert fact["state"] == "incompatible"
    assert fact["reason"] == "Original instruction explicitly forbids delegation."
    assert fact["grounded"] is False  # prose alone is not an original citation


@pytest.mark.parametrize("state", ["pending", "error"])
def test_bounded_autonomy_does_not_wait_for_alignment_opinion(tmp_path, state):
    db, store = stores(tmp_path, "bounded_autonomy")
    store.set_alignment_state(
        "admission-child",
        user_id="admission-owner",
        state=state,
        reason="Compatibility classifier unavailable.",
    )
    with patch(
        "intaris.hybrid_runtime.HybridRuntime.evaluate",
        return_value={
            "decision": "approve",
            "risk": "low",
            "reasoning": "Policy result",
        },
    ) as runtime:
        Evaluator(
            llm=MagicMock(),
            session_store=store,
            audit_store=AuditStore(db),
        ).evaluate(
            user_id="admission-owner",
            session_id="admission-child",
            agent_id=None,
            tool="read",
            args={"file_path": "/repo/findings.md"},
        )
    fact = runtime.call_args.kwargs["parent_alignment"]
    assert fact["state"] == "unavailable"


@pytest.mark.parametrize(
    "profile,waits", [("balanced", True), ("bounded_autonomy", False)]
)
def test_actual_http_handler_alignment_wait_uses_profile(tmp_path, profile, waits):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from intaris.api.deps import SessionContext
    from intaris.api.evaluate import _evaluate
    from intaris.api.schemas import EvaluateRequest

    db, store = stores(tmp_path, profile)
    evaluator = Evaluator(
        llm=MagicMock(), session_store=store, audit_store=AuditStore(db)
    )
    barrier = SimpleNamespace(wait=AsyncMock())
    http = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(alignment_barrier=barrier))
    )
    request = EvaluateRequest(
        session_id="admission-child",
        tool="read",
        args={"file_path": "/repo/findings.md"},
    )
    ctx = SessionContext(user_id="admission-owner")
    with (
        patch("intaris.server._get_evaluator", return_value=evaluator),
        patch.object(
            evaluator,
            "evaluate_async",
            new=AsyncMock(side_effect=RuntimeError("dispatch-reached")),
        ) as dispatch,
        pytest.raises(
            HTTPException, match="Internal error during evaluation"
        ) as failure,
    ):
        asyncio.run(_evaluate(request, http, ctx))
    assert failure.value.status_code == 500
    dispatch.assert_awaited_once()
    assert barrier.wait.await_count == int(waits)


def test_stale_result_cannot_replace_reason_or_another_tenant(tmp_path):
    _, store = stores(tmp_path)
    before = store.get("admission-child", user_id="admission-owner")
    store.set_alignment_state(
        "admission-child",
        user_id="admission-owner",
        state="error",
        reason="Reviewer timeout.",
    )
    assert not store.set_alignment_state(
        "admission-child",
        user_id="admission-owner",
        state="ready",
        reason="Stale pass",
        expected_state_revision=before["state_revision"],
    )
    assert not store.set_alignment_state(
        "admission-child",
        user_id="other-tenant",
        state="ready",
    )
    assert (
        store.get("admission-child", user_id="admission-owner")["alignment_reason"]
        == "Reviewer timeout."
    )
    store.set_alignment_state(
        "admission-child", user_id="admission-owner", state="pending"
    )
    assert (
        store.get("admission-child", user_id="admission-owner")["alignment_reason"]
        is None
    )


def test_alignment_reason_migration_is_additive_and_idempotent(tmp_path):
    from intaris.db import _SCHEMA_SQL_SQLITE

    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA_SQL_SQLITE)
        conn.execute(
            "INSERT INTO sessions (user_id, session_id, intention, created_at, updated_at) "
            "VALUES ('migration-owner', 'migration-session', 'Keep original task', '2026-10-18', '2026-10-18')"
        )
    for _ in range(2):
        store = SessionStore(Database(DBConfig(path=path)))
        session = store.get("migration-session", user_id="migration-owner")
        assert session["intention"] == "Keep original task"
        assert session["alignment_reason"] is None


def test_postgresql_alignment_reason_migration_is_additive():
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None
    db = Database.__new__(Database)
    db._migrate_pg(conn)
    statements = [
        call.args[0] for call in conn.cursor.return_value.execute.call_args_list
    ]
    assert (
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS alignment_reason TEXT"
        in statements
    )
