# Resource audit: bounded changes and remaining gates

This change does not alter classification, model prompts, evidence selection,
retention, flush intervals, queue claim cadence, retries, or lease deadlines.
It requires only a normal application restart, with no schema migration.

## Implemented

- Capture HTTP metrics ownership and route templates before mounted dispatch.
  Successful, rejected, and failed mounted requests retain bounded labels.
  Resolve FastAPI effective route contexts when available: newer FastAPI
  versions retain pathless included-router containers, unlike the flat routes
  in 0.135.1. Real application tests were validated against both 0.135.1 and
  0.143.3; middleware-wrapped mounts and included prefixes retain templates.
- Release task-context single-flight entries on every owner terminal path.
  Waiters retain their Future, but cannot remove a newer owner's entry.
- Skip filesystem-cache registry copying when already within its byte budget.
  Over-budget eviction order and failed-unlink behavior remain unchanged.
- Share immutable S3 chunk-index snapshots on hits instead of copying lists.
  Expose a constant-time chunk-count gauge, updated under the cache lock.
  The optional `chunk_cache_estimated_bytes` gauge and its bookkeeping were
  removed after matched reader benchmarks reproduced a cold-index regression
  from its full snapshot traversal. Absent means unavailable, not zero bytes.
  Chunk count is not a byte estimate or process RSS. Cache coverage is unchanged.
- Sample background queue depth from one resilient loop every ten seconds,
  instead of each idle worker. Claim/retry/recovery code is unchanged.
  With four idle workers this removes three statistics queries per ten seconds.
  This is not a claim that the target 0.2 transactions/second has been reached.
- Batch audit projections in groups of 80 rows (880 bind parameters), in the
  original transaction with the original contiguous checkpoint calculation.
  For 161 tool events: three inserts plus checkpoint read/write, versus 161
  inserts plus checkpoint read/write.
- Exclude superseded window summaries in SQL when a compacted summary exists
  for the same tenant/session. Preserve all compacted summaries, original
  `window_start` ordering, session selection, and Python grouping. No window,
  timestamp, prompt-budget or fallback semantics change.

## Deferred, not silently approximated

### Commit-aware cross-process queue wakeups

`search/outbox.py:41` accepts an external cursor, so an enqueue notification
cannot safely mean committed work at that call boundary. Search claims also
recover five-minute leases; failed rows have future retry deadlines.
Analysis has separate task retries, heartbeat and stale-running recovery.
The current database abstraction has no cross-backend commit-notification
contract. A process-local event cannot wake another process, survive producer
failure, or reveal a missed commit.

Simply lengthening recovery polls would regress the required lost-notification
latency bound. Retain the existing one-second search poll and ten-second
analysis poll until a durable signal/notification design is validated with
transaction rollback, producer crash before/after commit, listener reconnect,
notification loss, multiprocessing, and exact retry/lease deadlines on both
PostgreSQL and SQLite. The statistics centralization is independent of that
design. No cross-process wakeup acceptance is claimed by this change.

### Historical event windows, exports and audit pagination

The event store currently merges buffered and persisted events, filters and
deduplicates them before analysis applies last-sequence-per-part selection.
Sequence order alone does not prove timestamp ordering. Existing chunk names
contain sequence bounds, not trustworthy timestamp bounds. A metadata index
requires legacy fallback and parity for overlapping ranges, duplicate
sequences, corruption and late timestamps.

The v1 export captures `event_last_seq`, returns counts and all three evidence
collections, and converts read failures to an HTTP error before sending the
response. Direct streaming would commit headers before later corruption/read
failures and cannot safely supply counts without staging or an equivalent
snapshot. Streaming needs its own bounded staging/snapshot design rather than
an iterator substituted into JSONResponse. No export schema, high-water or
error behavior changes here. Audit-window pagination/count rewrites likewise
remain deferred pending differential ordering and evidence coverage tests.

### Cache coverage and sweep locking

There is no metadata byte estimate or new cap. Enforcing a lower
chunk/byte budget would reduce effective cache coverage and potentially amplify
S3 requests/latency. Qualify sizing against representative histories first.
The filesystem sweep still uses its existing registry lock: moving unlink
outside that lock needs identity-safe file replacement coordination with
concurrent fills/readers. No claim of sweep-lock improvement is made.

### Accepted-event durability during prolonged outage

`events/store.py:1380` still re-buffers on flush failure and drops oldest events
above 10,000 events per session. This is an existing reliability defect,
explicitly outside this optimization. Do not lower the cap. Durable spooling,
crash recovery, acknowledgment ownership and overload policy need a separate
design and accepted-event preservation tests.

## Deployment qualification

After parent-owned integration/deployment, confirm mounted HTTP series appear
with templates rather than IDs; compare chunk-index counts, S3
list/get calls, queue depth and pending age against a matched baseline.
Do not reduce production requests/limits based on synthetic microbenchmarks.
No production mutations or load tests are part of this work.
