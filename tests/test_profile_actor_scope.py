"""Actor attribution is explicit in both model boundaries."""

import json

from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import request
from intaris.l1_projection import classifier_view, render
from intaris.policy_evidence import from_observations


def test_actual_actor_is_preserved_in_l1_and_judge():
    state = from_observations({"tool": "read", "executing_agent": "worker"})
    artifact = {"originals": [], "items": []}
    assert classifier_view(artifact, state)["executing_agent"] == "worker"
    assert "Executing agent: worker" in render(artifact, state, {})
    question = request(
        {"tool": "read", "agent_id": "worker"},
        {
            "agent_id": "coordinator",
            "intention": "A stale generated summary",
            "policy": {"profile": "balanced"},
        },
        state,
        evaluate_policy([], state),
        artifact,
    )
    payload = json.loads(question["messages"][1]["content"])
    assert payload["executing_agent"] == "worker"
    assert payload["profile"] == "balanced"
    assert "fallible" in question["messages"][0]["content"]
