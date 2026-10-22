# Hybrid policy protocol v1

## Fact-first profile correction (`builtin-v10`)

Classification is profile-independent. Profiles weigh the same observations and
assessment; the classifier neither grants permission nor selects the outcome.
`risk=unknown` denotes missing impact evidence, not absence of a separate request
for a supporting step. Impact and instruction compliance remain distinct.

Bounded autonomy ignores unsupported task-relationship, target-alignment and
parent-compatibility opinions. It does **not** ignore a reported explicit
instruction violation merely because the classifier omitted its citation.
Such conflicts remain reviewable in every profile. Grounding diagnostics report
`grounded`, `ungrounded` or `unavailable`; they verify provenance and quote
existence, not semantic applicability. Configured denies, read-only ceilings,
human refusals, checkpoints and known hazardous effects remain protected.

For authenticated admitted-spawn lifecycle operations, unresolved task relevance
alone is no longer a reason to ask for permission again in balanced or bounded
autonomy. Explicit restriction conflicts and uncertain/high operational impact
still require review. Ownership is not proof that termination is harmless.

Hybrid parent alignment is carried as `Evidence.parent_alignment` and evaluated
by the same deterministic policy. Judge reviews `alignment.compatibility`
through the ordinary action-bound review path, not a second alignment-specific
Judge invocation. Bounded autonomy does not wait for an unavailable alignment
classifier in the HTTP handler or evaluator; other action checks still run.
Legacy session-level alignment acknowledgment retains its existing behavior.

The optional JSON `restriction_quote` is bounded free text. Enum-only typed
providers do not emit it. Only original instruction sources and advisory clauses
can ground a reported prohibition; generated text and bare target references
cannot. Missing authoritative bodies stay distinguishable from unsupported
opinions. Earlier independent restrictions are not discarded.

## Live-acceptance correction schema delta

`builtin-v3` / `classification-v3` keep the public request API unchanged. Model
answers add finite `authorization_source` (`session_intention`,
`latest_user_message`, `parent_intention`, `none`) and `authorization_scope`
(`within_scope`, `outside_scope`, `unclear`). These select supplied records; they
cannot create records, executor facts, resources or permissions. The controller
derives only `authorization_source_available` and `authorization_source_independent`;
availability/authenticity is NOT semantic applicability. No prose or shell-command
matcher proves permission. The finite `authorization_scope` assessment compares
the exact call, all targets and effective arguments to the supplied source.
Generated summaries and inherited parent context are not independent grants.
Aligned low risk is no longer sufficient to approve a
consequential action. Ordinary bounded local work can have implicit authorization.

`compiler-v4` publishes op-specific recursive JSON Schema, including legal field
and enum combinations. Independently checked English forms currently include
read/path, delete/(not-)path, production/development modification, simple
read/write/delete and tool-glob clauses. Unsupported compound exceptions and
exclusive-only grants remain advisory rather than rejecting activation; malformed
generation/provider failure remains 503. Coverage checks reject dropped operations,
conditions and negations from deterministic rules.
This is deliberately not a guarantee for arbitrary English; structured rules
remain the complete bounded authoring interface.

`path_matches.quantifier` is `any` or `all`; omitted/null retains the original
`any` behavior for existing structured callers. Empty targets are unknown.
`any` is true for a known match; false requires complete coverage. `all` is false
for a known counterexample; true requires a non-empty, completely covered set.
Other cases remain unknown, including under negation. English read grants require
`all` target coverage plus read-only whole-call effect obligations; negative path
deletion compiles to `contains(delete) AND NOT path_matches(all)`. Mixed targets
in a positive deletion denial use `contains(delete) AND path_matches(any)`.
Known matching witnesses deny even when target coverage is partial. Mixed targets
cannot borrow an allowed subset. Source-coverage checks compare the declared
full-match form with the normalized predicate, not arbitrary English meaning.

### Per-evaluation plan/read-only ceiling

Optional existing root `context.chat_mode` (`default`, `plan`, `build`),
`context.chat_mode_source` (string), and `context.read_only_required` (boolean)
are accepted unchanged. `plan` or true requires read-only behavior; conflicting
flags resolve restrictively. Invalid types return 422. Session
`policy.read_only_required=true` cannot be relaxed by a request. Build/default
removes only that call's overlay, not any session restriction or other policy.

The finite `mutation` label (`read_only`, `mutating`, `unclear`) describes actual
whole-call state effects, not execute/transmit capability. Python/Bash diagnostics
and public HTTP reads can be read-only. Known mutation is denied after all caps,
profiles, Judge and human resolution. Unknown requires assessment and remains
non-executable until established read-only. This also applies to legacy sessions
without requiring policy compilation. Missing metadata retains legacy behavior.

