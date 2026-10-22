"""Subprocess fixture: terminate after a real persistence boundary without cleanup."""

import os
import sys
from pathlib import Path

from intaris.config import DBConfig, EventStoreConfig
from intaris.db import Database
from intaris.events.backend import FilesystemEventBackend
from intaris.events.spool import EventSpool
from intaris.events.store import EventStore
from intaris.session import SessionStore

root = Path(sys.argv[1])
boundary = sys.argv[2]
owner, name = {
    "prepare": (EventSpool, "prepare"),
    "reserve": (SessionStore, "allocate_event_sequences"),
    "assign": (EventSpool, "assign"),
    "upload": (FilesystemEventBackend, "append_file"),
    "receipt": (SessionStore, "archive_spool_reservations"),
    "retire": (EventSpool, "_remove"),
}[boundary]
original = getattr(owner, name)


def terminate_after(self, *args, **kwargs):
    result = original(self, *args, **kwargs)
    if boundary != "retire" or args[0].parent.name == "batches":
        os._exit(73)
    return result


setattr(owner, name, terminate_after)
dsn = os.environ.get("INTARIS_SPOOL_WORKER_DSN")
db = Database(
    DBConfig(backend="postgresql", database_url=dsn)
    if dsn
    else DBConfig(path=str(root / "db.sqlite"))
)
store = EventStore(
    EventStoreConfig(
        backend="filesystem",
        filesystem_path=str(root / "archive"),
        spool_path=str(root / "spool"),
        flush_size=100,
    )
)
store.set_session_store(SessionStore(db))
store.recover_spool()
store.append(
    "tenant",
    "recording",
    [{"type": "user_message", "data": {"content": "Inspect the repository"}}],
    idempotency_key="same",
)
store.flush_all()
raise RuntimeError("Crash boundary was not exercised")
