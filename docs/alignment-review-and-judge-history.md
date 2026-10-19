# Independent alignment review and legacy Judge history

## Scope admission

Jev is a factual alignment classifier, not the final scope authority. A positive
result uses the existing admission path. Negative and below-confidence results
go to the existing analysis LLM for independent compatibility review before a
child is blocked. Low confidence is represented as abstention, not misalignment.

The barrier supplies both original assignments and latest captured user evidence.
Later human instructions may supersede an earlier parent assignment. Child
messages remain parent-agent instructions and cannot broaden human scope.
Compatible admission grants no particular action permission: ordinary action
evaluation, explicit policy controls, refusals and outcome caps still apply.

If independent review is unavailable, typed-classifier admission does not fail
open. Hybrid sessions remain pending; legacy sessions require human alignment
approval. The standalone LLM-only legacy failure behavior is unchanged.
Startup rechecks active, non-overridden children through this same path; it does
not override completed sessions, historical denials or human decisions.
The synchronous hybrid admission retry uses this same barrier-owned reviewer
and evidence assembly, never the ordinary action classifier. Without a configured
reviewer, it leaves the gate closed. Source evidence is boundary-wrapped data.

## Legacy Judge input

The ordinary and recent-Judge history sections now use the existing compact
history renderer, including its attached-evaluate-context deduplication.
Ordinary prior arguments are bounded. Arguments associated with human decisions
remain complete because approval/refusal scope can occur anywhere in them.
Automatic Judge resolutions also set `user_decision`; their `resolved_by=judge`
provenance makes them ordinary history, not human scope. Unattributed legacy
decisions retain complete arguments conservatively. Ordinary argument rendering
has a 2,000-character aggregate cap with a visible omission marker, including
nested batch arguments.

The current action, original source evidence, user refusals, human notes and
human proposal/action scope are not truncated by this change. Existing capacity
checks still reject protected evidence that cannot fit; this is not a guarantee
that every possible evidence packet fits a provider's context limit.

Regression coverage includes both legacy history sections, complete current
actions and human decision arguments, negative/uncertain independent review,
original assignments and later updates, durable hybrid admission, and unavailable
review. No model settings, policy defaults or public API version change.
