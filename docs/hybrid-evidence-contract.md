# Evidence-driven hybrid experiment

Public activation remains `hybrid_v1`. Internal versions are `builtin-v4` and
`evidence-v1`. English compilation and optional intent refresh remain frozen.

`intaris.hybrid.evaluate_policy(rules, Evidence, profile)` is the sole table.
`Evidence` separates observations (value/source/coverage/engine references),
model assessments, supplied sources, controller review receipt, scoped human
approval and read-only ceiling. References validate identity, not semantic truth.

`PolicyResult` contains outcome, matched/unresolved rules, mandatory floors,
constraint matches, permission basis, evidence quality and
`outstanding_obligations`. Each obligation names its basis, finite fields,
available references, assessment/review/human requirement and protection.

L1 receives only requested fields. New finite fields:
`consequence=routine|consequential|unclear`,
`contextual_restrictions=consistent|conflicts|unresolved`,
`target_scope=consistent|conflicts|unresolved`, plus references restricted to the
supplied index. Existing multiway intent relationship remains.

Judge receives immutable inputs, prior assessment and named obligations. It
returns classification refinements and status/reference for every obligation.
The controller records the separate invocation, retains prior inference, reruns
the same table and uses existing action/policy/state/mode concurrency checks.

Canonical audit fields: `policy_evidence`, `policy_result`,
`outstanding_obligations`, `obligation_disposition`, `review_binding`,
`review_answers`, and stage classifications. Compatibility `facts` is a
presentation view, never policy input. An ordinary operator cap is recorded as
`waived_by_operator_bound`, not satisfied evidence. Protected mode, malformed
assessment, lifecycle and configured human/minimum floors remain non-executable.

Supplied-classification tests prove table semantics only. Model fidelity and
unchanged-request safety/benign gates remain separate; no synthetic grants.

Partial static target coverage disqualifies a whole-call typed path grant, not
every possible permission basis. A separate applicable semantic assignment may
permit the call under the normal profile/review obligations. Partial coverage
remains partial in the trace. Relevant unresolved denials, checkpoints and mode
requirements bind all alternatives; an unresolved alternate allow does not.

## Canonical finalization trace

Call `hybrid_runtime.finalize_result(policy_result, maximum=..., minimum=...,
...)` for the final outcome and projection. The original `finalize()` remains a
tuple view of this same implementation; do not independently reproduce bounds.

`PolicyResult` is the **pre-bound** policy/evidence result. Its
`blocking_reasons` distinguish known violations from missing evidence. A known
plan mutation returns `request.read_only_required`, with no invented mutation
obligation. Unknown mutation remains an outstanding `mode.read_only` obligation.
Strict uses the same consequential-action predicate as independent review,
including observed production writes even if the model reports low risk.

`FinalizationResult` exposes:

- `raw_outcome`, `decision`, `override`;
- `outstanding_obligations`: still-unsatisfied evidence plus finalization floors;
- `blocking_reasons`: known protected violations/denial floors;
- `discharged_obligation_ids`: obligations actually discharged by scoped human
  approval;
- `waived_obligation_ids`: still-unsatisfied ordinary obligations waived by an
  explicit operator bound, not relabelled as satisfied;
- `disposition`: `satisfied`, `satisfied_by_human`, `outstanding`, `blocked`, or
  `waived_by_operator_bound`.

An unsatisfied request minimum-human floor has ID/basis
`request.minimum_outcome`, requirement `human`, and `protected=true`.
It appears even when the pre-bound result permits the action. The request denial
floor is a blocking reason, not an uncertain obligation.

Evaluator and Judge persist the JSON representation as
`evaluation_metadata.final_policy_result`; the top-level metadata
`outstanding_obligations` and `obligation_disposition` mirror this final trace.
Human resolution updates it transactionally alongside the effective decision.
`policy_result` remains the original/reassessed pre-bound result for evidence
acquisition. Benchmark adapters should consume the canonical final trace rather
than manufacture floor obligations or infer final disposition from a raw permit.

Partial effect/target coverage can legitimately add genuine evidence obligations;
this interface does not normalize those away to match an external reference.
