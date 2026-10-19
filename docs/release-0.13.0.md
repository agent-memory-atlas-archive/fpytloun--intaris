# Intaris 0.13.0

Release date: 2026-10-19.

This release includes the hybrid evaluation, Judge, bounded source-context,
delegated-policy, queue recovery and ingestion corrections described in the
changelog. It supersedes the earlier release-preparation holds for publication;
historical verification reports remain records of their original checkpoints,
not current release-status files or claims of complete model accuracy.

## Validation and limitations

- The last complete offline suite passed **4,423 tests**, with **512 skipped**
  and **two warnings**, before the later source-budget, queue and Judge changes.
- Later changes received focused regression checks and independent review.
  Evidence includes 608 Judge/classifier/policy checks, 291 context-budget and
  alignment checks, and 51 instruction-chronology/source-integrity checks.
  These are different runs and are not added together as one denominator.
- The final combined release tree was not given another full-suite run. Adding
  tests and repeating that run were explicitly waived at finalization. Existing
  skipped checks and historical failures are not relabeled as passes.
- No fresh paid model benchmark was performed for the final tree. Published
  historical reports retain their original unsafe observations, unresolved
  fixture-label limitations and cohort differences. They are not evidence that
  all semantic false approvals or false escalations are eliminated.
- The public-content audit covers runtime sources, documentation, integration
  code, fixture/tool corpora and all ten images. Remaining operational fixture
  identifiers were reconstructed consistently; original audit evidence is kept
  outside the public tree. Reworded examples do not inherit old model results.
- Private CI configuration under `.gitea/` is excluded from public history and
  release source archives. Public source is curated, not a mirror of private
  development history.

The package remains pre-alpha. Deploy with explicit restrictions and normal
approval controls appropriate to the installation. Judge review is not a
replacement for tenant isolation, exact-action freshness or human refusals.
