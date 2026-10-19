# Deterministic workflow-step configured scope, v1

## Wire contract

`POST /api/v1/intention` accepts `{session_id, workflow_step}` for an existing
tenant/agent-bound hybrid session. This branch rejects intention, policy, details
and other session-update fields. It never recreates a session, replaces intention,
emits `session_created`, or starts an alignment job.

`workflow_step` has exactly these required fields:

| Field | Type/value |
| --- | --- |
| version | integer 1 |
| origin | `controller_workflow_step` |
| session_id | Same as outer session ID |
| task_id, workflow_id, step_name, step_run_id, logical_call_id | Nonempty identifiers |
| workflow_definition_digest, step_definition_digest, rendered_args_digest | 64 lowercase SHA-256 hex characters |
| tool, executor_id | Exact evaluated tool and executor identifiers |
| evaluated_args_digest | 64 lowercase SHA-256 hex characters |
| authorized_by | `owner_configured` or `agent_composed` |
| projection | `exact` or `unsupported` |

Identifiers are limited to 256 characters. Unknown declaration fields are rejected.
Success returns exactly `{ok, declaration_digest, admission_revision}`.

## Canonical digests and authentication

Use `SHA256(UTF8(json.dumps(value, sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)))`, lowercase hex. No Unicode normalization,
whitespace, or trailing newline is added. `declaration_digest` covers the
**workflow_step object**, not the outer intention envelope, excluding the digest
field itself. V1 sends the digest in the JWT rather than inside that object.

The existing controller JWT signature/issuer/audience and five-minute lifetime
checks apply: `typ=controller`, capability `intaris:controller_context`, `sub`
(tenant), `agent_id`, `session_id`, `governing_execution_id`,
`governing_snapshot_id`, and `declaration_digest`. The digest authenticates the
declaration. Execution/snapshot bindings are persisted from validated JWT claims.
Ordinary service auth, session updates, `details.source`, agent context and tool
arguments cannot install or overwrite a declaration.

The controller attests workflow ownership and projection correctness. Definition
and rendered-argument digests are signed commitments: Intaris does not fetch or
reconstruct workflow definitions. Intaris independently recomputes the evaluated
argument digest from the exact arguments received by `/evaluate`.

Byte-parity fixtures are in `tests/workflow_step_vectors.json`: Unicode,
reordered keys, nested and empty arguments, complete declarations and expected
argument/declaration digests.

## Persistence and lifecycle

SQLite/PostgreSQL persist declarations keyed by
`(user_id, session_id, logical_call_id)`, cascading on session deletion.
Installation locks the session row and CAS-increments authorization
`state_revision`, returned as `admission_revision`. Admission and proxy dispatch
check the captured registry revision under that same lock. Activity cannot
invalidate the registry.

| Operation | Result |
| --- | --- |
| First authenticated declaration | Persist body/JWT bindings; increment authorization revision once |
| Identical body and bindings | Return original digest/revision without mutation |
| Same logical call, different content/bindings | HTTP 409 `workflow_step_conflict` |
| Session is not `hybrid_v1` (for example `legacy`) | HTTP 422 `workflow_scope_unsupported`; nothing stored. Clients evaluate the step undeclared |
| Changed declaration | New logical call ID, atomic authorization revision increment |
| Declaration added during assessment or before dispatch | Reject stale admission; no dispatch from the old registry |
| Policy/evidence/lifecycle update | Existing authorization checks apply; no declaration rewrite |
| Session deletion | Delete declarations |
| Session without declarations | Existing behavior; no workflow configured grant or obligation |

The frozen contract has no replacement/CAS request token. The conservative
interpretation is **immutable logical-call slots**, not an invented overwrite
API. Logical calls coexist; adding a new declaration invalidates previously
captured admissions/receipts. Legacy sessions must explicitly activate hybrid
policy first; installed declarations prevent downgrade to an engine that cannot
enforce them.

## Match and enforcement

Server-owned `workflow_scope` reports `matched`, `missing`, `mismatched`,
`unsupported_projection`, a reason code and registry revision. Only exact
session/logical-call/tool/argument/agent/executor/execution/snapshot matching,
`owner_configured`, and exact projection feed `configured_grant`, with permission
basis `controller_workflow_step`. Evaluation controller context must pass the
existing controller JWT checks.

This proves configured permission only, not read-only behavior or known effects.
It never sets `human_approved`, becomes `client_user_claim`, or enters Judge
citations. Assessment receives matched scope as structured configured scope,
separate from human sources. Deny rules, human denials/checkpoints, request floors,
read-only ceilings and policy caps remain enforced.

| Reason code | Meaning |
| --- | --- |
| `workflow_step.declaration_missing` | Declared session lacks the requested logical call or authenticated context |
| `workflow_step.declaration_mismatch` | Action, arguments, agent, executor, execution or snapshot mismatch |
| `workflow_step.definition_unverified` | Agent-composed declaration |
| `workflow_step.argument_projection_unsupported` | Projection is not exact |

Once a session has declarations, omitted/unknown logical IDs fail closed as
missing instead of bypassing declaration enforcement. Other tenants/sessions
cannot retrieve these declarations; sessions with none retain normal behavior.

Failures produce audited **non-waivable escalation**. Operator approve caps,
Judge approval/uncertainty, or a human receipt cannot repair a declaration
mismatch. Independent denial rules, explicit human denials, denial floors and
mutating actions under read-only ceilings still win. Correct the declaration
with a new logical call ID.

HTTP conflicts preserve structured detail:
`{"code":"workflow_step_conflict","message":"...","execution_state":"not_dispatched","retryable":false}`.
Authorization errors are 403 (invalid JWT 401); schema errors are 422.

## Non-goals

No Cognis changes, workflow execution, deterministic-task bootstrap, delegation
provenance, fabricated user evidence, Judge citation widening, blanket allowlist,
guessed argument projection, mutable-slot replacement API, production mutation,
push, or deployment.
