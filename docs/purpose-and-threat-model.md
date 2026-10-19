# Purpose and threat model

## Purpose

Intaris helps determine when an agent's actions may contradict the user's
intention or show harmful behavior. It looks at tool calls in context and at
behavior over a session and across sessions. Its purpose is intention guardrails
and behavior analytics, not prevention of every possible harmful action.

## In scope

Intaris is intended to help identify:

- Drift from the user's intention, including drift caused by prompt injection.
- Actions that conflict with explicit user prohibitions.
- Possible disclosure of secrets.
- Harmful patterns within one session and across sessions, including patterns
  that may develop over a short or longer period.

These checks provide signals and decisions based on the information available to
Intaris. They do not prove that an agent's behavior is safe.

## Out of scope

An agent may construct harmful actions in ways that Intaris cannot anticipate or
detect. Preventing or containing every such action is the job of the execution
environment, not Intaris. A sandbox can isolate filesystem and network access
and limit the tools an agent can use. Hard "never touch X" restrictions belong
to the sandbox or tool exposure, not to the Intaris evaluator.

## Recommended deployment

For untrusted agents, use layered controls: run the agent in a sandbox with
filesystem and network isolation and limited tools, and use Intaris for
intention-aware evaluation, audit, and behavioral analysis. Together these
provide complementary guardrails; Intaris is not a replacement for the sandbox.

## Static checks

All profiles assess intention alignment and severity, not per-action permission.
Aligned low/medium-risk actions within the bounded original task without a real target
or restriction conflict approve without a Judge. Delete, write, production and
external/irreversible labels inform severity assessment, audit and configurable
rules; they do not independently require approval. Unknown metadata does not
establish reversibility or manufacture permission.

Strict reviews unrelated or unclear scope unless a real scoped allowance or
validated explicit permission covers it. Balanced allows harmless low-risk
incidental drift but reviews unrelated medium-risk work and relationship-only
conflicts. Bounded autonomy permits low/medium goal deviation when there is no
actual instruction, target or restriction violation. A relationship label alone
is not proof of such a violation. There is no new blanket unknown-risk gate.

Source-backed conflicts and high severity receive independent assessment
(an existing validated explicit user authorization can cover high-risk work).
Model-only critical assessments also reach the Judge. Targeted review receipts
can correct fallible model interpretations without rewriting the original
assessment. Judge-confirmed critical consequences deny. Deterministic
prohibitions, human checkpoints/refusals, read-only modes and outcome caps remain
binding. Explicit exceptions require typed original user evidence covering the
operation and targets. The common classifier and independent Judge assess the
same factual questions across profiles; only policy chooses the outcomes.
Optional corrected fields keep old receipts readable without inventing alignment
or permission from a Judge vote. Operators can configure
approval requirements for particular effects or scopes. Approved-edit caches
retain conservative effect boundaries and do not bypass assessment of deletion,
production mutation or external actions.

An exact action already admitted under a validated human approval is not sent
back to the Judge because raw audit obligations remain. Receipt identity,
policy/state revisions, mode restrictions and deterministic denials still apply.

Static checks are a pragmatic fast path for routine work. They reduce unnecessary
evaluation, but they are not a security proof and cannot recognize every harmful
construction. The [Git static trust example](git-static-trust.md) shows how this
fast path should be scoped.

## Roadmap direction

The direction is to add agent scoring, use detected bypass attempts as a signal
that reduces trust rather than as a static wall, and integrate L2/L3 behavioral
analytics with Cognis. These are roadmap directions, not claims about current
capabilities.
