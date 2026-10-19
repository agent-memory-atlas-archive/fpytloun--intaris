# Bounded rich-context recovery

Single-call rich intention generation runs at the existing `IntentionBarrier`,
not inside individual tool evaluations. A provider exception could previously
leave an admitted update without a profile until another eligible user event.

For one admitted single-generation update, the barrier now permits at most three
provider calls: the initial call and retries after 2 and 8 seconds. Only network
errors and HTTP 408, 429, 500, 502, 503, 504 are retryable. Known request/auth
failures and invalid completions are not retried. Generic 502 responses can hide
deterministic upstream failures; the fixed budget contains that uncertainty, not
diagnoses or repairs it.

Retries require the same captured source digest and current generation token.
New user evidence, explicit cancellation or supersession stops recovery; existing
durable publication guards still apply. Duplicate pending triggers for the same
durable source-event sequence join the current generation, including backoff.
Provider work already running in an executor cannot be interrupted, but its
superseded result cannot publish.

The retry budget is per admitted generation, in memory, not a durable job queue.
After exhaustion, a new eligible user event is required to regenerate context.
This does not automatically backfill profiles missing before deployment, resume
recovery after process restart, or establish delegation authority.

Unavailable single-mode context artifacts are not retained in the evaluation
cache. Adapting a profile remains local and makes zero extraction calls per
evaluation. Ready cache keys already include the persisted profile digest.

Health metrics expose `recovery_attempts`, `recovery_exhausted`, and
`recovery_non_retryable`; bounded failure logs contain exception class, HTTP
status and retryability, not provider exception bodies.

## Human-facing review

Review explanations no longer expose internal stage names, basis codes or
citation arrays. Missing verified context is explicitly distinguished from the
user withholding permission. With available context, specific model questions
are retained. Structured audit diagnostics preserve the original model answer,
response origin and reason an unsupported approval was downgraded.

No approval scope, citation trust, outcome caps, human precedence or exact-action
receipt semantics change.

## Outstanding upstream diagnosis

The observed deployment failures were generic HTTP 502 responses from Cognis's
managed `routes/intaris_analysis` route. The deployed non-streaming exception
handler in `cognis/api/routes/inference.py` discards the underlying exception.
Historical root cause remains unknown; an HTTP replay cannot recover that lost
classification.

The Cognis inference owner should add one sanitized error event at that catch
boundary before returning the same generic response. Include only request ID,
exception class, validated integer HTTP status and an allowlisted machine code.
Do not log exception text, traceback, request/prompt/completion, response body,
headers or credentials. Preserve existing credential resolution and provider
routing. The authorized single diagnostic replay has not been consumed by this
patch; run it only once that boundary can safely expose the requested metadata.
