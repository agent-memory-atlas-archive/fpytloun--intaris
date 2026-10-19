# Analysis queue ownership and recovery

Each atomic claim assigns a new opaque `claim_token`. Heartbeat, completion,
and failure require the current token and `running` status. Recovery clears
the token only when the last heartbeat (falling back to start/due time for
legacy rows) is at least ten minutes old. Startup uses the same stale-only
rule; it must not reset another process's fresh work.

## Migration and rollout

Both SQLite and PostgreSQL receive an additive nullable
`analysis_tasks.claim_token TEXT` column. There is no numbered schema-version
table. Existing running rows retain their status and NULL ownership until
demonstrably stale; migration does not fabricate a successful completion or
an owner. New claims always assign a token.

**Stop/drain all old worker binaries before starting new workers.**
Mixed-version fencing is not supported: old workers can still finalize by
task ID, and old startup code can reset fresh work. A rolling deployment with
old workers alive therefore does not provide the ownership guarantee.
Do not downgrade while new workers are active.

The token fences queue state, not arbitrary external effects of an analyzer
that has already started. Execution remains at-least-once after a crash or
lease loss; this change does not promise exactly-once LLM calls, notifications,
or analyzer writes.

Parent summary retries are inserted with their final due time in one
transaction, rather than inserting an immediately eligible row and updating
its deadline in a second transaction.

## Wakeup delivery and bounds

The application owns one `Database` instance and one shared analysis coordinator
per process. All its analysis workers share a deadline cache, idle SQL scan,
and listener. Producers use `Database.cursor(notify_analysis=True)` at the
canonical queue store boundary. Event ingestion and direct judge reviews are
unchanged. No optional vector outbox changes are included.

PostgreSQL executes `pg_notify('intaris_analysis', '')` in the mutation
transaction. Rollback suppresses delivery. A dedicated non-pooled asynchronous
connection subscribes with LISTEN; its bounded one-element notification buffer
coalesces all messages into a queue rescan. Establishing or reconnecting the
subscription forces a rescan. Connection failures fall back to SQL checks and
retry connection establishment after one second. Notification queue exhaustion
can reject the producer transaction; it cannot report success for an uncommitted
task. Producers must retain their existing transaction-error handling.

SQLite uses post-commit local hints and a single persistent autocommit connection
checking `PRAGMA data_version` every second. Versions are compared only on that
connection. Unrelated commits can also trigger a harmless rescan. In-memory
databases have local hints only because SQLite memory databases are
connection-local. The coordinator adds no per-worker polling threads.

Workers capture the hint generation/event before checking SQL. A commit during
that check or before sleeping invalidates the captured event. Durable rows,
atomic claim ordering, summary dependencies, and claim tokens remain authoritative.
Notifications carry no task payload or authority.

With lost hints, the shared authoritative sweep runs at most ten seconds apart
while workers are available. Known retry and lease-expiry deadlines shorten that
wait. A row locked by another claimant is retried after at most one second.
Backlogs drain without a sleep between claims. Ten-minute stale leases are checked
by deadline rather than the former approximately 500-second iteration counter.
These are scheduling bounds with an available database/event loop and worker
capacity, not guarantees during database outages or when all workers are busy.

`BackgroundWorker.stop()` cancels workers and awaits listener shutdown before
database teardown. Cancellation removes socket readiness callbacks and closes
the dedicated connection. Stats sampling remains a single off-loop task every
ten seconds; the last-poll timestamp and stale-reset metric are retained.

Queue claim/read operations remain synchronous as in the previous worker.
This change does not claim to eliminate event-loop stalls caused by a slow
database. It reduces redundant idle transactions; it is not a broad DB-offload
refactor.
