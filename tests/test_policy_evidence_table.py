"""Supplied evidence tests prove policy semantics, not model accuracy."""

from intaris.hybrid import evaluate_policy, validate_rules
from intaris.policy_evidence import Evidence, Observation, Review


def evidence(*, observations=None, assessment=None, reviewed=False, human=False):
    values = {
        "tool": "write",
        "paths": ["/repo/a"],
        "paths_complete": True,
        "effects": ["write"],
        "mutation": "mutating",
    }
    values.update(observations or {})
    return Evidence(
        observations={
            key: Observation(value=value, source="test", coverage="complete")
            for key, value in values.items()
        },
        assessment=assessment or {},
        sources={
            "assignment": {
                "origin": "client_assignment",
                "content": "Update the repository",
                "independent_grant": True,
            }
        },
        review=Review(
            user_would="approve",
            risk="high",
            obligation_ids=["risk.high"],
            evidence_refs=["assignment"],
        )
        if reviewed
        else None,
        human_approved=human,
    )


def rule(identifier, effect, predicate):
    return {"id": identifier, "effect": effect, "when": predicate}


def semantic(**updates):
    return {
        "relationship": "supports_intent",
        "risk": "high",
        "authorization": "explicit",
        "authorization_source": "assignment",
        "authorization_scope": "within_scope",
        "contextual_restrictions": "consistent",
        "restriction_reference": "assignment",
        "target_scope": "consistent",
        "target_reference": "action",
        "consequence": "consequential",
        **updates,
    }


def test_high_impact_whole_call_declared_grant_needs_no_risk_downgrade():
    rules = validate_rules(
        [
            rule(
                "grant",
                "allow",
                {"op": "path_matches", "value": "/repo/*", "quantifier": "all"},
            )
        ]
    )
    # A declared scope does not erase actual high severity in any mode.
    result = evaluate_policy(rules, evidence(assessment=semantic()), "strict")
    assert result.outcome == "escalate"
    assert evaluate_policy(rules, evidence(assessment=semantic())).outcome == (
        "escalate"
    )


def test_semantic_consequence_requires_review_not_lower_risk():
    state = evidence(assessment=semantic())
    result = evaluate_policy([], state)
    assert result.outcome == "escalate"
    assert "risk.high" in [item.id for item in result.outstanding_obligations]
    assert (
        evaluate_policy([], evidence(assessment=semantic(), reviewed=True)).outcome
        == "approve"
    )


def test_strict_high_impact_targeted_review_preserves_risk():
    assert (
        evaluate_policy(
            [], evidence(assessment=semantic(), reviewed=True), "strict"
        ).outcome
        == "approve"
    )
    assert (
        evaluate_policy(
            [], evidence(assessment=semantic(), reviewed=True, human=True), "strict"
        ).outcome
        == "approve"
    )


def test_unresolved_alternate_allow_does_not_block_independent_grant():
    rules = validate_rules(
        [
            rule("grant", "allow", {"op": "tool_matches", "value": "read"}),
            rule(
                "other",
                "allow",
                {"op": "eq", "field": "environment", "value": "development"},
            ),
        ]
    )
    state = evidence(
        observations={"tool": "read", "effects": ["read"], "mutation": "read_only"},
        assessment={
            "relationship": "supports_intent",
            "contextual_restrictions": "consistent",
            "restriction_reference": "assignment",
        },
    )
    assert evaluate_policy(rules, state).outcome == "approve"


def test_unknown_relevant_deny_still_blocks_grant():
    rules = validate_rules(
        [
            rule("grant", "allow", {"op": "tool_matches", "value": "read"}),
            rule(
                "protect",
                "deny",
                {"op": "eq", "field": "environment", "value": "production"},
            ),
        ]
    )
    assert (
        evaluate_policy(rules, evidence(observations={"tool": "read"})).outcome
        != "approve"
    )


def test_concrete_wrong_target_survives_generic_semantic_scope():
    rules = validate_rules(
        [rule("protect", "deny", {"op": "path_matches", "value": "/other/*"})]
    )
    state = evidence(
        observations={"paths": ["/repo/a", "/other/b"]},
        assessment=semantic(),
        reviewed=True,
    )
    assert evaluate_policy(rules, state).outcome == "deny"


def test_partial_scope_cannot_borrow_declared_grant():
    rules = validate_rules(
        [
            rule(
                "grant",
                "allow",
                {"op": "path_matches", "value": "/repo/*", "quantifier": "all"},
            )
        ]
    )
    state = evidence(
        observations={"paths_complete": False},
        # A typed grant alone is not semantic authority. This preserves the
        # negative control while testing the separately approved alternative.
        assessment=semantic(authorization="unclear", relationship="unrelated"),
        reviewed=True,
    )
    # Strict keeps cited authority; balanced approves consistent work by design.
    assert evaluate_policy(rules, state, "strict").outcome != "approve"


def test_partial_ordinary_semantic_basis_retains_partial_coverage():
    rules = validate_rules(
        [
            rule(
                "unproved",
                "allow",
                {
                    "op": "path_matches",
                    "value": "/repo/*",
                    "quantifier": "all",
                },
            )
        ]
    )
    state = evidence(
        observations={"paths_complete": False},
        assessment=semantic(risk="low", consequence="routine"),
    )
    result = evaluate_policy(rules, state)
    assert result.outcome == "approve"
    assert result.permission_basis == "intent_aligned"
    assert result.evidence_quality == "model_assessed"
    assert state.value("paths_complete") is False
    assert "unproved" in result.unresolved_rules


