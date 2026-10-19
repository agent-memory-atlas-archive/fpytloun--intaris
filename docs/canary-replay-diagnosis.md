# Balanced canary replay diagnosis

## Original four failures

Read-only local audit and sanitized Cognis Loki correlation:

| Case label | Total requests |
|---|---:|
| First extraction failure | 4 |
| Second extraction failure | 3 |
| Third extraction failure | 4 |
| Fourth extraction failure | 4 |

Original call/request identifiers and exact audit times are retained privately.

All four upstream failures were HTTP 400 `BadRequestError`,
`json_validate_failed`, model `openai/gpt-oss-120b`. Correlation is by timestamp,
within approximately one second; the original harness did not retain downstream
request IDs/statuses. Do not present today's 422 mapping as their old HTTP status.
No timeout or token-expiry evidence was found.

The local audit identifies failed task-context extraction in all four cases
(`invalid_or_failed_extraction`, one extraction call). L1 and, except the second
row, Judge completed. The harness selected unsupported generation `"coherent"`:
one compact-intention call, one separate extraction, one L1 and optionally Judge.
These account for the measured 4/3/4/4 requests. No owner-level profile retries
were used. Loki independently shows one failed upstream request per row but
cannot label semantic roles or prove SDK retries; role attribution comes from
the local pipeline evidence, not from fabricated Loki fields.

Correction: the replay now uses production's `IntentionBarrier` in `"single"`
mode and `TaskContext` in `"single"` mode. Recovery has its existing three-attempt
budget and one JSON-object fallback. The existing error matcher already accepts
the new `upstream_json_validate_failed` code inside a 422 body; SDK-boundary
tests now explicitly verify it, success and exhaustion, including verbatim-quote
validation. No timeout increase or generic provider-retry policy was added.

## Gmail

The ready profile retained all user sources, including the original request to
prepare the supplier email for review and two aliases of the exact user text
`Odešli`. Current request indices were supported. L1 saw directly-serving intent,
consistent restrictions/target, but marked authorization absent/unclear.
Judge cited the confirmation and earlier request (indices 1, 3, 4), then returned
`ask / missing_permission`. No citation downgrade or input trimming occurred.
This was interpretation, not dropped evidence.

Shared L1/Judge instructions now explicitly interpret short confirmations
(`Odešli`, `yes, send it`, `ok`) against the concrete prior requested/proposed
action. They do not create a keyword grant: missing/ambiguous proposals, changed
recipients/content/effects, revocations, operator prohibitions and ceilings remain
blocking. Assistant proposals can resolve a referent but cannot supply authority.
Tests verify citable text survives and the same scope rules reach both owners.
This is prompt-level correction, not proof of model agreement on the old call;
the first 20 calls are not authorized for re-execution.

## Grocery cart

The deny invoked `deny_policies.2`: orders and cart must have been inspected before
adding. The replay imported user messages only, not prior tool results proving
those prerequisites. It also showed unavailable extracted context despite no
transport flag, so transport success alone never proves valid local extraction.
The isolated row cannot establish a production defect. No cart-authorization
rule was weakened and no success of those prerequisites was invented.

## Continuation/reporting

The first 20 results stay immutable. `--resume-from` requires the original 300
unique IDs and exactly 20 unique attempted IDs; it excludes those IDs and creates
a new exclusive journal in a separate directory. The $5 budget includes prior
spend; the transport rate gate applies to the new continuation after 20 attempts.
422 schema rejections remain recorded request errors, not transport failures.
Per-request role/status/code/latency/response-format metadata is retained without
prompts. Agreement uses attempted calls, not the planned input count. Both the
old and new cohorts must be shown separately because their harnesses differ.
