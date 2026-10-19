"""Approved semantic boundaries: source existence, quantified scope, shell facts."""

import pytest

from intaris.authorization import bind_authorization, scope_sources
from intaris.classifier import Classification, classify
from intaris.hybrid import (
    Predicate,
    Truth,
    evaluate_policy,
    validate_rules,
)
from intaris.hybrid import (
    test_predicate as predicate_value,
)
from intaris.policy_coverage import source_constraint


@pytest.mark.parametrize(
    "command,critical",
    [
        ("reboot $(printf '')", True),
        ("echo '$(reboot)'", False),
        ('echo "$(reboot)"', True),
        ("echo $(reboot)", True),
        (r"echo \`reboot\`", False),
        ("cat <<'TEXT'\nreboot\nshutdown\nTEXT", False),
        ("cat <<'TEXT'\nreboot\nTEXT\nshutdown -h now", True),
    ],
)
def test_quote_aware_shell_observations(command, critical):
    assert (
        classify("bash", {"command": command}) == Classification.CRITICAL
    ) == critical


@pytest.mark.parametrize(
    "paths,complete,quantifier,truth",
    [
        (["/cache/a"], True, "all", Truth.TRUE),
        (["/cache/a", "/docs/b"], True, "all", Truth.FALSE),
        (["/cache/a"], False, "all", Truth.UNKNOWN),
        (["/docs/b"], False, "all", Truth.FALSE),
        ([], True, "all", Truth.FALSE),
        (["/cache/a", "/docs/b"], True, None, Truth.TRUE),
    ],
)
def test_quantified_target_sets(paths, complete, quantifier, truth):
    node = Predicate(op="path_matches", value="/cache/*", quantifier=quantifier)
    assert predicate_value(node, {"paths": paths, "paths_complete": complete}) is truth


@pytest.mark.parametrize(
    "paths,effects,blocked",
    [
        (["/cache/a"], ["delete"], False),
        (["/docs/b"], ["delete"], True),
        (["/cache/a", "/docs/b"], ["delete"], True),
    ],
)
@pytest.mark.parametrize("profile", ["strict", "balanced"])
def test_compiled_deletion_never_borrows_allowed_subset(
    paths, effects, blocked, profile
):
    constraint = source_constraint(
        "Deny deletion of paths not matching /cache/*", "deny"
    )
    rules = validate_rules(
        [
            {"id": "protect", "effect": "deny", "when": constraint},
            {
                "id": "grant",
                "effect": "allow",
                "when": {"op": "tool_matches", "value": "bash"},
            },
        ]
    )
    result = evaluate_policy(
        rules,
        {"tool": "bash", "paths": paths, "paths_complete": True, "effects": effects},
        profile,
        assessed=True,
    )
    assert result.outcome == ("deny" if blocked else "approve")


@pytest.mark.parametrize(
    "paths,effects,permit",
    [
        (["/docs/a"], ["read"], True),
        (["/docs/a", "/secret/b"], ["read"], False),
        (["/docs/a"], ["read", "write"], False),
        (["/docs/a"], ["unknown"], False),
    ],
)
def test_read_grant_is_whole_call(paths, effects, permit):
    rules = validate_rules(
        [
            {
                "id": "read",
                "effect": "allow",
                "when": source_constraint(
                    "Allow reads of files matching /docs/*", "allow"
                ),
            }
        ]
    )
    result = evaluate_policy(
        rules,
        {
            "operation": "read",
            "effects": effects,
            "paths": paths,
            "paths_complete": True,
        },
        assessed=True,
    )
    assert (result.outcome == "approve") == permit


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git -c remote.origin.pushurl=https://other.test push origin main",
        "git -C /other push origin main",
    ],
)
def test_source_binding_never_proves_semantic_scope(command):
    sources = scope_sources(
        {"intention": "Push reviewed branch", "intention_source": "initial"}
    )
    bound = bind_authorization(
        {"command": command, "authorization_source": "session_intention"}, sources
    )
    assert bound == {
        "authorization_source_available": True,
        "authorization_source_independent": True,
    }
    facts = {
        **bound,
        "relationship": "directly_serves_intent",
        "risk": "low",
        "authorization": "explicit",
        "authorization_scope": "outside_scope",
        "effects": ["transmit"],
        "operation": "write",
    }
    assert evaluate_policy([], facts, assessed=True).outcome != "approve"


def test_generated_or_inherited_context_is_not_independent_grant():
    sources = scope_sources(
        {"intention": "Push", "intention_source": "user"}, "Deploy changes"
    )
    for source in sources:
        bound = bind_authorization({"authorization_source": source}, sources)
        assert bound["authorization_source_available"]
        assert not bound["authorization_source_independent"]


@pytest.mark.parametrize(
    "paths,complete,denied",
    [
        (["/cache/a"], True, True),
        (["/docs/b"], True, False),
        (["/cache/a", "/docs/b"], True, True),
        (["/cache/a"], False, True),
    ],
)
@pytest.mark.parametrize("profile", ["strict", "balanced"])
def test_positive_deletion_denial_uses_matching_witness(
    paths, complete, denied, profile
):
    rules = validate_rules(
        [
            {
                "id": "protect",
                "effect": "deny",
                "when": source_constraint(
                    "Deny deletion of paths matching /cache/*", "deny"
                ),
            },
            {
                "id": "grant",
                "effect": "allow",
                "when": {"op": "tool_matches", "value": "bash"},
            },
        ]
    )
    result = evaluate_policy(
        rules,
        {
            "tool": "bash",
            "paths": paths,
            "paths_complete": complete,
            "effects": ["delete"],
        },
        profile,
        assessed=True,
    )
    assert result.outcome == ("deny" if denied else "approve")
