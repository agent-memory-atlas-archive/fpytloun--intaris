# Event ingestion rejection diagnostics

Event requests retain their existing HTTP status and `detail` response body.
The `X-Intaris-Event-Error` response header adds a bounded machine-readable
category. Clients must not interpret detail text as a retry contract.

| Code | Meaning |
| --- | --- |
| `recording_disabled` | Event recording is unavailable; not a missing session |
| `session_not_found` | Session is absent from the authenticated tenant |
| `empty_batch`, `batch_too_large`, `invalid_event_type`, `invalid_idempotency_key` | Request rejected before append |
| `validation_failed` | Request schema validation failed |
| `unauthenticated`, `forbidden` | Authentication or authorization rejected |
| `request_too_large`, `rate_limited` | Request size or rate rejection |
| `append_in_progress` | A pending claim exists; no new append was attempted |
| `append_uncertain` | Append outcome cannot safely be inferred; do not replay automatically |
| `unknown_rejection`, `server_error` | Unclassified failure; not evidence of session absence |

`intaris_event_http_requests_total` counts event-route responses by bounded
`method`, exact `status`, and static `category`. It includes GET and POST;
do not interpret a method-aggregated total as POST-only. Existing HTTP
status-group counters and latency histograms remain unchanged. The new
counter does not label tenant/session identifiers, payloads, source headers,
or exception messages.

## Retry and recovery limits

Pending idempotency claims are no longer deleted because they are old, nor
deleted following an append exception. Exceptions and cancellation can occur
after side effects. Pending claims survive retention cleanup. An old pending
claim returns `409 append_uncertain`; a recent pending claim returns
`409 append_in_progress` if it does not complete during the bounded wait.
Completed receipts retain the existing 24-hour retention policy.

These changes prevent automatic duplicate replay of uncertain requests. They
do not make buffered acknowledgement crash-durable. The event buffer and
idempotency completion ledger are separate persistence boundaries. A completed
receipt can still outlive unflushed events after a crash. An interrupted append
may require operator investigation; no automatic reset or replay is provided.

### Persistence interface proposal (not implemented)

Before claiming crash-safe acknowledgement, the event-store owner must provide
an operation that durably binds tenant, session, stable request identity,
payload identity, allocated sequence range, and append receipt. Reconciliation
must return one of: committed receipt, provably not committed, or uncertain.
Only the first may acknowledge success and only the second may authorize a new
append. Concurrent callers must observe the same binding. Cancellation must
not abandon completion ownership. This requires a separate design spanning
the journal/buffer and ledger, not a per-request forced flush or an HTTP-layer
sender queue. No schema or connection-commit hooks are changed here.

Local reproduction establishes these failure windows, not the cause of any
historical production rejection. Learning historical/live substatus requires
appropriate telemetry evidence and separately authorized deployment.