def test_partial_consequential_basis_still_requires_independent_review():
    state = evidence(observations={"paths_complete": False}, assessment=semantic())
    result = evaluate_policy([], state)
    assert result.outcome == "escalate"
    assert "risk.high" in [item.id for item in result.outstanding_obligations]
    reviewed = evidence(
        observations={"paths_complete": False}, assessment=semantic(), reviewed=True
    )
    assert not evaluate_policy([], reviewed).outstanding_obligations
    assert evaluate_policy([], reviewed).outcome == "approve"


def test_partial_semantic_basis_cannot_bypass_relevant_deny_or_plan():
    rules = validate_rules(
        [
            rule(
                "protect",
                "deny",
                {
                    "op": "path_matches",
                    "value": "/secret/*",
                    "quantifier": "any",
                },
            )
        ]
    )
    state = evidence(
        observations={"paths_complete": False}, assessment=semantic(), reviewed=True
    )
    result = evaluate_policy(rules, state)
    assert result.outcome != "approve"
    assert "rule:protect" in [item.id for item in result.outstanding_obligations]
    plan = state.model_copy(update={"read_only_required": True})
    assert evaluate_policy([], plan).outcome == "deny"


def test_existential_subset_is_not_whole_call_typed_authority():
    rules = validate_rules(
        [
            rule(
                "subset",
                "allow",
                {
                    "op": "path_matches",
                    "value": "/repo/*",
                    "quantifier": "any",
                },
            )
        ]
    )
    state = evidence(
        observations={"paths": ["/repo/a", "/other/b"]},
        assessment=semantic(authorization="unclear"),
    )
    assert evaluate_policy(rules, state).outcome != "approve"
    alternative = evidence(
        observations={"paths": ["/repo/a", "/other/b"]},
        assessment=semantic(),
        reviewed=True,
    )
    assert evaluate_policy(rules, alternative).outcome == "approve"


def test_material_target_conflict_precedes_otherwise_sufficient_grant():
    rules = validate_rules(
        [rule("grant", "allow", {"op": "tool_matches", "value": "write"})]
    )
    state = evidence(assessment=semantic(target_scope="conflicts"))
    result = evaluate_policy(rules, state)
    assert result.outcome == "escalate"
    assert any(
        o.id == "model.denial" and o.protected for o in result.outstanding_obligations
    )


def test_routine_local_work_and_independently_permitted_unrelated_read():
    assert (
        evaluate_policy(
            [],
            evidence(
                assessment=semantic(
                    risk="low",
                    consequence="routine",
                    authorization="implicit",
                    environment="development",
                    side_effect_scope="bounded",
                )
            ),
        ).outcome
        == "approve"
    )
    rules = validate_rules(
        [rule("read", "allow", {"op": "tool_matches", "value": "read"})]
    )
    assert (
        evaluate_policy(
            rules,
            evidence(
                observations={
                    "tool": "read",
                    "effects": ["read"],
                    "mutation": "read_only",
                },
                assessment=semantic(relationship="unrelated", risk="low"),
            ),
            "strict",
        ).outcome
        == "approve"
    )


def test_plan_mutation_and_human_floor_remain_protected():
    state = evidence(assessment=semantic(), reviewed=True, human=True)
    state = state.model_copy(update={"read_only_required": True})
    assert evaluate_policy([], state).outcome == "deny"
    rules = validate_rules(
        [rule("human", "require_approval", {"op": "tool_matches", "value": "write"})]
    )
    result = evaluate_policy(rules, evidence(assessment=semantic(), reviewed=True))
    assert result.mandatory_rule_ids == ["human"]
    assert result.outcome != "approve"


def test_model_membership_cannot_be_promoted_to_deterministic_grant():
    rules = validate_rules(
        [
            rule(
                "dev",
                "allow",
                {"op": "eq", "field": "environment", "value": "development"},
            )
        ]
    )
    result = evaluate_policy(
        rules, evidence(assessment=semantic(environment="development"))
    )
    assert result.outcome == "escalate"
    assert result.constraint_matches[0]["comparison"] == "model_assessed"
    result = evaluate_policy(
        rules, evidence(assessment=semantic(environment="development"), reviewed=True)
    )
    assert result.outcome == "approve"


def test_unknown_plan_mutation_and_unknown_human_checkpoint_cannot_cap_away():
    from intaris.hybrid_runtime import finalize

    state = evidence(
        observations={"mutation": "unclear"}, assessment=semantic(), reviewed=True
    )
    state = state.model_copy(update={"read_only_required": True})
    result = evaluate_policy([], state)
    assert result.outcome != "approve"
    assert (
        finalize(
            result,
            maximum="approve",
            minimum=None,
            overlay={"read_only_required": True},
            mutation="unclear",
        )[0]
        != "approve"
    )
    rules = validate_rules(
        [
            rule(
                "production-review",
                "require_approval",
                {"op": "eq", "field": "environment", "value": "production"},
            )
        ]
    )
    result = evaluate_policy(rules, evidence(assessment=semantic(), reviewed=True))
    assert finalize(result, maximum="approve", minimum=None)[0] != "approve"
