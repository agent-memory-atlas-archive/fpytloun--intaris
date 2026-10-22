"""Source-grounded first-pass cases; replay never executes the proposed action."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from intaris.classifier import observe_hybrid
from intaris.hybrid import PolicyResult, evaluate_policy
from intaris.policy_evidence import Evidence, from_observations
from intaris.read_only import observe

CORPUS = Path(__file__).parent / "fixtures/first-pass-v1.json"


def cases() -> list[dict[str, Any]]:
    """Read the versioned synthetic corpus without provider or service access."""
    return json.loads(CORPUS.read_text())["cases"]


def specimen(
    case: dict[str, Any], *, clean: bool = False
) -> tuple[dict[str, Any], Evidence, PolicyResult]:
    """Build the same observable action with either full history or current scope."""
    # Dates are explicitly synthetic fixture sequence, not captured timestamps.
    messages = (
        case.get("minimal_messages", case["messages"]) if clean else case["messages"]
    )
    rows = [
        {
            "ref": "latest_user_message" if i == len(messages) - 1 else f"earlier:{i}",
            "origin": "client_user_claim",
            "content": message,
            "chronology": f"2026-01-{i + 1:02d}T12:00:00+00:00",
            "truncated": False,
            "scope_refs": [],
        }
        for i, message in enumerate(messages)
    ]
    # Minimal arms use manually specified equivalent instructions. Never infer
    # that the last message supersedes every independent earlier restriction.
    artifact = {
        "version": "task-context-single-v1",
        "context_mode": "raw",
        "status": "ready",
        "compact_intention": case.get("summary", case["messages"][-1]),
        "coverage": {"complete": True, "missing": []},
        "items": [],
        "current_request": {"status": "raw_sources", "item_indices": []},
        "originals": rows,
        "missing_horizons": [],
        "source_metadata": [
            {k: v for k, v in r.items() if k != "content"} for r in rows
        ],
    }
    observations = observe_hybrid(case["tool"], case["args"], policy={})
    observations.update(
        arguments=case["args"],
        mutation=observe(case["tool"], case["args"]),
        executing_agent="implementation-agent",
    )
    state = from_observations(
        observations,
        sources={
            r["ref"]: {"origin": r["origin"], "content": r["content"]} for r in rows
        },
    )
    state = state.model_copy(
        update={"advisory_clauses": case.get("operator_restrictions", [])}
    )
    return artifact, state, evaluate_policy([], state, "balanced")


def finish(state: Evidence, assessment: dict[str, Any]) -> dict[str, Any]:
    """Apply actual policy to an L1 answer; never invoke Judge."""
    state = state.model_copy(update={"assessment": assessment})
    result = evaluate_policy([], state, "balanced", assessed=True)
    return {
        "outcome": result.outcome,
        "obligations": [o.id for o in result.outstanding_obligations],
    }
