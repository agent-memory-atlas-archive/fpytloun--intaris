# RC3 read routing and emergency override

## Advisory invariant change

The a2e870e rule (“every unresolved advisory deny clause forces assessment even
for reads”) is narrowed intentionally. In `balanced` and `bounded_autonomy`,
verified static reads assess only plausibly applicable advisory clauses:
explicit tool references, reading/access restrictions matching a target, or
general sensitive/private-reading prohibitions. Unrelated action prose and
unknown applicability do not become hard gates for otherwise verified reads.
Strict is unchanged. This is not English compilation: bounded compiled deny
rules remain binding and run first. Disclosure-sensitive paths/arguments,
request floors, checkpoints, explicit human vetoes and workflow binding failures
remain protected. No inference may turn a granted fast-path read into an
unattended denial because its `judge_required` flag is false.

The previous contextual obligation remains for assessed actions, not eligible
static reads. Exact observational MCP names (including `list-chats`) have effect
proof; generic MCP infrastructure exemptions and arbitrary `get_*`/`list_*`
names do not. Unknown tools are not whitelisted to improve replay scores.

Git target extraction honors `--`, including option-looking filenames.
Unresolved glob/pathspec expansion is excluded from positive shell read proof.
Existing Git configuration is now trusted under the canonical
[Static Git trust boundary](git-static-trust.md). This supersedes the earlier
requirement to disable existing diff/textconv drivers.

## Operator kill switch

Set `EVALUATION_POLICY_FORCE=legacy`; unset or empty means normal routing.
Unknown nonempty values fail closed. The switch is read at evaluation time
(deployments normally restart processes when changing environment variables).
It makes a private copy of the session policy with legacy routing and never
rewrites the database. Removing it restores the persisted hybrid policy.

Bypassed during evaluation:

- durable hybrid pending/error alignment recovery and the HTTP alignment wait;
- hybrid alignment enforcement;
- declaration matching/non-waivable declaration escalation;
- hybrid task-context/profile extraction and hybrid admission CAS;
- requested hybrid version/revision/hash mismatch checks.

Not bypassed:

- authentication and controller capability validation;
- session and parent lifecycle restrictions;
- legacy classifier deny policies, critical rules, caps and request floors;
- read-only ceilings;
- audit persistence, notifications and structured error logging.

The ordinary intention barrier still waits for user intention updates. New
legacy evaluations use the existing legacy receipt rules. Existing hybrid audit
rows still resolve with their original hybrid binding/ceiling checks; the switch
does not reinterpret historical grants. Read-only rejection still returns
`detail.code=read_only_ceiling`. This switch is an intentional rollback of hybrid
guarantees, not a compatibility mode claiming those guarantees remain enforced.

## Offline production acceptance

Run `uv run --extra dev --extra postgresql python tools/rc3_replay.py
--export-production`. It performs a bounded seven-day PostgreSQL READ ONLY
transaction via the authorized local kubectl context. No provider client is
constructed. Raw redacted arguments exist only in process memory; export.json
retains type shape, derived safety observations and administrative policy/deny
clauses, never user messages, tool output or argument content. Directory mode is
0700 and files 0600. Neither export nor report belongs in Git.

The report lists totals, per-tool results, reason counts and each non-fast
legacy-approved call ID. Historical non-hybrid approved rows are a proxy for
de115a5 because image revision is not stored in audit rows. Current session
policy/user-evidence presence is used, with that limitation explicit. The policy
engine replay is not an HTTP/end-to-end replay, and must not be reported as one.
Writes previously approved by model/human are intentionally not counted as
read fast-path approvals. A target miss must be reported, never repaired by
bypassing risk gates or broadening tools based only on their names.
