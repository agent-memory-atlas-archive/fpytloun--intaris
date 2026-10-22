# Analytics evidence and limitations

L2 summaries and L3 analyses are observations, not permissions or execution
receipts. They do not change policy, model selection, scheduling cadence, or
review routing.

## Provenance

Analytics keeps the initial evaluation separate from its current resolution,
including resolver, scope and time. A Judge approval is not human authority.
An action-scoped approval does not authorize adjacent resources. Read batches
retain bounded redacted targets and explicit omitted-target counts.

Both conversation paths preserve intention eligibility. Event/audit copies
are merged by durable event sequence, not by matching text. Contextual messages
cannot supersede original user restrictions. Prompts distinguish recommendations,
approval requests, evaluated proposals and known execution.

## Coverage

The additive nullable `evidence_metadata` columns on `session_summaries` and
`behavioral_analyses` contain versioned local metadata. This is not a separate
ledger. Existing rows remain readable and have unknown coverage.

Window metadata records observed source identities, resolution revisions,
source bounds, child-summary dependencies and separate disposition statistics.
Late-arriving records and changed resolutions can produce another window, but
a resolution update does not increment the initial proposal counts.

Compactions record exact window membership and replace the previous compaction
transactionally. Consumers select compaction plus uncovered windows. A failed
compaction cannot suppress a fresh window. Legacy compactions cannot hide
available windows. Child-only changes invalidate the parent through the existing
queue; missing or compressed child evidence remains a coverage gap.

L3 selects direct actor sessions, including children, and presents delegated
context separately. Descendant statistics are not added to the parent's direct
action counts. Citations must match exact tenant-scoped session IDs actually
present in the rendered evidence. Metadata reports omitted sessions, coverage
gaps and dropped findings. An incomplete clean result cannot clear an existing
profile. Valid concerns can still be published; retained alerts keep their
original observation time.

Direct sessions without summaries, unresolved new source coverage, changed
resolutions and newer event prefixes also make L3 coverage incomplete. Parent
recovery compares persisted child-summary dependencies, not summary attempt
timestamps, so a child completion during parent inference remains discoverable.

An event watermark certifies only a successfully read, contiguous prefix that
is represented in completed, untruncated windows. It is never copied from the
session's event counter. Missing stores, read failures and empty filtered reads
do not advance it. A failed later partition cannot certify its uncommitted
events. The existing filtered reader cannot certify skipped or deduplicated
sequences: those gaps conservatively prevent advancement and keep L3 coverage
incomplete. This may retain concerns longer; it never turns omission into
evidence of safety. No automatic replay is added.

## Feedback and freshness

Only fresh, validated, bounded alerts enter existing legacy and hybrid Judge
reviews. They are explicitly non-authoritative and cannot supply authorization
citations. L1 receives no analytics history. Hybrid audit distinguishes an
available profile version from evidence actually consumed by Judge.

Freshness uses `ANALYSIS_LOOKBACK_DAYS`; expiry removes influence without
deleting history or forcing inference. Profiles are actor-specific. Generated
profile prose is not injected into review.

Each window, compaction and L3 operation records its accounting operation ID.
Client retries remain within the scope; queued window retries with a task ID
retain the logical operation ID. This joins analytics to content-free inference
accounting without storing provider credentials or prompts in metadata.

## Notifications and residual limits

Persisted concerning windows retain their findings for notifications even when
a later partition fails. The summary stores notification progress. Successful
delivery is not repeated on an ordinary retry. A process crash after external
delivery but before recording progress can still duplicate delivery; this is
not an exactly-once external notification protocol.

No historical backfill runs automatically. Legacy source progress uses its old
time boundary conservatively and cannot reconstruct identities that were never
stored. Legacy summaries can carry old interpretation errors until refreshed.
Evidence selection limits and compression remain explicit coverage limitations.
Reading prior source identities adds database work proportional to retained
session history; this does not add paid model calls for already covered sources.
Source discovery uses bounded keyset pages of audit identities and resolution
revisions. L2 hydrates only uncovered or changed records using an explicit
analytics projection; evaluation snapshots are neither fetched nor decoded.
L3 freshness checks use identities only and stop when uncovered evidence is
found. Event reads are paginated; covered conversation payloads and superseded
streaming parts are released while source identities still participate in
conservative prefix certification. A failed later page cannot certify a partial
read. Progress identities and genuinely uncovered evidence still grow with the
amount of retained evidence; these changes are not a hard process-memory cap.

Offline synthetic regressions verify selection, persistence and prompt contracts.
They do not prove that a model will correctly distinguish every recommendation,
legitimate approval request, or adversarial pattern. Evaluated proposals and
assistant claims do not prove execution.
