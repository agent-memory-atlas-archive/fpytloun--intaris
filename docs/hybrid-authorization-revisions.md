# Hybrid authorization revisions and failure contract

## Exact-action approvals and concurrent requests

An ordinary hybrid human approval without a note changes only the specific
action receipt. Its persisted `resolution_scope` is `action`; it neither
advances session authorization revision nor enters session-wide source
watermarks or Judge precedent. It remains available through exact receipt
lookup with the same tenant, session, action, mode and policy checks.
Human approval after Judge deferral follows the same rule.

Denials, notes (including whitespace), alignment approvals, legacy and unknown
records remain session-affecting. Their `resolution_scope` is `session` (old
records default to null and remain conservatively included). Their existing
revision, context-source and invalidation behavior is unchanged. This source
membership contract is versioned as `task-context-v4`; older profiles require
fresh capture, not rebasing of an approval to newer state.

New user evidence, policy/scope/lifecycle changes and guarded generated-intention
publication retain their existing invalidation rules. Thus stale responses
remain correct when the evidence actually changes; this fix prevents one
unrelated action-local approval from invalidating its siblings.

`sessions.state_revision` is an **authorization revision**, not an activity
counter. Admission and exact-action approval receipts use it together with
`policy_revision`. Existing values remain monotonic; no schema migration or
revision reset is needed. Previously invalidated receipts are not resurrected.

## Writers (canonical contract)

| Owner | Bumps authorization revision | Reason |
| --- | --- | --- |
| `SessionStore.update_session` | Policy, intention, intention source, or details replacement | Policy, authorization provenance, and working-directory scope can change. Title-only updates do not bump. Policy replacement also increments `policy_revision`. |
| `SessionStore.update_status`, idle sweep, child completion | Yes | Lifecycle changes invalidate receipts, including suspend/resume cycles. |
| `SessionStore.set_alignment_state`, `set_alignment_overridden` | Yes | Durable parent/child authorization gate changes. This is not the `last_alignment` display cache. |
| Event sequence allocation / latest user message tracking / `mark_user_message_observed` | Only user-message observations | New user evidence can revoke previous authorization. Non-user recording events do not bump. |
| `SessionStore` generated intention/profile persistence | Yes | Replaces the evaluated intention or its profile. |
| `AuditStore.insert` reasoning | Only `User message:` records with user-role provenance (including legacy records with no role) | Typed assistant, system and tool records are activity, not user authority. They do not replace `latest_user_evidence`. |
| `AuditStore.resolve_escalation` | Session-affecting human decisions, once per resolution | Exact-action approvals without notes leave sibling receipts current. Other decisions serialize with admission and record the post-decision revision. Alignment approval also opens the durable alignment gate in the same transaction. Judge decisions are not human authority. |
| Analyzer summary persistence / `last_alignment` | No | Generated summaries and list-display caches are non-authoritative activity. |
| Counters, activity timestamps, titles, ordinary audit rows | No | Presentation and bookkeeping only. |

Receipts retain tenant/session/tool/args hash/policy hash/mode identity binding.
Non-user reasoning and background summaries invalidate neither pending human
resolution nor a resolved approval. Policy, lifecycle, user evidence, intention
replacement and session-affecting human decisions still invalidate receipts. The existing
10-minute implicit receipt lookup window is unchanged.

## Operator approve bound (YOLO)

With hybrid `maximum_outcome=approve`, L1, Judge and task-context extraction are
**skipped**, not run in a fail-open background task. Alignment pending/error does
not block this path. Request denial floors, read-only ceilings, and deterministic
explicit approval requirements remain protected. Unknown read-only proof
escalates without a model call. Human-required escalations are still notified;
Judge cannot resolve them under this bound.

Admission takes the session write lock and verifies the current policy revision,
hash and approve bound. It records the current authorization revision rather than
rejecting a concurrent evidence update. A concurrent inactive lifecycle state
produces an audited denial. The latest exact human decision for the same tenant,
session, tool, args hash and mode wins if it is a denial, even across subsequent
evidence or policy updates; switching to YOLO cannot clear a human veto. A newer
exact human approval clears the veto, but cannot supply a reusable grant across
policies. Exact approved receipts can
still satisfy human floors, subject to current authorization revision checks.
Policy changes and logical-call conflicts remain errors; YOLO is not an
idempotency bypass. The response is built from the persisted audit decision.

## Pending/error alignment recovery

Outside YOLO, an active child with no in-flight alignment worker makes one
provider-timeout-bounded alignment attempt. If unavailable, or if another worker
is still pending, durable `pending` or `error` alignment returns an audited
`escalate` with reason prefix `alignment_not_ready`, rather than a permanent
409 or an unbounded model retry. Existing unattended/deny floors can turn this
into a denial. Normal action evaluation resumes after the human acknowledges
the alignment escalation through `POST /api/v1/decision`.

After deploying this source change, the stuck session can submit a normal
evaluation, then have a human approve that alignment record through the existing
UI/decision endpoint. That transaction sets `alignment_state=ready` and
`alignment_overridden=true`; no database surgery is needed. This is a recovery
procedure, not evidence that any production session has been changed.

## Stable HTTP errors and logging

Hybrid API failures preserve their status and return:

```json
{
  "detail": {
    "code": "stale_admission",
    "message": "Session changed during evaluation",
    "execution_state": "not_dispatched",
    "retryable": false
  }
}
```

Clients must branch on `detail.code`, not the message or status alone.
`execution_state` and `retryable` are existing compatibility fields; automatic
resubmission must not be inferred from a 409. A failure before admission has no
audit call ID. The shared API boundary emits a warning containing `code`,
`status`, `session_id`, `tool`, `call_id` and `logical_call_id` (null where
unavailable), also available as structured `hybrid_error` logging metadata.
It never logs exception messages, arguments, content, controller payloads or
credentials.
