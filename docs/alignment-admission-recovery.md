# Alignment admission and complete-input recovery

The existing first classifier and independent reviewer now receive the same
original parent/child assignments and user updates. Child instructions remain
parent-agent evidence, not human authorization. No hook exemption is added.

An unavailable review is not a negative verdict. Hybrid admission persists the
existing `error` state and returns HTTP 503, code `alignment_unavailable`,
`execution_state=not_dispatched`, `retryable=true`, rather than a fabricated
conflict requiring human consent. Genuine negative verdicts retain the existing
alignment acknowledgment path. Legacy standalone fail-open behavior is unchanged.

An additive, nullable `sessions.alignment_reason` column retains the admission
reason across worker restarts. Reason and state are updated atomically with the
existing tenant and state-revision checks. A fresh pending review clears the old
reason. Hybrid enforcement reads this durable state, not a stale cached negative
from an earlier review. Successful re-evaluation therefore cannot remain blocked
by an obsolete in-memory result.

Raw L1 fallback previously repeated original instruction bodies alongside
generated summaries, extracted quotes, and history. Even after contextual
fitting, this duplication could exceed the unknown-byte or known-token limit.
When captured originals are complete, projection v9 removes only those derived
duplicates. Original instructions, chronology, restrictions and executable action
arguments are unchanged. Missing/truncated originals retain the generated
fallback. Irreducibly oversized protected evidence still fails closed: no cap is
raised, no user instruction is silently truncated.

Regression tests cover first-classifier HTTP source transport, reviewer failure
and recovery, stale cached conflict recovery, durable reasons and tenant/revision
isolation, additive migration, and overflowing-before/fitting-after requests at
unknown 96-KiB and known 32K-token ceilings in both projections.

No paid inference was run for this change: the previously authorized attempt
budget was exhausted. These are integration and deterministic regression checks,
not proof of perfect live-model interpretation. Existing admission conflicts are
not automatically approved or rewritten by deployment. Missing historical
reasons cannot be reconstructed as facts.
