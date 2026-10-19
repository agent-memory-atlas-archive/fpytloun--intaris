# Profile policy and contextual Judge

The built-in policy contract is `builtin-v8`. L1 classifies the action; profile
policy decides. Judge is optional and handles exceptions using richer original
context, including correcting mistaken generated intentions and L1 denials.

## Profile defaults

Hard configured denials, explicit checkpoints, read-only constraints, actual
human refusals, observed critical risk and outcome bounds retain precedence.
Model-derived restriction/target/intention conflicts can be reviewed, not silently
discarded. A generated intention is a fallible summary, not a user restriction.

| Situation without an overriding control or concrete conflict | Strict | Balanced | Bounded autonomy |
|---|---|---|---|
| Low risk, weak/unclear/unrelated alignment | Review | Approve | Approve |
| Medium risk, aligned or unclear | Review if scope uncertain | Approve | Approve |
| Medium risk, unrelated | Review | Review | Approve |
| High risk, no scoped authorization | Review | Review | Approve unless a known secret/external/irreversible concern requires review |
| Unknown risk on a mutation | Review | Review | Approve |

Explicit scoped instructions can satisfy risk/scope requirements, but do not
override a configured human checkpoint or hard prohibition. Strict retains
conservative interpretation of uncertain restrictions and consequential effects.
Balanced does not require proof of permission merely because an action writes
or transmits data. Weak alignment is not a clear contradiction.

## One Judge, not a permission-proof pipeline

The Judge receives the actual executing agent and selected profile. Original
user instructions and actual effects take precedence over generated summaries.
Preparing a patch is not deployment; ordinary local tests and private supporting
artifacts do not need a named-tool grant. Restrictions naming a different actor
are not automatically restrictions on the executing actor.

A contextual approval can discharge the model scope/risk issue under review.
It cannot rewrite controller observations or override hard controls. A genuine
human choice remains an ask in interactive profiles; bounded autonomy does not
offer ask as a model disposition. Existing session outcome limits still apply.

There is no second authorization model call and no mandatory quotation match.
Source numbers and quotations are diagnostic aids, not admission prerequisites.
Role-based source provenance remains intact: action arguments and agent reasoning
are not promoted to human instructions. Legacy second-checker receipt fields are
readable for audit compatibility but ignored by the v8 admission policy.

Unavailable model review still remains a retryable service failure when review
is actually needed. Ordinary low-risk calls do not encounter that review path.

## Validation philosophy

Valid approvals are requirements, not secondary conveniences. Compare false
blocks, false approvals, review frequency and latency together. A missed negative
case does not authorize tightening Balanced or autonomy into Strict. Changes to
the profile trade-off require an explicit product decision.

Retain negative observations and budget records. Do not claim offline replay of
captured L1 results verifies new model behavior or production deployment.
