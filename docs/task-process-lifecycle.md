# Task-managed process lifecycle

An admitted managed background spawn can authorize the lifecycle of that exact
process. The controller must negotiate version 1, sign the declaration with its
ES256 controller capability and receive an acknowledgment. Ownership metadata
alone remains context, not permission.

Intaris validates the current action digest, user, agent and session, then joins
a termination declaration to the audited, approved spawn. The declaration binds
conversation, execution scope, canonical executor, logical spawn call, shell ID
and process incarnation. Cognis supplies fresh authenticated registry evidence;
the executor rechecks the incarnation immediately before signaling.

The policy permission basis is `task_process_lifecycle`, never a fabricated human
approval. Current restrictions, read-only requirements, mandatory checkpoints,
observed critical risks and human refusals remain effective. Normal assessment
still checks current restrictions and risk; it need not infer a separate stop
instruction when lifecycle authority is already established.

Deploy this consumer before enabling the Cognis producer. The producer negotiates
`GET /api/v1/process-lifecycle-capabilities` and records scope only after an
authenticated acknowledgment. Unsupported deployments use ordinary evaluation.
Existing processes without an acknowledged lifecycle admission are not silently
adopted. Restarting a helper under the new contract establishes the new binding.

This replaces the failed prompt-only attempt documented in
`owned-helper-cleanup-regression.md`. It does not turn `bash_kill` into a blanket
allowlist entry, and it does not authorize terminating unrelated external
services.
