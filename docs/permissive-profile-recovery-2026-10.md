# Permissive profile recovery

This supersedes the admission design in the October 15–16 release-preparation
reports. Those reports remain historical evidence, not the current policy
contract. See [profile policy and contextual Judge](judge-scope-authority.md).

## Removed

- Mandatory independent review of every consequential transmission or
  uncertain-scope mutation in Balanced and bounded autonomy.
- The second authorization model, quotation validation and repair loop.
- Policy dependence on second-checker routine-support receipts.
- Mandatory verbatim restriction quotations in contextual Judge responses.

## Retained

- Source capture, original-role provenance, raw-context fallback and deadlines.
- Action-local approvals, concurrent resolution and freshness isolation.
- Authenticated process admission, immutable incarnation and kill-time checks.
- Explicit deny/checkpoint rules, read-only requirements, real human refusals,
  critical observations and outcome bounds.
- Budget accounting, unsuccessful observations and benchmark infrastructure.

Strict retains conservative scope review. Balanced admits low-risk work despite
weak/unclear/unrelated alignment and medium-risk supporting work without a
separate tool grant. Autonomy defaults to admission except concrete conflicts,
critical risk or consequential high-risk observations requiring review.
Judge remains optional and can correct model denials and generated intention.
Actual actor identity is supplied to L1 and Judge; original instructions take
precedence over generated summaries.

## Offline production-snapshot replay

Window: 2026-10-16 23:39:53 UTC to 2026-10-17 06:03:45 UTC.
The replay removes prior Judge receipts and evaluates the captured original L1
classification under the new deterministic policy. It dispatches no tools,
uses no inference and modifies no production decisions.

- 356 captured calls; 306 have policy snapshots, 50 do not.
- Previously: 197 direct approvals and 159 initial escalations.
- Snapshot replay: 246 approvals, 38 reviews and 22 needing fresh L1 assessment.
- Of the 159 original escalations, 157 have replayable evidence: 120 now approve
  directly and 37 still require review; two lack snapshots.
- An earlier 335/19 summary was overstated and is withdrawn. Do not treat
  missing snapshots or cached paths as validated new-policy approvals.
- Remaining review reasons include concrete model restriction/intention/target
  conflicts, high/unknown risk and unrelated medium-risk actions.

This compares policy outcomes, not the complete admission wrapper, cached
approvals or session outcome bounds. One formerly approved record has unknown
L1 risk and requires review at this policy boundary. This demonstrates reduced
mandatory review, not correctness of a new model classification. It does not
prove all remaining conflicts are real. In
particular actor-aware model behavior needs post-rollout observation.

Negative fixtures are retained, including missed token-disclosure classification.
Permissive profiles deliberately do not convert every ambiguous read into Strict
to cover that model mistake. Actual controller/explicit restrictions still have
separate negative regression coverage. This trade-off is a product requirement.

## Executor rollout prerequisite

Executors running older builds may lack the admission IDs and immutable
incarnation required by the existing lifecycle protocol. Such processes cannot
obtain the authenticated lifecycle shortcut, even for same-session tests.

The lifecycle implementation is retained, not replaced by a blanket kill
allowlist. Rollout must upgrade affected executors to a build containing
the lifecycle implementation and verify a fresh admitted process can be managed
and stopped by its owning agent. Old processes cannot acquire a fabricated
admission/incarnation retroactively. An executor restart may affect active
sessions and requires coordinated approval.

## Verification boundaries

Independent review approved the runtime recovery and its Strict/actor-format
correction. Review identifiers are retained privately.

Validation:

- Focused profile/actor/Judge/cache suite: 296 passed, 133 skipped.
- Full joined `pytest tests/ -q --tb=short` reached the 93% progress band without
  reported failures, then hit the 900-second execution limit. This is not
  reported as a single successful full-suite run.
- The remaining modules, starting at `test_static_read_parity.py` and including
  task-context, source-fidelity, transport, simulator, webhook and workflow-scope
  tests, were then run separately: 277 passed, 111 skipped.
- Changed-file Ruff check/format and `git diff --check` passed.

No production deployment, executor upgrade, human approval or paid model replay
is implied by this document.
