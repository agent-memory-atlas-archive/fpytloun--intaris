# Evaluation input integrity recovery

This recovery preserves the existing L1 → profile policy → optional Judge
architecture and does not change policy thresholds, allowlists, hard controls,
read-only enforcement or human-decision precedence.

## Complete-request capacity

Raw L1 fallback previously flattened captured sources into numbered text fields
and did not register them with complete-request fitting. The fitter could not
identify eligible non-authoritative context, so otherwise reducible requests
failed the unknown-capacity 96-KiB guard or a discovered token ceiling.

Both JSON and line projections now carry the canonical source envelope. The
classification boundary registers its exact location with the existing fitter.
Only already-eligible non-authoritative bodies can be bounded/omitted, with
coverage markers and diagnostics. Action arguments and original user evidence
remain protected. Oversized protected evidence still produces an explicit
context-insufficient error; the cap is not raised and evidence is not silently
discarded. Projection version is `action-intention-view-v8`.

## Independent contextual review

The L1 instruction previously told the model to compare restrictions against
generated constraints, not reconstructed authority, contradicting its raw-source
fallback and phase-scope guidance. It now uses original evidence and stated scope
to correct stale generated interpretations when those originals are supplied.

Judge no longer repeats the disputed extracted item/status index. When at least
one complete original instruction or human decision is available, its generated
session summary is omitted as a redundant interpretation, with a presentation
marker. Missing history or other truncated/omitted rows must not promote that
summary into citable user authority. The existing summary fallback remains when
no complete original instruction or human decision is available. Original source
bodies, provenance, coverage and genuine restrictions remain unchanged.

This changes model inputs, not permission policy. It does not establish that
every helper operation is valid or override explicit operational restrictions.

## Presentation validation

A real replay response exceeded the previous 200-character clarification limit.
That invalidated the whole answer and triggered another model request. The
bounded user-facing question field now permits 700 characters, matching natural
clarification prose more closely. Decision, risk, basis and source validation
remain unchanged; approvals/refusals still do not display an ask question.

## Evidence and limitations

Private redacted captures and all unsuccessful experiments remain outside the
repository under the inherited release-verification ledger. Thirty-two newly
authorized provider attempts were consumed within the approved $1 limit
(approximately $0.045, subject to the ledger's actual usage). The last
attempt encountered the old question-length validation defect; its retry was
budget-blocked. It is not a semantic acceptance pass.

Earlier paired experiments showed that removing extracted-label anchoring
corrected the commit case and three explicit-prohibition controls, but helper
misclassifications persisted. The failed semantic-rule/provenance candidates
are not merged wholesale. This revision fixes concrete source/fitter/validation
defects; it is not a claim that all real-model false denials are eliminated.
Production observation and further separately authorized inference are still
needed to measure final semantic effectiveness.
