"""Audit pagination index bootstrap and upgrade coverage on both backends."""

from uuid import uuid4

from intaris.audit import AuditStore
from intaris.session import SessionStore

INDEX_NAME = "idx_audit_user_timestamp_id"
INDEX_SQL = f"CREATE INDEX {INDEX_NAME} ON audit_log(user_id, timestamp DESC, id DESC)"


def index_definition(db):
    with db.cursor() as cur:
        if db.backend == "postgresql":
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = ?",
                (INDEX_NAME,),
            )
        else:
            cur.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (INDEX_NAME,),
            )
        row = cur.fetchone()
    assert row is not None
    return " ".join(row[0].replace('"', "").split())


def test_fresh_database_has_ordered_audit_index(queue_db):
    definition = index_definition(queue_db)
    assert "(user_id, timestamp DESC, id DESC)" in definition


def test_upgrade_and_precreated_index_preserve_records_and_order(queue_db):
    db = queue_db
    owner = f"index-owner-{uuid4().hex}"
    SessionStore(db).create(
        user_id=owner,
        session_id="audit-index-session",
        intention="Test audit pagination",
    )
    with db.cursor() as cur:
        # Simulate a pre-index installation with real, tied-timestamp records.
        cur.execute(f"DROP INDEX {INDEX_NAME}")
        for record_id in ("audit-a", "audit-c", "audit-b"):
            cur.execute(
                "INSERT INTO audit_log "
                "(id, call_id, user_id, session_id, timestamp, decision, "
                "evaluation_path, latency_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{owner}-{record_id}",
                    f"{owner}-call-{record_id}",
                    owner,
                    "audit-index-session",
                    "2026-01-01T00:00:00+00:00",
                    "approve",
                    "fast",
                    0,
                ),
            )
    db._ensure_tables()
    assert "(user_id, timestamp DESC, id DESC)" in index_definition(db)

    # Model an operator-created index, then repeat startup without rebuilding it.
    with db.cursor() as cur:
        cur.execute(f"DROP INDEX {INDEX_NAME}")
        cur.execute(INDEX_SQL)
        if db.backend == "postgresql":
            cur.execute("SELECT ?::regclass::oid", (INDEX_NAME,))
            index_oid = cur.fetchone()[0]
    for _ in range(2):
        db._ensure_tables()
        assert "(user_id, timestamp DESC, id DESC)" in index_definition(db)
        result = AuditStore(db).query(user_id=owner, limit=2)
        assert result["total"] == 3
        assert [row["id"] for row in result["items"]] == [
            f"{owner}-audit-c",
            f"{owner}-audit-b",
        ]
        next_page = AuditStore(db).query(user_id=owner, limit=2, page=2)
        assert [row["id"] for row in next_page["items"]] == [f"{owner}-audit-a"]
        if db.backend == "postgresql":
            with db.cursor() as cur:
                cur.execute("SELECT ?::regclass::oid", (INDEX_NAME,))
                assert cur.fetchone()[0] == index_oid