The audited overlay/mode identity scopes cached decisions, approval references
and admission identity. A late build result cannot supply plan evidence. Modes
do not stick across later calls. Native MCP transports existing root context
under `_meta.intaris.context`, and returns audit metadata under `_meta.intaris`.
No tool argument, assistant rationale, or ordinary chat text can relax the overlay.

Hybrid Jev records posterior winner probability, top-runner margin and provider
confidence separately. `JEV_HYBRID_MINIMUM_POSTERIOR` (0.6) and
`JEV_HYBRID_MINIMUM_MARGIN` (0.1) apply only to the new hybrid questions. They are
candidate settings pending separate-cohort calibration, not a holdout result.
Existing legacy confidence and risk-floor settings retain their semantics.

Status: implemented contract; benchmark and rollout acceptance are separate gates.

## Delegated-session scope

An Intaris child session links to its parent for intention alignment. This is not
an authority-inheritance mechanism: creating a child does not import a Cognis
agent profile, tool grants, executor facts or parent policy automatically.
Explicit child policy inputs remain subject to ordinary activation and
authentication. Cognis runtime profile inheritance is deferred; no fixture may
treat its absence as authenticated inherited authority in standalone Intaris.

## Compatibility and activation

All existing `/intention`, `/reasoning`, `/evaluate`, `/decision`, `/mcp/call`
and native MCP requests remain valid. Authentication remains API key or existing
JWT. No controller, client receipt consumption, revision, hash or logical call
identifier is required.

`EVALUATION_POLICY_VERSION` selects the server default: `hybrid_v1` (default) or
explicit opt-in `legacy`. A session policy may explicitly select `evaluation_policy_version`.
Existing persisted sessions without a version remain legacy. New sessions pin
the selected version; changing the server default never silently migrates them.
Hybrid profile defaults to `balanced`; alternatives are `strict` and
`bounded_autonomy`. Existing hook clients require no changes.

Additive policy inputs are `evaluation_policy_version`, `profile`, `rules`, and
`intention_gate` (`disabled` by default, or `jev`). Existing tool/command/path
globs, allow/deny text policies and supported text-object forms remain accepted.
Ready structured rules never invoke a compiler. Text compiles at creation/update
on a tenant-scoped cache miss, atomically. Unsupported text is retained as advisory
operator policy. Malformed policy/rule structures and unsupported Judge policies
still reject activation. Legacy normalization and prompt bytes are unchanged.

### Advisory operator policy

The active snapshot stores `rules` and `advisory_clauses` side by side. Each
advisory clause has a stable container/index `id`, `effect`, exact `text`, and
original `source`. Both collections participate in `policy_hash`. Unsupported
compiler output retains the complete untrusted batch as advisory, never partial
deterministic rules. No grammar expansion is implied: for example, "Never push
to main" and external-email restrictions are advisory.

Advisory clauses are context, not routing. They never force assessment and
never create obligations, so verified static reads keep the deterministic fast
path regardless of them. When a call reaches L1 for another reason, L1 receives
the clause text as an `Operator restrictions` data block. L1 classifies a conflict
in its existing `contextual_restrictions` field and may select the clause as
`restriction_reference`. Such a conflict is independently reviewed in every
profile; it is never a deterministic deny. Operator clauses never supply user
authority or a grant.

The Judge receives the clauses as non-citable context beside controller facts,
never among numbered user originals. Its output schema has no operator fields.

Hard restrictions belong to exact compiled rules (`deny_tools`, `deny_paths`,
`deny_commands`, compiled deny rules), explicit human denials and
`minimum_outcome` floors, which still apply deterministically, or to the sandbox
and tool exposure.

Rules have `id`, `effect` (`allow`, `deny`, `require_approval`), `when`, and
optional `description`. Predicates use bounded `all`, `any`, `not`, `eq`, `in`,
`contains`, `tool_matches`, `command_matches`, `path_matches`. Known field/operator
types only; 64 rules, 32 nodes per rule, depth 6. No executable expressions or
user classifier instructions. Unknown is not false, including under negation.

## Optional request extensions

Evaluation and REST MCP accept `evaluation_policy_version`, `logical_call_id`,
`expected_policy_revision`, `expected_policy_hash`, and `controller_context`.
REST MCP also accepts the existing evaluate `context`, `minimum_outcome` and
`approval_call_id`. All extensions are optional. Native MCP carries extensions
under request `_meta["intaris"]`, never inside upstream arguments.

Absent identifiers and revisions are captured from authenticated server session
state before evaluation/compilation. Supplied expectations are checked; they do
not confer authority. A logical identifier binds to the exact original action.
Changed arguments under the same identifier conflict. Approval retries continue
to support existing `approval_call_id` and exact-call approval semantics.

