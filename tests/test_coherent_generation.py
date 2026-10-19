"""Mechanical v5 evidence/lifecycle tests, not semantic acceptance."""

import json
from unittest.mock import Mock

import pytest

from intaris.task_context import TaskContext, render
from tests.test_policy_evidence_runtime import runtime as _runtime
from tests.test_policy_evidence_table import evidence
from tests.test_task_context import append_reasoning

runtime = _runtime


def owner(runtime, answer):
    sessions, audit, llm, evaluator, sid = runtime
    model = Mock()
    model.generate.side_effect = lambda messages, **kw: json.dumps(
        answer(json.loads(messages[1]["content"])["sources"])
    )
    return TaskContext(
        llm=model,
        sessions=sessions,
        audit=audit,
        extractor_identity={"model": "offline"},
        generation="coherent",
    ), model


def context_echo(sources):
    return {
        "current_request": {"status": "unknown", "item_indices": []},
        "items": [
            {
                "kind": "context",
                "horizon": "short",
                "explicitness": "uncertain",
                "source_ref": row["ref"],
                "quote": row["content"],
                "temporal_status": "uncertain",
                "polarity": "unknown",
                "reference_resolution": "missing",
                "relation": "uncertain",
                "related_source_refs": [],
            }
            for row in sources
            if row["content"] and len(row["content"]) < 800
        ][:24],
    }


def test_immutable_original_and_generated_summary_not_generation_authority(runtime):
    sessions, audit, llm, evaluator, sid = runtime
    initial = sessions.get(sid, user_id="owner")
    assert initial["original_assignment"] == initial["intention"]
    sessions.update_generated_intention(
        sid,
        user_id="owner",
        intention="Proceed confirmed INVENTED_GOAL",
        intention_source="user",
        title=None,
    )
    context, model = owner(runtime, context_echo)
    session = sessions.get(sid, user_id="owner")
    assert session["original_assignment"] == initial["intention"]
    artifact = context.capture(session)
    assert artifact["status"] == "ready"
    assert artifact["current_request"] == {"status": "unknown", "item_indices": []}
    assert artifact["compact_intention"] == ""
    assert "INVENTED_GOAL" not in str(model.generate.call_args)
    assert "INVENTED_GOAL" not in render(artifact, evidence())
    assert "INVENTED_GOAL" in render(artifact, evidence(), judge=True)
    assert any(row["ref"] == "original_assignment" for row in artifact["originals"])
    assert (
        sessions.get(sid, user_id="owner")["original_assignment"]
        == initial["intention"]
    )
    sessions.update_session(sid, user_id="owner", intention="Explicit manual task")
    assert (
        sessions.get(sid, user_id="owner")["original_assignment"]
        == initial["intention"]
    )


def test_older_missing_original_is_not_backfilled_from_summary(runtime):
    sessions, audit, llm, evaluator, sid = runtime
    with audit._db.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET original_assignment=NULL WHERE user_id=? AND session_id=?",
            ("owner", sid),
        )
    sessions.update_generated_intention(
        sid,
        user_id="owner",
        intention="Invented test-and-publish task",
        intention_source="bootstrap",
        title=None,
    )
    context, model = owner(runtime, context_echo)
    artifact = context.capture(sessions.get(sid, user_id="owner"))
    assert "original_assignment_unavailable" in artifact["coverage"]["missing"]
    assert artifact["status"] == "unavailable"
    assert artifact["current_request"]["status"] == "unknown"
    assert not model.generate.called


@pytest.mark.parametrize("resolution", ["direct", "ambiguous", "missing"])
def test_bare_yes_does_not_become_explicit_unscoped_approval(runtime, resolution):
    sessions, audit, llm, evaluator, sid = runtime
    append_reasoning(
        audit, user_id="owner", session_id=sid, content="User message: yes"
    )

    def answer(sources):
        row = next(row for row in sources if row["ref"] == "latest_user_message")
        item = context_echo([row])["items"][0]
        item.update(
            kind="approval", explicitness="explicit", reference_resolution=resolution
        )
        return {
            "current_request": {"status": "unknown", "item_indices": []},
            "items": [item],
        }

    context, _ = owner(runtime, answer)
    artifact = context.capture(sessions.get(sid, user_id="owner"))
    assert artifact["status"] == "unavailable"
    assert artifact["current_request"]["status"] == "unknown"


def test_context_only_cannot_become_supported_current_request(runtime):
    sessions, audit, llm, evaluator, sid = runtime

    def answer(sources):
        result = context_echo(sources)
        result["current_request"] = {"status": "supported", "item_indices": [0]}
        return result

    context, _ = owner(runtime, answer)
    assert (
        context.capture(sessions.get(sid, user_id="owner"))["status"] == "unavailable"
    )


def test_long_sources_and_role_scopes_preserve_unknown_not_cached_permission(runtime):
    from intaris.source_capture import MAX_SOURCE_CHARS

    sessions, audit, llm, evaluator, sid = runtime
    context, _ = owner(runtime, context_echo)
    first = context.capture(sessions.get(sid, user_id="owner"))
    for i in range(34):
        append_reasoning(
            audit,
            user_id="owner",
            session_id=sid,
            content=f"User message: historical {i}",
        )
    append_reasoning(
        audit,
        user_id="owner",
        session_id=sid,
        content="User message: NoDeploy remains. Docs only. " + "x" * MAX_SOURCE_CHARS,
    )
    new = context.capture(sessions.get(sid, user_id="owner"))
    assert new["source_digest"] != first["source_digest"]
    assert any(row["truncated"] for row in new["source_metadata"])
    assert any("window" in value for value in new["coverage"]["missing"])
    assert new["current_request"]["status"] == "unknown"


