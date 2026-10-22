# Inference accounting attribution

Use `intaris.inference_attribution.llm_attribution(operation=...,
session_id=None, task_id=None)` around one logical operation **inside its
synchronous worker**. The yielded immutable scope has `operation_id`, suitable
for analysis evidence metadata. Keep the scope around validation and transport
retries. A fresh scope identifies a new logical operation, not an idempotency key.

Allowed operations: `summary_window`, `compaction`, `l3`, `intention`,
`source_context`, `policy_compile`, `policy_assess`, `alignment`, `evaluation`,
`judge`. Nested scopes inherit available session/task IDs, not operation IDs.
IDs must use 1–128 ASCII letters, digits, underscores, dots, colons or hyphens.
Unsupported correlation values are omitted rather than failing inference.
Never put prompts, titles, user identities or other content in attribution.

The central LLM client sends optional `X-Cognis-Inference-Operation`,
`Operation-ID`, `Session-ID`, `Task-ID` and `Client-Request-ID` headers
(each with the same `X-Cognis-Inference-` prefix) only to the explicitly
configured managed endpoint. Each HTTP submission gets a unique client request
ID; retries retain the enclosing operation ID. Standalone providers receive no
attribution. These headers do not alter authentication, route affinity, model
parameters or retry policy. Old Cognis servers ignore them; new servers must
continue accepting requests without them.

Intention, source extraction, policy compilation/assessment and alignment owners
establish their own scopes. Session/task IDs unavailable at those boundaries are
omitted unless inherited. Analytics, evaluator and Judge owners must scope their
own operations. ContextVars isolate concurrent execution; `run_in_executor` does
not copy a caller's context, so establish the scope in the worker, not around
thread submission.

Attribution is observational, not proof of execution or billing. Unknown failed
request usage and invisible provider retries cannot be assumed free. Token-price
estimates are not provider invoices.
