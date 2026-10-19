# L1 presentation-only ablation

Frozen base: `bbef4db6c410ea0abee67cc92cbcaa37c03e016a`.
Only the source-context L1 view changes. Existing opt-in `HYBRID_CONTEXT_MODE`
is unchanged. `context_input.l1_view_version=source-context-l1-view-v4` accompanies
the existing exact L1 prompt digest outside the captured source artifact.
Extraction schema, cache identity and artifact version remain `task-context-v3`.

Presentation order: redacted flat tool/arguments; unchanged trust/contradiction
warnings; grouped selected items and compact intention with its actual origin;
selected/linked metadata plus required finite assessment references; remaining
flat observation values; observation coverage/provenance; effective read-only
ceiling. No classification, policy, routing, effect or selection changes.

Every selected item is retained verbatim with an added original `item_index`;
metadata retains chronology and its original `source_index`. Group membership
does not assert that a statement is current, consistent or permission. Reference
closure follows source scope refs and linked referents transitively. References
without supplied bodies are marked `not_supplied`/`unknown`, never invented
target descriptions. Unused historical metadata is omitted from L1 only.

Judge retains the old view byte-for-byte. Mixed request construction is unchanged.
The published frozen wrapper `tests/source_context_v3_reference.py` supports byte
comparisons and keeps the old render available without modifying another worktree.
Actual mocked GPT/Jev HTTP tests record old/new prompt bytes and action position;
size reduction is measured, not guaranteed for every small artifact. Unit tests
also exercise a full metadata window, unavailable extraction, transitive refs,
contradictions, source choices, coverage, and unchanged artifact contents.

No model calls establish efficacy here. This ablation cannot be claimed to fix
false-positive semantic assessments or routing. Main owns any paid A/B freeze.