Session create/update accepts optional `expected_policy_revision`. Activation
returns additive `policy_revision`, `state_revision`, `policy_hash`, and
`active_policy` metadata. The server-generated active snapshot contains version,
profile, bundle/classification versions, compiled hash, rules, advisory clauses and compiler
provenance. It persists in existing session storage; no registry.

## Classification and decision ownership

Current-intent relationship choices are exactly `directly_serves_intent`,
`supports_intent`, `unrelated`, `conflicts_with_intent`, `unclear`.
Models choose finite classifications over supplied evidence. They cannot invent
resources, authenticated facts or permissions. Existing prompt assembly,
intention generation and parent/child alignment remain owners of their context.
Latest restrictions are supplied independently of intention summaries.
Their presence can require intent classification even when an older permit
matches; this is not policy recompilation. An intent conflict cannot silently
reuse an older permit or human approval.

Additional finite classification fields are risk, authorization, operation,
environment, effects, reversibility and side-effect scope. Canonical enums and
JSON schemas live in `intaris/hybrid.py`.

### Profiles

- `strict`: approval needs cited user evidence; consequential actions need a human.
- `balanced`: approve work consistent with the intention and not harmful. An
  `unclear` relationship, unresolved restrictions or targets and a missing
  per-action grant are not reasons to ask. Escalate only for an `unrelated` or
  conflicting relationship, high risk, a classified restriction conflict, a
  target outside the task, or an external/irreversible effect (send, order,
  delete, push, deploy) without explicit authorization. The Judge answers "is
  this consistent with what the user is doing, and not harmful?"; citations are
  optional evidence and never a reason to downgrade its approval.
- `bounded_autonomy`: approve unless the action clearly contradicts the
  intention or a classified restriction, or is high risk with an
  external/irreversible effect. The Judge approves or refuses and never asks;
  a refusal is a deny with a specific agent-facing reason.

Session caps apply on top of every profile. The Judge is offered only answers
whose outcome the session `maximum_outcome` can still produce: with
`maximum_outcome=escalate` it chooses approve or ask, and the never-ask
behaviour applies only when deny is reachable. A `minimum_outcome=deny` floor
fixes the outcome and the Judge is not called.

One policy owner handles static facts, L1 and Judge classifications. Judge sees
the full prior output/distributions and broader context and returns classifications,
not permission. Disabled/advisory/auto modes remain supported. At most one L1
and one Judge assessment; a bounded malformed-schema repair is not an approval loop.

Precedence: structural/error gates; ordinary policy (deny before allow);
existing maximum outcome cap; mandatory-human rule floor; request minimum floor.
Only current scoped human approval satisfies human floors, never Judge.
Human approval reuse is bound to both policy identity and relevant state revision.
New user evidence invalidates an old exact-call approval. Judge allowed-decision
and uncertainty settings restrict the policy result without weakening human floors.
Permitted writes never become factual READ. Static coverage is not expanded into
a general shell/resource analyzer.

Optional intention gate answers `keep`, `refresh`, `uncertain`. Disabled preserves
existing refresh behavior; refresh/uncertain follow the existing generation path.
Hybrid child alignment remains available with behavioral analysis disabled.
Pending checks and infrastructure errors remain non-executable; misalignment
creates an ordinary human-resolvable alignment escalation.

## Admission and execution

Short session-local transactions compare captured policy/state revisions and
human resolution, then finalize and record admission in the existing audit.
Policy, status, latest user evidence and alignment writers participate in ordering.
No network calls under the lock. SQLite and PostgreSQL implement the same boundary.
A pre-admission change invalidates the stale assessment; a post-admission change
governs later calls and does not retroactively cancel dispatch.

Additive result metadata: `evaluation_policy_version`, `logical_call_id`,
`action_digest`, `policy_revision`, `state_revision`, `policy_hash`, `profile`,
`admission` (null or `{admitted_at, dispatch_deadline}`), `execution_state`
(`not_dispatched`, `dispatched`, `completed`, `uncertain`),
`human_approval_required`, `mandatory_rule_ids`, `reason_codes`.
Existing decision fields and meanings remain unchanged.

Admission expires after five seconds for server-owned MCP dispatch. Original
arguments are hashed and statically inspected before compaction; omitted evidence
cannot establish absence of prohibited effects. MCP executes originals only.
No automatic execution-bearing retry after ambiguous upstream delivery.
Native MCP returns a `CallToolResult` preserving `isError`; additive Intaris
execution/admission metadata is carried under response `_meta.intaris`.

**Legacy client limitation:** `/evaluate` cannot guarantee that an independent
client dispatches the unchanged action within the receipt deadline, nor prevent
that client retrying a lost execution. Admission metadata is informational for
unchanged clients. Intaris guarantees its own admission ordering and MCP dispatch,
not global revocation or exactly-once external execution.

## Provenance and errors

