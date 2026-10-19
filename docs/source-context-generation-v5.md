# Source-generation ablations

Requires `HYBRID_CONTEXT_MODE=source_context`. Default `SOURCE_CONTEXT_GENERATION=spans`
retains qualified v3 extraction and v4 presentation. Ablation A sets `quotes`:
`task-context-quotes-v1`, unique exact source quotes with controller-calculated
offsets and copied recorded scopes. Dynamic eligible-source enums and strict
closed schemas use the existing analysis client. No new model scope IDs, target
strings, grants, policy, routing or renderer changes.

Invalid/repeated/absent/token-cut quotes fail extraction explicitly. A valid
word boundary is not proof of complete qualifiers or semantic entailment.
Source refs vary by snapshot, so wire hashes vary with the actual inventory.
Versions remain in source/cache identity. Originals, provenance, decision
watermarks and prior v4 fallback semantics are unchanged in A.

## B: coherent source generation

`SOURCE_CONTEXT_GENERATION=coherent` selects `task-context-generation-v5`.
It includes A's quote selectors and adds current_request(status,item_indices)
plus temporal_status, polarity, reference_resolution, relation and
related_source_refs to each interpreted item. All are model interpretations,
not actual resolutions or permissions. Every selected historical restriction
remains visible. Scope refs are copied from recorded evidence, never generated.
Current request cannot cite context-only, non-current, unknown or truncated
items. Empty/missing current requests remain unknown, including extractor failure.

The immutable nullable `sessions.original_assignment` is populated only by
SessionStore.create. Both database migrations leave older rows null; generated
text is never backfilled. A still-initial current intention can serve as existing
assignment evidence. Other absent originals are declared unavailable. Generated
legacy intention is excluded from generation and L1's current-task/fallback view;
it is available to Judge only as clearly labelled diagnostic context. Current
initial/manual assignment semantics remain source-provenance dependent; the
public session-update API does not accept a rewritten intention. User messages
continue through the unchanged intention barrier and captured source records.

B caps individual source bodies at 2,000 characters under the existing 24k
budget and bounded record window; omitted bodies/history remain explicit. No
cached interpreted approval substitutes for an unavailable original. Missing
or ambiguous approval referents cannot support explicit confirmation. Direct
approval interpretation and complete semantic qualifiers remain live-model QA
requirements: word boundaries and linked IDs alone do not prove either.

## Offline evidence and limits

Eight-history tests use the unchanged external SOURCE_HISTORY_FIXTURE, copying
only raw event text/actors and real decision/action bindings into actual public
event/reasoning/evaluate/decision lifecycle calls. Oracle items, titles, scopes
and expectations are not provider inputs. Echo providers return context/unknown,
not ideal goals. This validates collection, schema, references, immutable sources,
actor identity, barrier/watermarks and capture persistence—not semantic accuracy.
Both GPT/Jev transports are intercepted locally; no paid requests or execution.
The raw fixture is read-only and is not modified or bundled into a new benchmark.

The existing v4 action-first layout stays. B adds its current-request view and
diagnostic-only legacy summary for Judge; A's layout is unchanged. Extraction
version is part of source/cache identity. Snapshot-specific schema hashes include
the finite source inventory; compare only with that inventory held constant.
All policy, classifier, threshold, mode-floor, activation and routing code remains
unchanged. Real eight-story quality, original47/Git/action controls and human QA
are acceptance gates owned by main, not conclusions drawn from these mocks.
