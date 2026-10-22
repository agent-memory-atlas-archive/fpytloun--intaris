"""Compare spool fsync modes using disposable storage and optional test PostgreSQL.

Run with the project Python environment. INTARIS_SPOOL_BENCH_DSN optionally
selects a local disposable database whose name ends in "_test". Only sessions
created by this invocation are deleted; no tables are truncated.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from intaris.config import DBConfig, EventStoreConfig
from intaris.db import Database
from intaris.events.store import EventStore
from intaris.session import SessionStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--payload-bytes", type=int, default=32768)
    args = parser.parse_args()
    if min(args.samples, args.rounds, args.payload_bytes) < 1:
        parser.error("samples, rounds, and payload bytes must be positive")
    dsn = os.environ.get("INTARIS_SPOOL_BENCH_DSN")
    if dsn:
        parsed = urlparse(dsn)
        if parsed.hostname not in {
            "localhost",
            "127.0.0.1",
        } or not parsed.path.endswith("_test"):
            parser.error("PostgreSQL benchmark requires a local disposable *_test DB")
    results: dict[tuple[str, bool], list[float]] = {}
    for backend in ["sqlite"] + (["postgresql"] if dsn else []):
        for trial in range(args.rounds):
            # Alternate order to reduce warmup / host-load bias.
            for fsync in [True, False] if trial % 2 == 0 else [False, True]:
                with tempfile.TemporaryDirectory(
                    prefix="intaris-spool-benchmark-"
                ) as tmp:
                    root = Path(tmp)
                    db = Database(
                        DBConfig(backend="postgresql", database_url=dsn)
                        if backend == "postgresql"
                        else DBConfig(path=str(root / "db.sqlite"))
                    )
                    sid = "spool-benchmark-" + uuid.uuid4().hex
                    tenant = "spool-benchmark"
                    sessions = SessionStore(db)
                    sessions.create(
                        session_id=sid,
                        user_id=tenant,
                        intention="Disposable local spool latency benchmark",
                    )
                    store = EventStore(
                        EventStoreConfig(
                            backend="filesystem",
                            filesystem_path=str(root / "archive"),
                            spool_path=str(root / "spool"),
                            spool_enabled=True,
                            spool_fsync=fsync,
                            flush_size=args.samples + 21,
                            flush_bytes=max(
                                4 * 1024 * 1024,
                                args.payload_bytes * (args.samples + 21) * 2,
                            ),
                        )
                    )
                    try:
                        store.set_session_store(sessions)
                        store.recover_spool()
                        measured = results.setdefault((backend, fsync), [])
                        for index in range(args.samples + 20):
                            start = time.perf_counter()
                            store.append(
                                tenant,
                                sid,
                                [
                                    {
                                        "type": "message",
                                        "data": {"content": "x" * args.payload_bytes},
                                    }
                                ],
                            )
                            if index >= 20:
                                measured.append((time.perf_counter() - start) * 1000)
                        store.flush_all()
                    finally:
                        store.close()
                        with db.cursor() as cursor:
                            cursor.execute(
                                "DELETE FROM sessions WHERE user_id = ? AND session_id = ?",
                                (tenant, sid),
                            )
                        db.close()
    report = []
    for (backend, fsync), samples in results.items():
        ordered = sorted(samples)
        report.append(
            {
                "database": backend,
                "spool_fsync": fsync,
                "samples": len(samples),
                "payload_bytes": args.payload_bytes,
                "p50_ms": round(statistics.median(samples), 3),
                "p95_ms": round(ordered[int((len(samples) - 1) * 0.95)], 3),
                "p99_ms": round(ordered[int((len(samples) - 1) * 0.99)], 3),
            }
        )
    print(
        json.dumps(
            {
                "scope": "Sequential direct append on local disk; excludes HTTP/S3 network latency",
                "results": report,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