Ordinary context/recording APIs remain available with honest source provenance.
Assistant rationale and tool output are not user authorization.
Optional privileged `controller_context` requires a dedicated signed JWT payload:
`iss=cognis`, `aud=intaris`, `typ=controller`, `sub`, `agent_id`, `session_id`,
`capabilities=["intaris:controller_context"]`, `iat`, `nbf`, `exp`, `jti`.
Maximum lifetime five minutes. Governing execution/snapshot references must match
signed claims; executor identity is a controller assertion. UI/exchange tokens and
API keys cannot assert this envelope. Ordinary API-key/JWT access is unchanged.

Structured error detail adds `code`, `message`, optional `field_errors`,
`execution_state`, `retryable`: 422 invalid/unsupported policy or protocol;
401/403 authentication/provenance; 409 stale revision/hash/action/approval;
503 pre-dispatch compiler/assessment failure. For compatibility, upstream MCP
errors retain the existing successful HTTP envelope with `isError=true`, adding
`execution_state=uncertain` and `retryable=false` after dispatch. A transport
502/504 or lost MCP response must likewise be treated as uncertain by clients.
Failures never become executable through outcome caps.

## Local configuration and authoring

Leave `EVALUATION_POLICY_VERSION` unset, or set it to `hybrid_v1`, to exercise
unchanged clients against hybrid balanced behavior. Set it explicitly to `legacy`
to opt into the legacy evaluator for new sessions.
Existing sessions are pinned; create a fresh session or explicitly activate
`policy.evaluation_policy_version=hybrid_v1` on that session. Do not change
production defaults on the strength of unit tests alone.

Deployments that explicitly set `EVALUATION_POLICY_VERSION=legacy` must remove
that override or change it to `hybrid_v1` to adopt the new default. Changing the
environment variable back to `legacy` affects future sessions only; already
pinned hybrid sessions are not silently downgraded.

Minimal structured policy (no compiler/model call at activation):

```json
{
  "evaluation_policy_version": "hybrid_v1",
  "profile": "balanced",
  "rules": [
    {
      "id": "review-push",
      "effect": "require_approval",
      "when": {"op": "command_matches", "value": "git push*"}
    }
  ]
}
```

Existing `allow_policies`/`deny_policies` text compiles using the configured
conventional analysis LLM, independently of L1 and `ANALYSIS_ENABLED`.
Optional `POLICY_COMPILER_LLM_MODEL`, `POLICY_COMPILER_LLM_BASE_URL`,
`POLICY_COMPILER_LLM_API_KEY`, `POLICY_COMPILER_LLM_REASONING_EFFORT` and
`POLICY_COMPILER_LLM_TIMEOUT_MS` override the corresponding `ANALYSIS_LLM_*`
settings/defaults. For the 120b/low benchmark, set `ANALYSIS_LLM_MODEL` to
`openai/gpt-oss-120b` and `ANALYSIS_LLM_REASONING_EFFORT=low`, or use explicit
compiler overrides. Analysis retains its existing provider/key fallback to L1.

With `COGNIS_INFERENCE_URL` and `COGNIS_INFERENCE_TOKEN_FILE`, compilation uses
the existing `routes/intaris_analysis` route by default; an explicit
`POLICY_COMPILER_LLM_MODEL` may select another configured managed route.
Managed endpoint and lazy service credentials remain authoritative over direct
base/key overrides. No new Cognis route or client API field is required.
Compiler clients are created only on text compilation cache misses and closed
after use. Missing credentials fail text activation with a non-executable 503;
ready structured policies do not initialize or require a compiler.
Injected compiler clients supply their own effective routing identity; cache
keys include that identity and compiler/schema versions, never inferred L1 defaults.

Compiler and Judge are not routed to Jev. `EVALUATOR_BACKEND`
selects the L1 backend as before. Enable `policy.intention_gate=jev` only when
Jev is configured; unavailable/uncertain gates follow the normal refresh path.
Never send server-generated `active_policy`/`policy_hash` as authoring fields.
On compiler failure, correct the rejected clause and resubmit the policy; do not
continue assuming that the requested update became active.

Session GET and call audit expose active snapshot/revision and assessment-stage
metadata for diagnosis. An ordinary `context` value is always evidence, not a
permission override. The standalone UI can continue managing sessions and human
resolutions; richer profile UI is not required for deployment or API use.

## Acceptance boundaries

Old request fixtures run under both server defaults, using API keys, old JWT,
REST/native MCP and OpenCode/Claude hooks. Additional coverage includes compiler
effect/exception fidelity and atomic rejection, restart/cache isolation, all
outcome-floor combinations, both transaction race orders, stale Judge/approval,
provenance forgery, original/compacted arguments and fake-upstream invocation
counts after response loss. Benchmark files remain separately owned.
