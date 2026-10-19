# Single-call rich intention (experimental hybrid runtime)

Set `SOURCE_CONTEXT_GENERATION=single` and `HYBRID_CONTEXT_MODE=source_context`
to generate title, summary and compact items in one analysis completion.
The existing `spans`, `quotes`, `coherent` modes and default remain unchanged.

Model, endpoint and effort remain configuration:

```sh
ANALYSIS_LLM_MODEL=openai/gpt-oss-20b
ANALYSIS_LLM_REASONING_EFFORT=low
```

The same code supports comparison with `openai/gpt-oss-120b`. No model name
is embedded in the generation implementation.

The completion uses a closed, strict JSON Schema, with no reasoning field.
Single mode makes one provider attempt: no SDK retry, format probing, repair
request or token-budget expansion. If a provider explicitly rejects the
response format, that update fails; the same client uses text JSON on future
updates. Known unsupported providers skip strict format immediately.
Other callers retain the existing LLM client retry and fallback behavior.
The optional Jev intention gate is unchanged and should be unset when measuring
one inference request total.

## Evidence and retention

The required top-level `current` field contains zero-based item indices.
Only validated `request` items with status `current` can be selected. Invalid
indices are dropped and counted, with remapping after invalid items are dropped.
The controller never infers active tasks from all requests in the list.

Items contain `kind`, numbered `msg`, `quote`, `status` and required nullable
`confirmation`. Permission without asking for execution is `approval`, never
`request` or a current task. There are at most
12 items, 240 characters per quote and 2,400 quote characters overall. The
completion ceiling is 2,048 tokens. Strict output requires at least one item
when eligible sources exist; an empty accepted list then reports unavailable,
not ready. Recent reasoning uses a 20-record window;
previous validated items retain their original sources beyond that window.
The controller stores provenance, offsets and scoped decision bindings, not
model-generated source identities.

Quotes must match a unique normalized substring of the captured user message
or final human decision note. Authority selectors must preserve complete
statements, token boundaries and scope; code/JSON, quoted-document blocks,
assistant/tool text and Judge decisions cannot supply them. Invalid selectors
are dropped and counted. These checks prove source binding, not arbitrary
natural-language semantics; the model still interprets meaning.

A bare approval needs a recorded immediately preceding assistant message and:

```json
{"confirmation":{"form":"single_permission","actions":["upload the snapshot after review"]}}
```

The closed `form` enum also includes `alternatives`, `multiple_actions`,
`open_question` and `unknown`. The controller requires `single_permission`,
exactly one action, a unique verbatim token-complete substring (at most 240
characters), and no omitted trailing conditions/actions or punctuated lists.
Missing/malformed proof, other forms, multiple actions or invalid selectors
downgrade the recorded reply to uncertain. Bare replies never become requests.
The validated selector is persisted as `approves`; L1 lines uses it for the
approval's Scope rather than treating "yes" or the entire question as the action.

An unpaired `context` argument, older assistant turn or missing/truncated
antecedent cannot establish the link. Antecedents remain non-authorizing
context, never user quote sources. These structural checks do not prove
arbitrary natural-language action count: the model classifies the whole
question and enumerates actions. No English keyword heuristic or second
model call is used.

The public-history benchmark submits reasoning with `from_events=true` and
empty content after flushing/readback of each event, and checks the exact
event sequence in its audit record. Direct reasoning plus a `context` string
does not prove recorded conversational adjacency.

## Persistence and unavailable behavior

Nullable `sessions.intention_profile` stores the controller-validated profile
atomically with title and intention under the sequence, bootstrap and snapshot
guards. SQLite and PostgreSQL upgrades do not backfill old sessions.
No new mandatory API, hook or MCP fields are required.

TaskContext makes zero LLM requests in this mode. It checks the summary binding,
latest-user and human-decision freshness, retained original evidence, and
profile integrity before adapting items to the existing render shape.
When no eligible user message or nonempty recorded human note exists,
TaskContext reports `not_required` and L1 uses the unchanged production mixed
builder, including the current session intention, context and parent scope.
That builder currently uses legacy JSON even when `lines` is requested; the
fallback deliberately preserves the same routing and bytes as mixed mode.
It does not attach an unavailable rich artifact to downstream review.
The captured fallback boundary participates in the source digest and retains
source-change guards. An observed-user marker or retained nonempty profile
also requires rich evidence even when its source cannot be read.

Once rich evidence is expected, missing, stale, malformed or empty validated
profiles report unavailable, never mixed fallback or stale grants.
Malformed completion JSON retains the existing summary parser but invalidates
the profile. Historical statuses remain visible; interpreted items never
establish permission by themselves.
Version 1/2 profiles remain bounded retention input, but are unavailable to
rendering until a version 3 update supplies the new proof contract. Old bare
approvals without proof become uncertain in that retention input.

## Measurement

Each single generation logs `Intention generation metrics` with model,
prompt/completion token usage, latency, call count and prompt character count.
Successful `intention_updated` lifecycle events include the same `generation`
metrics plus profile validation counts. Missing provider usage is null,
never estimated. Context-local metrics prevent concurrent sessions from
sharing another call's usage.
Per-item drops persist and log only zero-based item indices and finite reason
codes (for example `partial_statement`, `embedded_non_authorizing_data`,
`invalid_source_reference`, `non_user_source`), never source content or
Pydantic error messages.

Offline prompt-size comparisons:

```sh
uv run --extra dev pytest tests/test_single_intention.py -k prompt_size -s
```

The output reports exact old/new character counts and serialized schema size.
Characters divided by four are only rough token estimates, not model-specific
tokenization or paid-call quality/latency evidence.

Measured paired fixtures against the previous single-call commit `c7c60f6`
(same synthetic snapshot, no provider calls):

| Fixture | c7c60f6 total chars | Current total chars | Old/new user chars |
| --- | ---: | ---: | ---: |
| One short request | 1,785 | 1,884 | 300 / 149 |
| Full 20-record window | 6,992 | 6,351 | 5,507 / 4,616 |
| Twelve retained restrictions beyond the window | 8,062 | 7,820 | 6,577 / 6,085 |

The system prompt is 1,735 characters (previously 1,485); serialized strict
schema size is 1,772 (967 at `c7c60f6`; 1,089 at integration base `e041494`),
separate from the totals above. The title clarification adds 17 characters
relative to the confirmation fix; twelve retained null proofs add 240
characters to that payload. The completion ceiling remains unchanged.
The shortened payload removes generated-summary duplication, null fields and
ordinary false flags. Provider token usage remains the billing measure.
These offline tests prove the protocol and wiring, not a 20B quality gate.
