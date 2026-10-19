"""Admission transport and durable diagnostics; no model-accuracy claims."""

import json
import sqlite3
from unittest.mock import MagicMock

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


def stores(tmp_path):
    db = Database(DBConfig(path=str(tmp_path / "admission.db")))
    store = SessionStore(db)
    policy = {"evaluation_policy_version": "hybrid_v1", "interaction_mode": "none"}
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


def test_durable_conflict_reason_survives_worker_restart(tmp_path):
    db, store = stores(tmp_path)
    assert store.set_alignment_state(
        "admission-child",
        user_id="admission-owner",
        state="misaligned",
        reason="Original instruction explicitly forbids delegation.",
    )
    # No in-memory barrier: emulate another worker reading the durable result.
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
    assert result["decision"] == "deny"
    assert result["reasoning"] == "Original instruction explicitly forbids delegation."


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
