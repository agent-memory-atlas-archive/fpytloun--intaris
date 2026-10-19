"""Nullable/no-backfill profile migration and backend CAS acceptance."""

import json
import os
import uuid
from contextlib import closing
from unittest.mock import MagicMock

import pytest

from intaris.config import DBConfig
from intaris.db import Database
from intaris.intention import generate_intention
from intaris.intention_profile import snapshot
from intaris.session import SessionStore
from intaris.task_context import TaskContext
from tests.test_policy_evidence_runtime import runtime as runtime  # noqa: F401


def check_upgrade(config):
    first = Database(config)
    sessions = SessionStore(first)
    sessions.create(user_id="owner", session_id="older", intention="Initial assignment")
    sessions.update_generated_intention(
        "older",
        user_id="owner",
        intention="Generated summary.",
        intention_source="user",
        title="Preserved title",
    )
    assert sessions.get("older", user_id="owner")["intention_profile"] is None
    with first.cursor() as cur:
        cur.execute("ALTER TABLE sessions DROP COLUMN intention_profile")
    first.close()
    for _ in range(2):
        migrated = Database(config)
        try:
            session = SessionStore(migrated).get("older", user_id="owner")
            assert session["intention_profile"] is None
            assert session["intention"] == "Generated summary."
            assert session["title"] == "Preserved title"
            with migrated.cursor() as cur:
                cur.execute(
                    "SELECT intention_profile FROM sessions WHERE user_id = ?",
                    ("owner",),
                )
                assert cur.fetchone()[0] is None
        finally:
            migrated.close()


def test_sqlite_upgrade_no_backfill(tmp_path):
    check_upgrade(DBConfig(path=str(tmp_path / "upgrade.db")))


def test_postgresql_upgrade_no_backfill():
    url = os.environ.get("TEST_HYBRID_POSTGRES_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL fixture required")
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn

    name = "single_intention_upgrade_" + uuid.uuid4().hex
    with closing(psycopg2.connect(url)) as connection, connection.cursor() as admin:
        connection.autocommit = True
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            check_upgrade(
                DBConfig(
                    backend="postgresql",
                    database_url=make_dsn(url, dbname=name),
                )
            )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_profile_cas_atomicity_both_backends(runtime):
    sessions, audit, llm, evaluator, sid = runtime
    before = sessions.get(sid, user_id="owner")
    assert sessions.update_generated_intention(
        sid,
        user_id="owner",
        intention="Winning summary.",
        intention_source="user",
        title="Winning title",
        intention_profile={"captured": "winner"},
        expected_state_revision=before["state_revision"],
    )
    assert not sessions.update_generated_intention(
        sid,
        user_id="owner",
        intention="Stale summary.",
        intention_source="user",
        title="Stale title",
        intention_profile={"captured": "stale"},
        expected_state_revision=before["state_revision"],
    )
    current = sessions.get(sid, user_id="owner")
    assert current["intention"] == "Winning summary."
    assert current["title"] == "Winning title"
    assert '"winner"' in current["intention_profile"]


@pytest.mark.parametrize("human_note", [False, True])
def test_no_user_bootstrap_and_human_refresh_both_backends(runtime, human_note):
    sessions, audit, _, _, sid = runtime
    call_id = uuid.uuid4().hex
    audit.insert(
        call_id=call_id,
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="inspect",
        args_redacted={"path": "/repo"},
        classification="read",
        evaluation_path="llm",
        decision="escalate" if human_note else "approve",
        risk="high" if human_note else "low",
        reasoning="Inspect repository.",
        latency_ms=0,
    )
    if human_note:
        audit.resolve_escalation(
            call_id,
            "approve",
            "You may inspect this repository.",
            user_id="owner",
        )
    before = sessions.get(sid, user_id="owner")
    captured = snapshot(sessions._db, before)
    assert not any(r["ref"] == "latest_user_message" for r in captured["originals"])
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "title": "Repository inspection",
            "intention": "Inspect the repository.",
            "items": [
                {
                    "kind": "approval",
                    "msg": 1,
                    "quote": "You may inspect this repository.",
                    "status": "current",
                }
            ]
            if human_note
            else [],
            "current": [],
        }
    )
    assert (
        generate_intention(
            llm=llm,
            db=sessions._db,
            session_store=sessions,
            user_id="owner",
            session_id=sid,
            generation="single",
            intention_source="bootstrap",
        )
        == "Inspect the repository."
    )
    llm.generate.assert_called_once()
    payload = json.loads(llm.generate.call_args.args[0][1]["content"].splitlines()[1])
    assert payload["latest"] is None
    properties = llm.generate.call_args.kwargs["json_schema"]["schema"]["properties"]
    assert ("minItems" in properties["items"]) is human_note
    after = sessions.get(sid, user_id="owner")
    assert after["title"] == "Repository inspection"
    assert after["intention"] == "Inspect the repository."
    assert after["intention_profile"] is not None
    assert after["state_revision"] == before["state_revision"] + 1
    context = TaskContext(
        llm=llm,
        sessions=sessions,
        audit=audit,
        extractor_identity={},
        generation="single",
    ).capture(after)
    assert context["status"] == ("ready" if human_note else "not_required")
    assert context["current_request"]["item_indices"] == []
    assert len(context["items"]) == int(human_note)
    llm.generate.assert_called_once()
