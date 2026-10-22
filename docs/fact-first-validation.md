# Fact-first policy: implementation and measured limits

## Scope

The implementation follows the existing layer owners:

- `policy_assessment`: profile-blind, permission-free classification definitions,
  with an optional verbatim restriction quote for JSON providers.
- `policy_evidence`: instruction-source verification and immutable fact
  precedence; grounding does not establish semantic applicability.
- `hybrid`: deterministic profile-dependent treatment of task/alignment
  opinions, with explicit restrictions and hazardous effects kept reviewable.
- `alignment` / `evaluator`: compatibility classification becomes action-policy
  evidence. Original parent words, not generated summaries, ground citations.
  Compatibility approval does not become a permanent hybrid-session grant.
- `judge_simulator`: ordinary residual review and safe content-free rejection
  reason codes. Judge cannot override immutable controller facts.

The rich intention generator already supplies source references, verbatim item
quotes and temporal status. This change consumes that metadata; it does not
introduce a second intention generator or remove captured-original visibility.

## Recorded-case offline replay

Run:

```sh
uv run --extra dev python -m tools.benchmark.fact_first CAPTURE --baseline 9465d59
```

`CAPTURE` is a private audit capture, not a committed fixture. The baseline
uses captured evidence; the candidate reconstructs its current source-item
registration from the same originals. No action or inference is executed.
Stored L1 classifications are held fixed, so these are policy counterfactuals,
not measurements of new classifier accuracy or production latency.

| Dataset/profile | Baseline escalations | Candidate escalations | Avoided |
|---|---:|---:|---:|
| Recent 13 action evaluations, strict | 13 | 13 | 0 |
| Recent 13 action evaluations, balanced | 13 | 12 | 1 |
| Recent 13 action evaluations, bounded autonomy | 8 | 7 | 1 |
| Earlier 306 action evaluations, strict | 306 | 306 | 0 |
| Earlier 306 action evaluations, balanced | 306 | 288 | 18 |
| Earlier 306 action evaluations, bounded autonomy | 197 | 171 | 26 |

The recent dataset also contains two historical alignment-gate records, and the
earlier dataset nine records without action-policy evidence. They are excluded,
not invented as action evaluations or counted as approvals. Compatibility
behavior has dedicated policy, restart, pending/error, citation and actual HTTP
handler tests.

These are selected intervention cohorts, not denominators for estimating
production escalation rates. Human/Judge approval is not used as ground truth.
Genuinely protected-deployment and credential-uncertainty controls remain
reviewable. Explicit-restriction false positives are not declared solved.

## Bounded live classification

Exactly 24 live typed classifications were run on the existing synthetic
regression corpus (12 authorized, 12 prohibited controls). Only the assessor was
called: none of the proposed commands ran and no service configuration changed.
The same classification output is assessed under both profiles.

An initial rule that ignored ungrounded explicit-restriction findings approved
two prohibited controls. That rule was rejected. The accepted policy preserves
review of every asserted explicit instruction violation even without a usable
citation. Replaying the 24 saved live outputs through the accepted policy gives:

| Profile | Authorized direct approvals | Prohibited controls retained |
|---|---:|---:|
| Balanced | 11 / 12 | 12 / 12 |
| Bounded autonomy | 12 / 12 | 12 / 12 |

This is final-policy replay of live classifications, not a second fresh live
run or an untouched holdout. The corpus has been inspected during development;
it proves concrete regression behavior, not a general zero-unsafe-approval rate.
No reduction in actual user-question count or Judge-model latency was measured
by this run, and no new cheap-versus-frontier capability claim is made.

## Safety boundaries

- Missing citation is not proof that an explicit prohibition does not exist.
- A local path is not an owned worktree; ownership is not recoverability.
- A bare observed target does not establish a violated boundary.
- Generated parent text and tool output cannot become original instructions.
- An action approval cannot silently authorize every future child-session action.
- Provider choice/distribution mismatches, unknown-dominant risk and combined
  hazardous risk mass cannot be recovered as known medium risk.

## Verification commands

Focused policy/alignment/schema/runtime checks:

```sh
uv run --extra dev --extra postgresql pytest -q \
  tests/test_fact_first_policy.py tests/test_alignment_admission_integrity.py \
  tests/test_alignment.py tests/test_alignment_compatibility_contract.py \
  tests/test_hybrid_runtime.py tests/test_hybrid_contract.py \
  tests/test_intent_first_defaults.py tests/test_policy_evidence_runtime.py \
  tests/test_policy_finalization.py tests/test_jev.py
uv run --extra dev ruff check intaris tests/test_fact_first_policy.py \
  tools/benchmark/fact_first.py
```

Full primary-suite verification is `pytest -q tests`, separately from integration
packages with overlapping import names. Existing opt-in PostgreSQL and external
integration gates are not disabled or rewritten to produce a green result.
Any remaining failures must be reported against the unchanged-base comparison.

Final verification:

- Main acceptance run (the 15 owning/HTTP modules listed in the implementation
  log): **783 passed, 75 skipped, 1 deselected, 2 failed**.
- Additional Judge correction, input integrity, advisory, policy table,
  user-simulator and source-budget modules: **165 passed**.
- Both remaining failures are analysis-task queue endpoint assertions. Running
  those exact two cases on unchanged base reproduced both failures with the
  same actual/expected values. They were not changed or counted as passing.
- The exhaustive candidate/base `tests` runs were interrupted after about nine
  minutes of slow service/transport retries. They are not full-suite acceptance.
- PostgreSQL cases require the existing isolated database configuration; no
  opt-in gates were bypassed.
- Changed Python sources/tests and offline replay tooling pass Ruff and
  `git diff --check`.
- Independent code review approved the final correction, including HTTP
  no-wait integration and preserved not-found translation.

The change is based on `9465d59`. Later upstream image-publication/CI commits
were observed but not merged into this isolated task branch. No production
profile, provider, deployment or stored approval was changed by this work.
