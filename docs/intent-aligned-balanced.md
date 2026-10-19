# Balanced means intention-consistent, not per-action consent

Canonical permission ownership remains `hybrid.evaluate_policy`. `strict` retains
its existing rules. Balanced and bounded autonomy add `intent_aligned` after
binding prohibitions, workflow gates, checkpoints, read-only ceilings and
applicable advisory checks. A complete L1 assessment must report:

- `directly_serves_intent` or `supports_intent`;
- low risk (bounded autonomy also permits medium);
- consistent contextual restrictions and target scope;
- known effects with no disclosure or external/irreversible boundary.

Consequential local edits are eligible. Unresolved scope/restrictions do not
gain this fast grant; they go to the profile-aware Judge. This path never invokes
Judge, including unattended sessions. Request floors still apply in finalization.

## External boundary

Existing effects are coarse: read/write/delete/transmit/disclose/unknown, not a
complete action taxonomy. `effect_scope` combines delete/transmit/disclose,
irreversibility and production mutation witnesses with a small operation-name
and shell-command observation for send/publish/pay/order/delete/push/deploy and
credential changes. The observation is additive, not a model-overridable grant.
It is not an executable security proof; the purpose/threat-model document applies.
External actions require an explicit scoped user grant. Judge cannot approve
them on the routine-within-task basis. A quoted confirmation such as “Odešli”
can supply the existing explicit-request basis for the actual referenced send.

## Judge contract

For balanced/bounded non-external actions the question is whether the action is
consistent with what the user is trying to do and not harmful. `missing_permission`
is removed from the model's basis choices. The captured session intention can be
cited when no user-message originals exist. Approval still needs valid citations
and acceptable risk; operator restrictions and disclosure witnesses are retained.
A model returning the invalid missing-permission rationale is kept unresolved as
outside-task, not mechanically converted to approval. No keyword grants exist.
Strict and external-action Judge contracts preserve explicit-grant handling.

## Profile and assessment diagnosis

Prior 280-call cohort: 19 unavailable profiles and 18 assessment failures.

- 13 unavailable profiles had no user messages/tool history, so regeneration
  correctly returned without inference. The harness wrongly required an update;
  it now uses the real declared session intention in that no-history case.
- Six profiles exhausted three upstream analysis 502s each. Existing bounded retries remain;
  provider availability cannot be fixed by granting or inventing context.
- 18 evaluation-role HTTP-200 responses failed local parsing/schema validation.
  Historical subtype/body was not retained,
  so malformed JSON versus enum/schema errors cannot be reconstructed. L1 now
  allows one repair request with the exact schema, validates again, and fails
  closed if invalid. Safe cause class/validation keyword is recorded on failures.

Analysis strict-schema responses were 130 schema 422s, 131 successes and 18
502s—not 100% schema failure. The observed high failure rate nevertheless makes
JSON-object plus existing local semantic/quote validation the primary profile
mode for `routes/intaris_analysis`. Other routes keep strict primary mode and
the existing bounded fallback. Primary JSON-object transient failures retain
the three-attempt owner budget; a strict-to-object fallback is still one shot.

The deployed `hotfix/workflow-scope-legacy` branch (8f28a87, 81f7ab1) is merged.
This fixes unsupported declarations on legacy sessions independently of the
balanced semantics. No new deployment is part of this task.
