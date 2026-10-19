# Capacity recovery and policy scope

The model transport keeps complete input accounting, the 96 KiB
unknown-capacity fallback, output reservations and protected evidence intact.
When that fallback cannot fit the request, the transport makes one fresh
metadata lookup with a maximum two-second timeout bounded by the remaining
evaluation deadline. It bypasses a cached unknown result and refits the original,
unmodified request using the recovered input/output limits. It does not retry
inference or the proposed tool action. Actual known-limit overflow does not
trigger this recovery. Unavailable or still-insufficient capacity remains closed.
Diagnostics log exception type, HTTP status and timeout, never exception bodies,
URLs, credentials or task content.

Legacy Jev alignment/decision abstention no longer manufactures high operational
risk when the risk axis is confidently classified. Admission still escalates.
An uncertain risk axis retains a conservative high fallback, explicitly marked
in audit metadata. The legacy Judge receives this provenance and independently
assesses risk rather than treating that fallback as observed danger.

Allow-policy semantics have one shared prompt owner for legacy evaluation,
legacy Judge and hybrid Judge. Allowances are not exhaustive allowlists;
non-matching allowances are not implicit denials. Explicit restrictive wording
(`only`, `must not`, `do not`), deny clauses, controller facts and outcome caps
remain binding. Topic keywords and compressed intention omissions do not establish
that public research is unrelated or high risk. Web tools do not bypass checks
for private query disclosure or restricted access.

This change does not change profiles, model routes, session policies, public
policy versions, or unattended `defer -> deny` behavior. It does not re-resolve
historical denials. Offline tests establish routing and provenance invariants;
prompt behavior remains probabilistic and needs production observation.
