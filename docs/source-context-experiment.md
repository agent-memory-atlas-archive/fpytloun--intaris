# Source-context input experiment

Switch: `HYBRID_CONTEXT_MODE=mixed|source_context` (default `mixed`).
It applies to `hybrid_v1` model input only. Policy, routing, thresholds, activation,
intention generation and admission decisions remain unchanged.

Treatment owner: `intaris.task_context.TaskContext`. It uses the existing
`ANALYSIS_LLM_*` configuration, not L1/Judge configuration. Direct callers may
inject the owner through `Evaluator(task_context=...)`; absence means baseline.
No calls are made until the unchanged policy requests L1 assessment.

Wire/prompt version: `task-context-v3` (portable strict extraction schema).
Extraction schema: bounded `items`, each
with `kind`, `horizon`, `explicitness`, `source_ref`, `start`, `end`, and
`scope_refs`. No generated text, target names or permissions. The controller
validates span/reference membership and renders the redacted source quotation.
Kinds: request/restriction/approval/disapproval/uncertainty/context. Horizons:
short/mid/long. Explicitness: explicit/inferred. Missing horizons remain missing.
Inferred items and generated/inherited context cannot be rendered as approvals.

`evaluation_metadata.task_context` is the captured private audit artifact:
version, source_digest, artifact_digest, status, coverage, watermarks,
source_metadata, originals, items, compact_intention, policy_clues, extraction
telemetry, and input versions. Originals are redacted bounded audit data, never
generic debug logging or a public artifact. Selected quotes are the only source
bodies shown to treatment L1. Judge receives that SAME artifact plus captured
originals. Extraction failure gives L1 unavailable status, compact intention and
reference metadata, never a hidden raw-history fallback.

Cache: bounded process-local single-flight keyed by tenant/session, source
content/origin/order/scope, source/decision watermarks, and extractor config plus
prompt/schema version. No current action, arguments, descriptions, verdicts or
mode enter its key. Failed extraction is cached for that source snapshot; report
fallback separately rather than retrying to improve an outcome. Source updates
invalidate; stale completion is rejected, never published as fresh context.

Telemetry: cache hit/cold/joined, extraction calls/latency/model diagnostics
(unavailable usage/cost is null, not zero), coverage/truncation, L1 input digest,
captured Judge input digest, and negative/ambiguous L1 result with no Judge yet.
Provider attempts remain recordable on the normal analysis LLM generate boundary.
The experiment does not invoke or change routing; distinguish "not yet reviewed"
from an inference that Judge was scheduled.

Human decisions require `resolved_by=user`; Judge decisions are not human
approvals. Only recorded decision text/scope IDs enter extraction, never
historical action descriptions. Legacy `User message:` forwarding is explicitly
client-attributed, not authenticated human provenance. Typed assistant/tool
events cannot become user sources through a role-looking string.

Short replies retain their already-recorded bounded assistant-question context
as `linked_non_authorizing_context`, with explicit `linked_to`/`scope_refs`;
that context cannot itself be classified as an approval. No unrelated tool
history is retrieved. Eligibility filtering precedes the record limit. Current
intention/latest message reserve their bodies; remaining user/decision bodies
are allocated newest-first, then presented chronologically, so old sources do
not consume the budget before recent restrictions. Coverage lists truncations.

## Identity-bound referents (owner replan)

`latest_user_message` aliases a captured reasoning record only by the server's
`source_audit_call_id` or by equality of its recorded `source_event_seq` and the
session's allocated `latest_user_message_seq`. Equal text, row order and recency
never establish identity. Missing linkage is `unlinked_reference:<ref>`; no
older user body is substituted for a newer record. Reasoning producers persist
audit ID and recorded role/context as internal identity metadata. These fields
are excluded from the frozen mixed-arm source presentation.

All source bodies enter one budget owner before clipping. Metadata includes
`original_length` (redacted input length), `captured_length`, and `truncated`.
The referent cap is 1,500 characters; source/global budgets still apply.
Truncated originals are not advertised as full Judge history.

Offline QA uses public events/reasoning/evaluate APIs, actual IntentionBarrier
and Judge wiring, and mocked HTTP provider transports for GPT and Jev. No paid
calls or action execution. Human-QA latency remains a measured gate: cold
extraction adds an analysis request inside client timeouts, warm snapshots do
not. No latency guarantee follows from mocked tests. Span validation proves
record membership, not human authenticity, semantic entailment or approval.

The local `extraction_wire_schema()` owner closes every object (including
`$defs.Item`) with `additionalProperties=false` and marks every property required.
`scope_refs` is therefore mandatory on the provider wire, with `[]` representing
no scope references; the internal Pydantic default is unchanged. No nullable
alternatives, new fields, or policy changes are introduced. Version v3 is included
in cache identity and captured artifacts, preventing reuse of v2 extraction.
Offline service tests validate the actual serialized strict request through
LLMClient and its HTTP transport, not just the generated Pydantic schema.

Recording identity is internal metadata, not a new client field. Typed
non-user reasoning keeps its original audit body/role and the pre-existing
revision notification, but cannot overwrite the latest-user pointer. Persisted
older metadata with no verifiable ID remains explicitly unlinked; it is not
repaired by equal text. Multiple audit bindings for one latest recorded sequence
are reported ambiguous rather than choosing a referent by recency.

### Cold/warm human-QA budget inspection

The existing barrier documents a 5-second generation wait and optional
10-second arrival wait (`intaris/intention.py`). Analysis timeout defaults to
30 seconds (`ANALYSIS_LLM_TIMEOUT_MS`); cold extraction can therefore exceed a
30-second client budget once L1/Judge and retries are added. These tests do NOT
establish acceptable live latency. Before human QA, record the pinned client's
actual timeout, SDK retry envelope, cold/warm extractor latency, barrier time,
L1/Judge latency, and separately count fallbacks/timeouts. Do not silently extend
the benchmark timeout and call client compatibility proven. Benchmark owner
must pin the approved 120B analysis/Judge configuration explicitly; no runtime
model or timeout default was changed by this experiment.
