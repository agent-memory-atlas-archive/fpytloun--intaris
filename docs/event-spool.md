# Local event spool

The event store uses a local filesystem spool by default. With the default fsync
setting, incoming batches are synced before success is returned, then consolidated into immutable NDJSON chunks
using the existing count, byte, and age thresholds. This protects acknowledged
events from process/container crashes without writing event payloads to PostgreSQL.

## Boundaries

- One application process and one replica per spool/deployment. A filesystem lock
  prevents competing writers on the same directory; it is not cross-pod fencing.
  Session-aware routing and multi-replica ownership are a future stage.
- A disk-backed Kubernetes `emptyDir` survives a container restart in the same pod.
  It does **not** survive pod replacement, eviction, node loss, or volume loss.
- This is not offline ingestion. Startup probes the archive. A failed probe,
  archive read, or upload pauses new appends. Background recovery probes and
  drains accepted data before reopening admission. Existing accepted files are
  preserved; they are never evicted or dropped to keep accepting requests.
- Batching and periodic probes imply a detection window. The system does not
  perform an S3 round trip for every append, nor promise atomic local/S3 commits.
- An ambiguous append response must be retried with the same idempotency key.
  Without a client key, separate client submissions can duplicate events.

## Protocol and ownership

1. Validate and normalize the input. Write a checksummed batch file using
   file fsync, atomic replacement, and directory fsync.
2. Allocate sequences and advance user-message state in the same relational
   transaction as a small batch reservation. Retrying a reservation returns the
   same range without repeating sequence/user-state changes.
3. Publish the committed assignment in the in-memory file index, then acknowledge.
   The fsynced input file plus committed relational reservation are sufficient for
   replay; rewriting/fsyncing the payload merely to duplicate its sequence range
   is unnecessary. PostgreSQL holds only metadata, never the staged payload.
   Embedded stores without a SessionStore persist the assignment locally instead.
   Recovery of database-backed batches always requires the original SessionStore.
4. Seal a contiguous group of batches into a final NDJSON file. Persist exact
   membership, destination, range and checksum before upload.
5. Upload with an immutable destination. Retry lost responses using identical
   bytes/key, not a newly grouped batch. Retire only the confirmed source files.

Atomic batch files avoid a custom append-in-place WAL and torn-line recovery.
Temporary files are discardable; committed checksum failures are not silently
ignored. Replay resolves prepared assignments before request/background producers
start. Missing spool data with unresolved relational reservations blocks startup
unless the exact original events can be verified in the archive.

The main relational database retains batch receipts for the session lifetime.
Explicit recording deletion invalidates receipts and uses a local deletion intent
so restart cannot resurrect pending events. Session deletion cascades reservations.
This adds a small row per incoming batch, not a second payload storage system.
Archived receipts remain valid after a drained spool is disabled. An unfinished
deletion fences both append and export until deletion recovery completes.

Pending reads merge local files with the archive under the session lifecycle
lock. Forward, reverse, tail, exact-sequence, streamed reads and availability
checks use this same pending view. Only bounded export batches are materialized;
upload uses the sealed file body rather than reading a large chunk into memory.
Individual incoming batches and existing archive chunk reads still have their
ordinary memory cost. Larger flush targets should therefore be benchmarked.

## Configuration and batching

| Setting | Default |
|---|---|
| `EVENT_STORE_SPOOL_ENABLED` | `true` |
| `EVENT_STORE_SPOOL_FSYNC` | `true` |
| `EVENT_STORE_SPOOL_PATH` | `${EVENT_STORE_PATH}-spool` |
| `EVENT_STORE_SPOOL_MAX_BYTES` | 1 GiB |
| `EVENT_STORE_BACKEND_CHECK_INTERVAL` | 5 seconds |
| `EVENT_STORE_FLUSH_SIZE` | 100 events |
| `EVENT_STORE_FLUSH_BYTES` | 4 MiB, approximate including staging metadata |
| `EVENT_STORE_FLUSH_INTERVAL` | 30 seconds maximum age, checked periodically |

Thresholds are evaluated at input batch boundaries: one large request can exceed
a threshold. Increasing them consolidates S3 writes; it does not delay the local
durability barrier. Age is persisted and is not reset by restart.

### Optional unsynced mode

`EVENT_STORE_SPOOL_FSYNC=false` skips **all spool** file and directory fsync
barriers (preparation, manifests, sealed files, deletion and retirement). Python
buffers are still flushed/closed, and atomic replacement and checksums remain.
This is suitable only when protection against an Intaris process/container crash
on a surviving node is sufficient. It does not guarantee correct persistence
ordering or preservation of acknowledged events across a host/kernel crash,
reboot, or power loss. `emptyDir` still does not survive pod deletion in either
mode. Database commits and archive backend durability are unaffected.

