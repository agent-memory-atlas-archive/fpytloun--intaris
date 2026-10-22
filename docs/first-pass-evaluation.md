# First-pass source visibility and escalation regression

## Defect and correction

The rich/single L1 projection previously showed extracted task items and a
generated summary while omitting their captured original instruction bodies.
In four inspected production false-conflict cases, the current original user
instruction was absent from the rendered L1 request. Old PLAN/read-only items
remained visible. Judge received the original bodies, so comparing the two
decisions did not compare equal evidence.

`l1_projection.classifier_view` now uses the existing captured-source view in
every mode when originals exist. The existing completeness checks determine
whether redundant generated interpretations can be omitted. Partial originals
retain context and visible gaps. Earlier original instructions, provenance,
reference mapping, action facts, and input-budget limits are preserved.
Generated-only legacy inputs retain their previous path.
The existing source-aware budget registry and citation enum now include these
originals in rich/single mode too. Non-authoritative oversized context can be
bounded without silently discarding original restrictions or action facts.

A second defect was exposed by a negative control: an assessed restriction or
target conflict could disappear when its reference abstained to `none` or did
not resolve. Such a conflict now requires independent review rather than silently
becoming approval. A grounded confirmed restriction retains the existing denial
behavior. Configured restrictions and approval ceilings are unchanged.

Projection version is `action-intention-view-v10`; policy bundle is `builtin-v9`.
Risk thresholds, provider selection and the Judge contract are unchanged.

## Experiment and limits

The finite synthetic corpus is
`tools/benchmark/fixtures/first-pass-v1.json`: 24 cases, 12 authorized and 12
negative controls, with development/holdout labels assigned before inference.
Fixture dates are synthetic sequence markers, not captured event timestamps.
The builder is `tools/benchmark/first_pass.py`.

The baseline primary typed evaluator approved 11/12 authorized cases directly
and retained review for all 12 negative controls. On development cases, the
configured Judge route used as a JSON assessor handled 5/5 positives and 3/3
negatives. These are configured route identities, not independent verification
of the upstream model's size or frontier capability.

A semantic-criteria candidate performed worse (10/12 authorized cases direct,
11/12 negatives retained before correcting the reference hole). It was rejected.
Those holdout cases have now been inspected and are regression cases, not an
untouched future accuracy estimate. A risk-conversion hardening experiment was
also excluded from this source-visibility change.

The final eight inference jobs compared four deliberately selected historical
false conflicts with identical actions and original instruction evidence:

| Input | Old policy outcome | Final policy outcome |
|---|---|---|
| Old rich L1 projection | 0/4 direct approvals | 0/4 direct approvals |
| Originals visible to L1 | 4/4 direct approvals | 3/4 direct approvals |

The remaining case still reported a restriction conflict with no grounding.
The final policy correctly sends that contradiction for review rather than
counting the previous fail-open as a performance improvement.

These live jobs preceded the final source-budget/citation-registration review
correction. The table applies the final policy to their saved assessments; it
is not a live remeasurement of the final complete request. The registration
correction is covered by deterministic wire/budget tests in every projection
mode. No further inference was run after reaching the fixed experiment cap.

This is a 3/4 reduction on a four-case diagnostic sample, **not a claim of 75%
fewer production escalations**. It establishes a source-visibility defect and a
bounded improvement, not general model accuracy or that Judge will be rare on
unseen traffic.

The local experiment cap was 120 recorded jobs: 72 usable classification
records and 48 failed runner-result unpacking jobs. Failed jobs are excluded
from accuracy counts, not from the experiment budget. Full original prompt
fidelity was not asserted. No benchmark action, session resolution, deployment
or runtime policy mutation was executed. Captured user sources and detailed
logs remain private outside the repository.

## Verification

Run the source-visibility, negative-control and policy tests:

```sh
uv run --extra dev --extra postgresql pytest -q \
  tests/test_first_pass_sources.py tests/test_l1_projection.py \
  tests/test_hybrid_contract.py tests/test_hybrid_runtime.py \
  tests/test_raw_projection_budget.py tests/test_instruction_scope_guidance.py
```

Future model comparisons should use fresh independent cases and report both
unnecessary reviews and unsafe approvals. Retain actual source provenance,
whole-action effects and genuine independent restrictions. Do not tune the
policy merely to turn a high-confidence conflict into an approval.

The expanded local run covered 15 owning/adjacent modules: 384 passed, 12 skipped
and 1 deselected. PostgreSQL variants require an isolated
`TEST_HYBRID_POSTGRES_URL`, which was not configured. Two critical-label
immutable-denial assertions in `test_hybrid_review.py` failed on unchanged base
as well as the initial expanded run; they were not modified or counted as passing.
The complete repository suite and production rollout were not performed.
