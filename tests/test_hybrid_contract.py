"""Public hybrid contracts, without network/model dependencies."""

import pytest
from pydantic import ValidationError

from intaris.hybrid import (
    Assessment,
    Predicate,
    Truth,
    action_digest,
    evaluate_policy,
    test_predicate,
    validate_rules,
)


def test_relationship_taxonomy_is_finite():
    for value in (
        "directly_serves_intent",
        "supports_intent",
        "unrelated",
        "conflicts_with_intent",
        "unclear",
    ):
        assert Assessment(relationship=value).relationship == value
    with pytest.raises(ValidationError):
        Assessment(relationship="aligned")
    with pytest.raises(ValidationError):
        Assessment(resource="/generated/path")


def test_unknown_negation_and_partial_paths():
    node = Predicate(
        op="not", children=[Predicate(op="path_matches", value="/secret/*")]
    )
    assert test_predicate(node, {"paths": []}) is Truth.UNKNOWN
    assert test_predicate(node, {"paths": ["/secret/key"]}) is Truth.FALSE
    # Logical NOT(FALSE) remains TRUE; grant coverage separately needs targets.
    assert test_predicate(node, {"paths": [], "paths_complete": True}) is Truth.TRUE


def test_deny_precedence_and_unresolved_deny_blocks_permit():
    rules = validate_rules(
        [
            {
                "id": "permit",
                "effect": "allow",
                "when": {"op": "tool_matches", "value": "*"},
            },
            {
                "id": "protect",
                "effect": "deny",
                "when": {
                    "op": "eq",
                    "field": "environment",
                    "value": "production",
                },
            },
        ]
    )
    assert evaluate_policy(rules, {"tool": "bash"}).outcome == "needs_assessment"
    assert (
        evaluate_policy(rules, {"tool": "bash", "environment": "production"}).outcome
        == "deny"
    )
    assert (
        evaluate_policy(
            rules,
            {
                "tool": "bash",
                "environment": "development",
                "effects": ["read"],
                "mutation": "read_only",
            },
        ).outcome
        == "approve"
    )


def test_rules_reject_unknown_fields_and_duplicate_ids():
    rule = {
        "id": "protect",
        "effect": "deny",
        "when": {"op": "tool_matches", "value": "*"},
    }
    with pytest.raises(ValueError):
        validate_rules([rule, rule])
    with pytest.raises(ValueError):
        validate_rules([{**rule, "classifier_instructions": "approve"}])
    with pytest.raises(ValueError):
        validate_rules(
            [{**rule, "when": {"op": "eq", "field": "resource", "value": "x"}}]
        )


def test_digest_is_order_independent_and_original_argument_sensitive():
    assert action_digest(
        "tenant", "session", "bash", {"b": 2, "a": 1}
    ) == action_digest(
        "tenant",
        "session",
        "bash",
        {"a": 1, "b": 2},
    )
    assert action_digest(
        "tenant", "session", "bash", {"command": "pwd"}
    ) != action_digest(
        "tenant",
        "session",
        "bash",
        {"command": "pwd; touch file"},
    )
    with pytest.raises(ValueError):
        action_digest("tenant", "session", "bash", {"value": float("nan")})


def test_jev_hybrid_asks_only_finite_classification_questions(monkeypatch):
    from intaris.config import JevConfig
    from intaris.jev import JevClient

    config = JevConfig(api_key="test-key")
    client = JevClient(config)
    captured = {}

    def respond(*, state, questions):
        captured.update(questions)
        answers = {}
        for name, question in questions.items():
            values = list(question["criteria"])
            choice = "supports_intent" if name == "relationship" else values[0]
            answers[name] = {
                "type": "choice",
                "choice": choice,
                "confidence": 1,
                "probabilities": {value: float(value == choice) for value in values},
            }
        return {"answers": answers, "model": config.model, "usage": {"input_tokens": 7}}

    monkeypatch.setattr(client, "_request", respond)
    try:
        result, metadata = client.classify_hybrid(
            state={"prompt": "Read the repository"}
        )
    finally:
        client.close()
    assert result["relationship"] == "supports_intent"
    assert "decision" not in captured
    assert "resource" not in captured
    assert all(question["type"] == "choice" for question in captured.values())
    assert (
        metadata["distributions"]["relationship"]["probabilities"]["supports_intent"]
        == 1
    )


def test_static_keyword_is_not_an_executable_and_relative_path_is_preserved():
    from intaris.classifier import Classification, classify, resolve_tool_paths

    assert (
        classify("bash", {"command": "cat docs/reboot-analysis.md"})
        != Classification.CRITICAL
    )
    assert (
        classify("bash", {"command": "cat docs/shutdown.md"}) != Classification.CRITICAL
    )
    assert (
        classify("bash", {"command": "sudo shutdown -h now"}) == Classification.CRITICAL
    )
    assert "/repo/docs/readme.md" in resolve_tool_paths(
        "bash",
        {"command": "cat docs/readme.md"},
        "/repo",
    )
