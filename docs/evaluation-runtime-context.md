# Evaluation runtime evidence

`context.tool` is the canonical tool-metadata boundary. Intaris normalizes and
redacts description, source, annotations, classification, capabilities and the
selected native operation through `evaluation_context`. Descriptor authority,
schema hash/version and selector remain visible. Full input schemas are excluded.

Metadata describes effects; it never grants permission. External MCP annotations
and classifications are hints, not controller proof. Missing fields are unknown;
`openWorldHint=false` and `read_only=false` do not prove nonexternal behavior.
Valid metadata demotes name-only guesses so models can refine them. Concrete
command effects, secret disclosure, policy denials, human denials, floors, caps
and read-only overlays retain precedence. Metadata-free clients retain fallback.

All L1 projections and both model backends receive the same runtime section.
Hybrid Judge receives the same normalized evidence; legacy evaluation/Judge also
receive it. Loaded skill summaries are context only, not additional grants.

## Raw sources and citations

Raw captured initial assignment and current input are selected independently of
compact extracted items. Source identities, origin, chronology and coverage
travel with each body. The immutable assignment is never backfilled from a
generated summary. Latest-user aliases of a recorded statement share one raw
body, with aliases explicitly listed; they are not independent human grants.

Model projection no longer imposes a routine 24 KB source or 6,000-token Judge
ceiling. Canonical capture still has independent storage bounds such as
`MAX_SOURCE_CHARS`; these are not claims about model capacity. The source section includes later
restrictions, not only favorable extracted statements. Generated summaries,
linked agent context, skill instructions and configured assignments retain their
recorded origin; none become forged human consent.

Every fitting request is preserved after canonical redaction. Only on overflow,
builder-owned context sections can be deduplicated/excerpted. Authoritative
sources, instructions, corrections and controller-linked approval/antecedent
units stay complete. Executable actions never undergo middle truncation.
Context-only excerpts retain head/tail, matching validated passages with nearby
qualifiers, original character ranges and explicit omission markers. Excerpts
remain non-authorizing; the original capture is unchanged. Unknown source
origins and missing section bindings are protected, not guessed trimmable.

Complete original bodies can be cited by L1. Bounded excerpts are contextual
evidence, not complete approval-scope witnesses. Judge still admits citations
through its original source validator and checks availability in the actual
packet. Strict policy and configured-workflow scope semantics remain unchanged.

## Execution context

Recent outcomes join canonical call/result events by exact call ID within the
authenticated tenant/session. The event-store tail includes its unflushed buffer.
Controller context can additionally supply current-turn pending outcomes for the
same tenant/session. Unknown/unrecorded outcomes remain unknown. Successful
approval is not execution success; arbitrary output strings are never parsed for
process identity. Only bounded structured execution metadata is projected.

The snapshot is included in the evaluation context and evidence audit. No new
result store or unbounded tool-output copy is introduced.

## Model-view diagnostics

Builder snapshots retain `evaluation-context-v1`; actual provider-request
diagnostics use `complete-model-input-v1`. They include final messages/schema or
the complete Jev state/questions envelope, actual requested output cap, input
ceiling, estimator, framing, margin, capacity provenance/staleness and omitted
source references/ranges. No second prompt copy or secret-bearing excerpt is
logged. Provider headers and server-internal hidden framing are not observed.

Capacity comes from standard OpenAI-compatible model discovery. The client-local
cache is scoped by endpoint/model/credentials/operation, with 60-second TTL;
transient refresh failures may retain explicitly stale known limits on the same
route for at most 120 seconds from successful discovery. Definitive operation
errors and capacity rejection invalidate it. Concurrent stale probes cannot
overwrite a newer discovery. Probes disable SDK retries and have a
200ms timeout bounded by the remaining evaluation deadline.

Known input ceilings are intersected with total window minus the actual outgoing
output reserve. A known output ceiling also reduces the outgoing output cap.
Accounting uses the rough estimate `ceil(serialized_characters / 3)` plus framing
and an 8% margin (at least 256 tokens), not an exact token count; real JSON-heavy
evaluator packets measured 2.7-3.6 characters per provider token. UTF-8 bytes are
reported separately and still bound the unknown-capacity application fallback.
No tokenizer dependency or runtime download is used.
Jev has no requested output cap; its hidden output overhead stays unknown and
only known input capacity is used. Unknown capacity uses the existing 98,304-byte
application safety ceiling, labelled `UNKNOWN-CAPACITY`, never a guessed token
window. API, storage and security bounds remain independent.

If protected evidence cannot fit, `ContextInsufficientError` fails closed with
code `context_insufficient` (HTTP 413 at `/evaluate`), not invented missing
permission or a missing-session 404. Missing sessions remain 404; malformed legacy
LLM assessments have a distinct `invalid_model_response` error. Judge decision
policy, external authority gates and classifier override rules are unchanged.
