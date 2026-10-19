# Hybrid evaluation stage separation

The public policy version remains `hybrid_v1`. Legacy remains the default;
this change does not migrate sessions or add another public policy mode.

## Owners

1. **Intention generation** interprets original instructions into a compact rich
   goal, targets and constraints. The single rich generator must refresh these
   facts even when the intention gate would keep the same high-level goal.
2. **L1** classifies the complete effective action: mutation, effects, risk,
   relationship and compatibility with the generated goal/constraints.
   Both JSON and lines use one action/intention view. Neither asks for user
   authorization or emits a permission verdict.
3. **Policy** evaluates observations, model classification, configured rules,
   profile defaults, approvals and review evidence deterministically.
   Alignment is not a permission grant. Authorization predicates are resolved
   by independent review, not additional L1 consent classification.
4. **Judge** independently reviews escalations against originals, refusals and
   complete approval/proposal pairs. A generated intention cannot choose which
   originals the Judge sees. Validated coherent corrections cause policy,
   including configured predicates, to be evaluated again.

## Invariants

- Generated intentions and summaries are not human consent.
- Configured denials/checkpoints, human refusals, action-bound approvals,
  read-only limits, profile behavior and outcome caps remain authoritative.
- An explicitly supplied prohibition with unresolved applicability requires
  review before approval. Absence of authorization alone is not such a
  prohibition.
- A caller with a new original but no rich interpretation owner conservatively
  requires independent review. A failed interpretation retains originals for
  Judge instead of replaying them through L1.
- Known static reads with only independent-review obligations route directly
  to review, without an L1 authority question.
- Original bodies appear once in the Judge packet where safely deduplicated;
  source metadata, chronology, incompleteness and citation identity remain.
  No arbitrary 6K input cap is introduced.
- Source capture has its own bounds. A larger Judge window cannot reconstruct
  uncaptured text. Judge overflow continues to fail closed.
- Judge normally sees escalations only. It cannot repair an incorrectly
  interpreted goal on an action already approved without review.

## Transport limits and diagnostics

The evaluate API retains its existing compact ASCII-escaped serialized
character limits: `args` 65,536 and `context` 8,192. Oversize errors now identify
the field and include exact `size`/`limit` counts. They do not expose bodies.
Cognis omits loaded skills and prior outcomes from the per-call envelope;
canonical execution outcomes remain available independently to Judge.

This is not proof of the historical Outlook 422's exact cause. Its exact
rejected envelope was unavailable. It removes unnecessary envelope content and
makes any remaining size failure diagnosable without logging private content.

## Validation and later comparison

Local regressions use mocked/offline model clients. They exercise selective
chat/Jev payloads, rich generation, deterministic policy, original-source Judge
review, approval binding, freshness, finalization and HTTP source lifecycle.
They establish software contracts, not comparative model quality or latency.

After architecture acceptance, freeze the pre-change private release
`a92f0a2ba2fcfc4ddb2053e4d77261b2fb9416e2`, the corrected revision and legacy.
Use the existing real-record replay as the primary matched comparison and the
benchmark for broader/adversarial coverage. Keep cases, original context,
profiles, provider/model configuration and grading identical. Measure unsafe
approvals, unnecessary interruptions, failures, latency and **whole-pipeline**
tokens/cost, including intention generation and Judge. Do not replay mutations.
Paid comparison and deployment are separate authorization steps.

## Verified local handoff

This historical checkpoint covered the Intaris stage-separation implementation
and the corresponding client evaluation-envelope integration. Exact private
development revision and branch identifiers are retained in the private review
record, not published as public source references.

Both isolated worktrees were clean after their implementation commits. No
merge, push, deployment, worker delegation or paid model comparison was made.

Portable equivalent of the executed Intaris acceptance command, run from the
repository root (the original run reused a local virtualenv):

```bash
uv run --extra dev pytest -q -p no:cacheprovider tests/test_stage_separation.py tests/test_hybrid*.py tests/test_policy*.py tests/test_l1_projection.py tests/test_user_simulator.py tests/test_source_capture.py tests/test_input_budget.py tests/test_sanitize.py tests/test_evaluation_context.py tests/test_confirmation_scope.py tests/test_single_fallback_runtime.py tests/test_task_context.py tests/test_raw_history_context.py tests/test_source_context_service.py tests/test_source_context_render_v4.py tests/test_workflow_scope.py tests/test_advisory_policy.py tests/test_intention*.py tests/test_jev*.py -m 'not e2e' --tb=short
```

Result: **861 passed, 147 skipped, 1 deselected**. The selected suites include
SQLite and offline HTTP lifecycle tests; isolated PostgreSQL was not configured.
The 13 new stage-separation tests also passed independently.

Exact Cognis acceptance/type commands, executed from its isolated worktree:

```bash
uv run --python 3.12 --all-packages --extra dev --frozen pytest -q tests/unit/test_guardrails_context.py tests/unit/test_intaris_evaluation.py tests/unit/test_tool_router.py --tb=short
MYPYPATH=packages/common/src:packages/executor/src uv run --python 3.12 --all-packages --extra dev --frozen mypy --explicit-package-bases cognis/core/tool_router.py cognis/providers/guardrails/intaris.py
```

Tests: **218 passed**. Mypy reports two pre-existing optional `async_load`
accesses at `cognis/core/tool_router.py:3479,3519`. Running the same command on
the unchanged client baseline reproduced both errors
at lines `3480,3520`. They were not changed or suppressed.

Diff whitespace checks and task-scoped Ruff check/format checks passed.
Portable equivalents of the commands:

```bash
# Intaris worktree, before its implementation commit:
git diff --check
uv run --extra dev ruff check $(git diff --name-only -- '*.py') tests/test_stage_separation.py
uv run --extra dev ruff format --check $(git diff --name-only -- '*.py') tests/test_stage_separation.py
# Cognis worktree:
git diff --check
uv run --python 3.12 --frozen ruff check cognis/core/tool_router.py cognis/providers/guardrails/intaris.py tests/unit/test_guardrails_context.py tests/unit/test_intaris_evaluation.py
uv run --python 3.12 --frozen ruff format --check cognis/core/tool_router.py cognis/providers/guardrails/intaris.py tests/unit/test_guardrails_context.py tests/unit/test_intaris_evaluation.py
```

Paid/E2E provider checks, production rollout and the matched replay/benchmark
remain unrun. This evidence establishes local software contracts, not model
quality, a production timeout fix, or the historical Outlook envelope's size.
