"""Real two-connection PostgreSQL authorization fencing; isolated test DB only."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from intaris.config import DBConfig
from intaris.db import Database
from intaris.policy_activation import HybridError
from intaris.policy_inheritance import validate_parent_chain
from intaris.session import SessionStore


@pytest.fixture
def pg_store():
    url = os.environ.get("TEST_HYBRID_POSTGRES_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL fixture required")
    db = Database(DBConfig(backend="postgresql", database_url=url))
    store = SessionStore(db)
    owner = "inheritance-test-" + uuid.uuid4().hex
    store.create(
        user_id=owner,
        session_id="parent",
        intention="Research",
        policy={"evaluation_policy_version": "hybrid_v1", "profile": "balanced"},
    )
    store.create(
        user_id=owner,
        session_id="child",
        parent_session_id="parent",
        intention="Research",
    )
    yield db, store, owner
    db.close()


@pytest.mark.parametrize("boundary", ["admission", "activation"])
def test_parent_writer_waits_until_authorization_transaction_commits(
    pg_store, boundary
):
    db, store, owner = pg_store
    snapshot = store.get("child", user_id=owner)
    started, committed = threading.Event(), threading.Event()

    def writer():
        started.set()
        store.update_session("parent", user_id=owner, intention="Do not publish")
        committed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.cursor() as cur:
            if boundary == "admission":
                validate_parent_chain(cur, snapshot, postgresql=True)
            else:
                store._guard_parent(cur, store.get("parent", user_id=owner), owner)
            future = pool.submit(writer)
            assert started.wait(5)
            # A real independent connection cannot commit behind the fence.
            assert not committed.wait(0.2)
        future.result(timeout=10)
    assert committed.is_set()
    with db.cursor() as cur:
        with pytest.raises(HybridError, match="Parent authorization changed"):
            validate_parent_chain(cur, snapshot, postgresql=True)


def test_parent_change_committed_first_is_rejected_before_admission(pg_store):
    db, store, owner = pg_store
    old = store.get("child", user_id=owner)
    store.update_status("parent", "suspended", user_id=owner)
    with db.cursor() as cur:
        with pytest.raises(HybridError, match="suspended"):
            validate_parent_chain(cur, old, postgresql=True)
