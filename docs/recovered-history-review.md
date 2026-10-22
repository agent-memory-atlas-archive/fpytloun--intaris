# Recovered history and restriction-review provenance

Recovered event envelopes can retain sequence identity while lacking a usable
timestamp. Such events remain available through unfiltered and sequence-based
reads. Time-bounded reads exclude them because their membership in a historical
snapshot cannot be established. Readers never invent timestamps or delete these
events. Persisted filesystem, S3 and buffered reads use the same filter.

A Judge restriction claim, whether refusing or asking, must quote text present in the
cited original instruction or configured clause. Prior automated decisions and
assistant reports are not independent user restrictions. This verifies source
provenance, not the semantic correctness of the model's interpretation.

An unverifiable restriction claim gets the existing single repair attempt. If the repair
still cannot substantiate the restriction, the action remains unresolved for
human review. It does not become an approval, an automatic uncertainty denial,
or an infrastructure error. Actual model/transport failures retain their
separate failure contract.

The pending action is labeled as a review-validation failure, not missing user
permission. The unverified restriction is not repeated as an established fact.
The original risk assessment is preserved; when it is unknown, the explanation
states that it remains unknown rather than implying a validated harmful effect.
A genuine, validated missing-permission question retains its ordinary wording.
No approval or denial is manufactured from a failed review.

Validation: 291 tests passed, 14 skipped across event storage, review behavior,
source integrity, outcome finalization and the dedicated regressions. No paid
model calls or history repair operations were required.

The clarification-provenance and failure-diagnostics correction passed 331 tests
with 78 skipped across review, HTTP integration, source integrity, profile and
outcome-finalization suites. These are offline regression results, not a claim
that all model interpretations are correct.

## Risk abstention recovery and evidence-aware restriction retry

The hybrid risk fallback resolves an abstained label to `medium` when the
posterior is a low/medium split: hazardous (`high` + `critical`) mass below 0.2,
`unknown` mass below 0.5, and `low` + `medium` holding a majority. Diffuse
uncertainty between the two safe labels is label ambiguity, not evidence of
harm. The earlier rule refused whenever `unknown` reached 0.2, which sent routine
local commands to Judge, where they were approved as routine. Hazard mass at the
floor, dominant `unknown` mass, or an arg-max mismatch still keep the abstention.

A recovered label resolves label ambiguity only. Policy treats a recovered risk
as still unassessed whenever the action crosses a consequential boundary
(`requires_explicit_grant`: observed external or irreversible effect,
delete/disclose effects, irreversible reversibility, or a production mutation)
unless a deterministic grant or an action-bound human receipt already covers it.
Controller facts cannot be discharged by a model label.

When a Judge restriction claim fails quote verification, the retry depends on
whether any citable instruction text exists. If citable originals or configured
clauses exist, the restriction basis stays available and the retry names the
exact failure so a paraphrased real limit can be corrected to its verbatim words.
Absence of a restriction cannot be proven from text, so this check fails closed.
Only when nothing citable exists at all does the retry remove
`restriction_conflict` from the response contract; a repeated claim after that is
the same failure and stays pending for human review.

Offline replay of 72 recorded abstention escalations: 57 resolve to direct
approval, 15 remain reviewed, and none of the retained-review set carries a
consequential boundary that reaches approval. These are offline regression
results, not a claim about model interpretations.