def test_scoped_reply_requires_actual_captured_single_referent(runtime):
    sessions, audit, llm, evaluator, sid = runtime
    append_reasoning(
        audit,
        user_id="owner",
        session_id=sid,
        content="User message: yes",
        context={"context": "Send draft notice-42 to ops@example.test only?"},
    )

    def answer(sources):
        row = next(row for row in sources if row["ref"] == "latest_user_message")
        item = context_echo([row])["items"][0]
        item.update(
            kind="approval",
            explicitness="explicit",
            reference_resolution="single_linked",
        )
        return {
            "current_request": {"status": "unknown", "item_indices": []},
            "items": [item],
        }

    context, _ = owner(runtime, answer)
    artifact = context.capture(sessions.get(sid, user_id="owner"))
    assert artifact["status"] == "ready"
    scoped = artifact["items"][0]["scope_refs"]
    assert len(scoped) == 1
    referent = next(row for row in artifact["originals"] if row["ref"] == scoped[0])
    assert (
        "notice-42" in referent["content"]
        and "ops@example.test only" in referent["content"]
    )
    assert referent["origin"] == "linked_non_authorizing_context"


def test_new_restriction_during_coherent_generation_rejects_old_snapshot(runtime):
    from intaris.policy_activation import HybridError

    sessions, audit, llm, evaluator, sid = runtime

    def answer(sources):
        append_reasoning(
            audit,
            user_id="owner",
            session_id=sid,
            content="User message: Cancel publishing. Docs only.",
        )
        return context_echo(sources)

    context, _ = owner(runtime, answer)
    with pytest.raises(HybridError, match="Sources changed"):
        context.capture(sessions.get(sid, user_id="owner"))
    assert not context._cache


def test_sqlite_migration_keeps_older_generated_assignment_unknown(tmp_path):
    import sqlite3

    from intaris.config import DBConfig
    from intaris.db import Database
    from intaris.session import SessionStore

    path = str(tmp_path / "old-schema.db")
    config = DBConfig(path=path)
    db = Database(config)
    store = SessionStore(db)
    store.create(user_id="owner", session_id="old-session", intention="Original")
    store.update_generated_intention(
        "old-session",
        user_id="owner",
        intention="Generated",
        intention_source="user",
        title=None,
    )
    db.close()
    # Produce the prior schema in an isolated test file, never a live database.
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN original_assignment")
    migrated = Database(config)
    try:
        old = SessionStore(migrated).get("old-session", user_id="owner")
        assert old["original_assignment"] is None
        assert old["intention"] == "Generated"
    finally:
        migrated.close()


@pytest.mark.parametrize(
    "invalid", ["historical", "truncated", "out_of_range", "duplicate"]
)
def test_current_request_indices_require_valid_current_evidence(runtime, invalid):
    from intaris.source_capture import MAX_SOURCE_CHARS

    sessions, audit, llm, evaluator, sid = runtime
    append_reasoning(
        audit,
        user_id="owner",
        session_id=sid,
        content="User message: Update README only."
        + (" " + "x" * MAX_SOURCE_CHARS if invalid == "truncated" else ""),
    )

    def answer(sources):
        source = next(row for row in sources if row["ref"] == "latest_user_message")
        item = {
            "source_ref": source["ref"],
            "quote": "Update README only.",
            "kind": "request",
            "horizon": "short",
            "explicitness": "explicit",
            "temporal_status": "historical" if invalid == "historical" else "current",
            "polarity": "positive",
            "reference_resolution": "direct",
            "relation": "none",
            "related_source_refs": [],
        }
        indices = (
            [1]
            if invalid == "out_of_range"
            else [0, 0]
            if invalid == "duplicate"
            else [0]
        )
        return {
            "items": [item],
            "current_request": {"status": "supported", "item_indices": indices},
        }

    context, _ = owner(runtime, answer)
    artifact = context.capture(sessions.get(sid, user_id="owner"))
    assert artifact["status"] == "unavailable"
    assert artifact["current_request"] == {"status": "unknown", "item_indices": []}


def test_postgresql_old_schema_upgrade_in_separate_database():
    import os
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn

    from intaris.config import DBConfig
    from intaris.db import Database
    from intaris.session import SessionStore

    url = os.environ.get("TEST_HYBRID_POSTGRES_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL fixture required")
    name = "source_upgrade_" + uuid.uuid4().hex
    with closing(psycopg2.connect(url)) as connection, connection.cursor() as admin:
        connection.autocommit = True
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            isolated = make_dsn(url, dbname=name)
            config = DBConfig(backend="postgresql", database_url=isolated)
            first = Database(config)
            try:
                sessions = SessionStore(first)
                sessions.create(
                    user_id="owner", session_id="older", intention="Initial original"
                )
                sessions.update_generated_intention(
                    "older",
                    user_id="owner",
                    intention="Generated newer summary",
                    intention_source="user",
                    title=None,
                )
            finally:
                first.close()
            with closing(psycopg2.connect(isolated)) as old:
                old.autocommit = True
                with old.cursor() as cursor:
                    cursor.execute(
                        "ALTER TABLE sessions DROP COLUMN original_assignment"
                    )
            for iteration in range(2):
                migrated = Database(config)
                try:
                    sessions = SessionStore(migrated)
                    assert (
                        sessions.get("older", user_id="owner")["original_assignment"]
                        is None
                    )
                    assert (
                        sessions.get("older", user_id="owner")["intention"]
                        == "Generated newer summary"
                    )
                    if iteration == 0:
                        sessions.create(
                            user_id="owner",
                            session_id="newer",
                            intention="Exact client assignment",
                        )
                    assert (
                        sessions.get("newer", user_id="owner")["original_assignment"]
                        == "Exact client assignment"
                    )
                finally:
                    migrated.close()
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
