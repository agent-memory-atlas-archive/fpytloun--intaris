# Delegated policy and evaluation recovery

Delegation is not an independent authorization boundary. Intaris composes a
child's policy from its tenant-scoped parent and child-local source at the
existing activation boundary. The parent selects evaluation mode/profile.
Child restrictions and outcome floors may tighten enforcement; children cannot
introduce independent grants. Child details remain child-owned.

Source policy and the captured parent authorization identity are stored in
nullable `policy_source` and `parent_policy_hash` columns. Existing children
without a source record conservatively treat their stored policy as local input.
Inherited changes are refreshed before evaluation, root to leaf. Ancestor
state/policy revisions and suspension are fenced again inside admission.
PostgreSQL locks ancestor rows root-first through transaction commit.

Plain identical policy/details refreshes are no-ops. Genuine policy, intention,
evidence or lifecycle changes still invalidate exact-action receipts. Nothing
here approves an old denial or acknowledgment automatically.

Jev no longer receives duplicate schema/options in both state and questions.
The actual question schema and all protected instructions/action arguments stay
unchanged. Real transport regressions exercise known-token and unknown-byte
ceilings; irreducibly oversized protected input still fails closed.

Legacy L1 and Judge now receive original parent scope plus later evidence, not
only a stale generated intention. Conditional recovery instructions remain
conditional; they do not create blanket rollback or protected-service grants.
An escalation floor no longer claims to have caused an existing model denial.

Deploy Intaris before the Cognis client update. Cognis supplies its configured
minimum outcome into canonical source policy and verifies server-composed child
policy, including nested mode changes. New Cognis defaults are Balanced;
explicit existing Legacy choices remain explicit choices.

Validation includes exact HTTP wire, no-op/real-change receipt regressions,
tenant isolation, nested parent changes and real PostgreSQL concurrency tests.
No new paid inference was used. Model interpretation improvements are not a
claim of universal semantic accuracy.

The initial full-suite run recorded 4,379 passing and 37 failing tests. Replaying
every failure on the unchanged base confirmed 24 pre-existing failures. The 13
new failures were corrected and all passed on rerun; the pre-existing failures
were retained, not skipped or rewritten to claim a green full suite. They concern
older source-presentation and unavailable-review expectations and remain separate
follow-up work. The final focused concurrency/inheritance checks also include a
parent-resume race and preservation of existing audited lifecycle denials.
