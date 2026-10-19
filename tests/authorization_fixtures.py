"""Scripted positive semantic evidence for tests of the real Judge boundary."""

import json


def with_verified_routine_support(state, result):
    """Policy-unit fixture for a completed, independently validated scope check.

    This does not replace API/normalization tests. The caller must first assert
    that unreviewed consequential work is not admitted.
    """
    from intaris.policy_evidence import Review

    assert result.outcome == "escalate"
    assert any(item.id.startswith("scope.") for item in result.outstanding_obligations)
    assert state.value("risk") in {"low", "medium"}
    return state.model_copy(
        update={
            "review": Review(
                obligation_ids=[
                    item.id
                    for item in result.outstanding_obligations
                    if item.id.startswith("scope.")
                ],
                evidence_refs=["verified-task-source"],
                scope_support_refs=["verified-task-source"],
                user_would="approve",
                risk=state.value("risk"),
                basis="routine_within_task",
            )
        }
    )


def add_supported_authorization_response(llm, *, outcome="supported"):
    previous = llm.generate.return_value
    previous_effect = llm.generate.side_effect

    def generate(messages, **kwargs):
        if kwargs.get("json_schema", {}).get("name") != "authorization_support":
            return (
                previous_effect(messages, **kwargs)
                if callable(previous_effect)
                else previous
            )
        payload = json.loads(messages[1]["content"])
        source = payload["sources"][0]
        assert source["text"], "The fixture must present an actual citable original."
        return json.dumps(
            {
                "outcome": outcome,
                "strength": "routine",
                "support": [{"source": source["index"], "quote": source["text"]}],
                "explanation": "The captured request supports this bounded task step.",
                "decision_status": "none",
                "missing_user_decision": "",
            }
        )

    llm.generate.side_effect = generate
