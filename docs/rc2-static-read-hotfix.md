# RC2 static reads, provider recovery and hard ceilings

## Static reads

Previously, `evaluate_policy` added `context.compatibility` for the session
intention before considering the deterministic read grant. The grant then
returned `needs_assessment` while this obligation remained. Shell commands and
metadata tools also lacked mutation proof, leading to permission/target
assessment. This forced routine reads through L1 and sometimes Judge.

Balanced and bounded autonomy now share `read_only_static`: a deterministic
static read with read-only mutation/effects and no outstanding prohibition,
checkpoint, failed assessment, advisory deny clause, or workflow binding issue.
Strict retains its existing policy path. Request floors and atomic exact-action
human veto checks still apply. Disclosure-sensitive paths and secret-bearing
arguments are excluded, using the existing classifier protections.

The legacy local tool allowlist is reused. Three missing names are now explicitly
included: `list_directory` (directory observation), `get_current_datetime` (clock
observation), and `skill_load` (retrieval of configured instructions/tool metadata,
not execution of those tools). No generic MCP exemption becomes mutation proof:
legacy exemptions include stateful operations such as memory writes. `todowrite`
is likewise not effect-free and does not enter this fast path.

## Rich intention recovery

The first provider attempt explicitly requests strict `json_schema`. A recognized
schema rejection, including a forwarded `error.upstream_code=json_validate_failed`
inside an HTTP 502 body, consumes the next existing attempt as `json_object`.
The schema is embedded in the system prompt. There is at most one JSON-object
attempt; failure leaves the profile unavailable. Local schema, source identity,
verbatim quote and integrity validation remain mandatory.

The owner still has its existing three-attempt budget and freshness/cancellation
guards; the provider client has SDK retries disabled for each owner attempt.
Unknown 502s retain bounded transient recovery, not speculative schema fallback.
Metrics record `response_mode` and `max_tokens` without provider bodies.

The previous fixed output cap was 2048 tokens. It is now
`min(8192, max(4096, 2048 + ceil(captured_source_chars / 4)))`.
A 15000-character captured source receives 5798 output tokens; at 16000 it
receives 6048. This includes provider reasoning/output budget where applicable;
it does not prove the reported production failures were truncation.

**Cognis integration requirement:** preserve a sanitized upstream error code in
the HTTP error body (for example `error.upstream_code`), not only internal logs.
An ordinary HTTP 502 without that code cannot be distinguished from transport
failure by Intaris. Tests exercise the real OpenAI SDK boundary with offline HTTP
400 and forwarded-502 responses; no paid provider calls were made.

## Read-only ceilings and questions

An unproven action under a read-only ceiling is denied, not offered as a human
approval. The explanation distinguishes known mutation from absent read-only
proof. Existing resolution guards still return HTTP 409 with
`detail.code=read_only_ceiling`; a human decision cannot remove the ceiling.

Compound shell proof uses a bounded grammar for `;`, `&&`, `||` and pipes. Every
segment must be a recognized observational command. Redirects, substitutions,
backticks, interpreter wrappers, executable filters, unknown commands and
executable/output options are not proof. No shell is executed for classification.
Git long options must be explicitly allowlisted (abbreviated output flags are not
safe). The later accepted [Static Git trust boundary](git-static-trust.md)
supersedes the original requirement to disable existing configured drivers.
Git blob operands
and pathspecs feed path denials; sensitive Git arguments also block static grants.

Judge questions are now templates from structured basis plus bounded tool/target
labels. Model free text cannot attribute a proposal to the user.

## Approval incident: verified production evidence

Read-only PostgreSQL inspection of the affected session found
(production identifiers are retained privately):

- The evaluation was created about 31 seconds before human approval committed.
- Its pre-review binding has state revision 6. Successful human resolution
  increments authorization state once and stores revision 7 in the receipt.
- The current session and receipt both have state revision 7 and policy revision
  3; policy hashes match. This is not evidence of a post-approval activity bump.
- The earlier approved grep had a different argument hash.

The rejected retry has no audit row. Its exact request arguments, mode and
transaction timing cannot be reconstructed from these rows. A race against the
approval commit or mismatched action remains possible, not established. No
speculative receipt weakening or production mutation was performed.