Switch modes by changing configuration and restarting the process with the same
spool volume. When fsync is re-enabled, startup syncs surviving spool files before
recovery/serving; this cannot recover data already lost during an earlier crash.
Keep fsync enabled unless a measured latency benefit justifies the weaker guarantee.

To repeat the local comparison, run `python scripts/benchmark_event_spool.py`
with the project environment. Optionally set `INTARIS_SPOOL_BENCH_DSN` to a
disposable local PostgreSQL database ending in `_test`. The script never truncates
tables and removes only its own sessions. It alternates mode order across three
rounds, excludes warmup, and measures 300 sequential direct appends per mode with
one 32-KiB event per request. Upload/retirement time and HTTP/S3 network latency are
outside the append measurement.

One local executor qualification produced:

| Database | Spool fsync | p50 | p95 | p99 |
|---|---|---:|---:|---:|
| SQLite | on | 33.01 ms | 37.31 ms | 43.21 ms |
| SQLite | off | 12.96 ms | 16.22 ms | 17.73 ms |
| PostgreSQL | on | 34.34 ms | 39.45 ms | 45.41 ms |
| PostgreSQL | off | 6.32 ms | 9.68 ms | 13.51 ms |

These results are not a production throughput or disk-latency guarantee. Repeat
on the deployment's spool volume and representative batch sizes/concurrency.

The admission budget reserves room for assignment replacement and export copies,
and also checks actual free disk space. A separate ceiling of 10,000 pending
batches bounds metadata growth. Full/read-only disks stop writes. Do not size the
volume exactly to the admission budget: allow additional filesystem/temporary
space. Local write/fsync faults require investigation and process restart.
Read failures are retried before admission reopens; a successful bucket probe
alone cannot clear failed GetObject access. More than 32 distinct failed read
checks require a restart after repairing the backend, bounding recovery metadata.

The immutable S3 read cache remains separate and disposable. No cache TTL, LRU,
cleanup or cache-write failure may delete or invalidate pending spool data. Large
file uploads warm that cache lazily on subsequent reads instead of copying the
whole chunk into memory during upload.

## Kubernetes deployment

Add a **separate disk-backed emptyDir**, not a subdirectory managed by the cache:

```yaml
spec:
  replicas: 1
  strategy:
    type: Recreate
  template:
    spec:
      terminationGracePeriodSeconds: 120
      containers:
        - name: intaris
          env:
            - name: EVENT_STORE_SPOOL_ENABLED
              value: "true"
            - name: EVENT_STORE_SPOOL_PATH
              value: /var/lib/intaris/event-spool
            - name: EVENT_STORE_SPOOL_MAX_BYTES
              value: "1073741824"
          volumeMounts:
            - name: event-spool
              mountPath: /var/lib/intaris/event-spool
      volumes:
        - name: event-spool
          emptyDir:
            sizeLimit: 2Gi
```

Merge this fragment with existing deployment configuration. Configure pod
ephemeral-storage requests/limits for both spool and cache. Do not use
`emptyDir.medium: Memory`. Ensure the service UID can write and sync the mount.
S3 credentials need bucket-probe access (`HeadBucket`, governed by bucket access)
as well as existing read/list/write permissions.

### Rollout and rollback

Before a pod-replacing rollout, stop upstream producers, let in-flight requests
finish, and verify the archive is healthy and the spool is drained. Inspect
`event_store.spool.pending_batches`, `pending_events`, `write_admission`, and
`backend_unavailable` in the health response. Normal shutdown also flushes after
background producers stop, but shutdown timeout or node failure is not a drain
guarantee. Do not delete the pod during an archive outage.

Before disabling spooling, changing its path, changing archive destination, or
rolling back to a version without spool support, drain all accepted data first.
Startup refuses a disabled nonempty spool or unresolved relational assignments.
Do not remove pending files to bypass this guard. A PVC can preserve the same
protocol across pod replacement if stronger operational guarantees are needed.

Legacy pre-spool idempotency receipts are honored. Ambiguous legacy claims remain
uncertain; the implementation never invents missing payloads or sequence history.

## Validation

`tests/test_event_spool.py` exercises persistence, API retries, admission gating,
capacity, checksum/ownership failures, deletion recovery and batching. Its helper
`tests/spool_crash_worker.py` exits a real subprocess without cleanup after
preparation, reservation commit, assignment, upload, archive receipt, and partial
retirement. Tests assert unchanged timestamps, contiguous history, duplicate-safe
responses, and one user-state update.

Run against SQLite and an explicitly disposable PostgreSQL database:

```bash
INTARIS_SPOOL_TEST_DSN=postgresql://test:test@localhost:5432/spool_test \
  uv run pytest -q tests/test_event_spool.py
```

The PostgreSQL fixture truncates session data; never supply a production DSN.
Also run event-store, audit, reconciliation, API and analytics regression suites.
Before enabling in production, measure append latency/throughput, fsync latency,
archive chunk sizes, disk usage and restart recovery time on the actual volume.
