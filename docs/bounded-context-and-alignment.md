# Bounded evaluator context and alignment review

Oversized inference inputs first use the existing source-aware excerpts. If that
is insufficient, dynamic text uses a bounded middle excerpt retaining its start
and end, an omission marker, original length and SHA-256 digest. Large ordinary
lists and mappings retain their ends with an explicit middle-omission marker.
The fitter measures the complete request after each bounded projection against
the discovered token budget or the configured unknown-capacity byte limit.

This changes only the model view. Original source records, action arguments,
action identity and audit evidence remain complete. Evaluation instructions,
response schemas and citation/source identities are not silently rewritten.
Irreducible protocol or identity overhead still fails closed.

Partial views mark source completeness and grant eligibility explicitly.
Neither an explicit-request nor a scoped-approval verdict may establish an
affirmative grant from a partial view. Citation identities remain stable; an
omitted proposal restriction cannot turn a short linked “yes” into permission
for a different scope. Ordinary task-support judgments remain distinct from
affirmative authorization claims.

When Judge is enabled, the existing independent alignment review uses the
configured Judge model. A negative first-level alignment result is advisory
until that review finishes. Judge may confirm or correct it using the original
parent and child assignment context; failure or invalid output remains
unavailable, not permission. Positive first-level results do not add an extra
model call. Disabling Judge retains the prior independent analysis-model path.

This does not migrate or fabricate historical approvals. Previously persisted
alignment results change only through the existing alignment recheck lifecycle.
Tenant, policy, parent lifecycle and exact-action admission checks still apply.
