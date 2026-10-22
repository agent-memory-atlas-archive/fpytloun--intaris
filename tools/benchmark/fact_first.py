"""Offline policy replay of private captured evaluations; no inference or actions.

Usage: python -m tools.benchmark.fact_first CAPTURE --baseline HEAD
Only aggregates are printed. Private captured bodies never enter public fixtures.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import types
from pathlib import Path

from intaris.hybrid import evaluate_policy, validate_rules
from intaris.policy_evidence import Evidence


def captured_state(record: dict, *, captured_sources: bool = False) -> Evidence | None:
    meta = record.get("evaluation_metadata") or {}
    stage = next((s for s in meta.get("stages", []) if s.get("stage") == "l1"), None)
    if stage is None or not meta.get("policy_evidence"):
        return None
    sources = {}
    context = meta.get("task_context") or {}
    for source in context.get("originals", []):
        sources[source["ref"]] = {
            "origin": source["origin"],
            "text": source.get("content"),
            "truncated": bool(source.get("truncated")),
            "constraint_items": [],
        }
    for item in context.get("items", []):
        if item.get("kind") not in {"restriction", "disapproval", "uncertainty"}:
            continue
        source = sources.get(item.get("source_ref"))
        if source is not None:
            source["constraint_items"].append(
                {
                    "kind": item["kind"],
                    "status": item.get("temporal_status", item.get("status")),
                    "quote": item.get("quote"),
                }
            )
    state = Evidence.model_validate(meta["policy_evidence"])
    return state.model_copy(
        update={
            "assessment": stage["classification"],
            "review": None,
            "human_approved": False,
            "sources": state.sources if captured_sources else sources,
        }
    )


def replay(records: list[dict], baseline) -> dict:
    result = {}
    for profile in ("strict", "balanced", "bounded_autonomy"):
        before, after = collections.Counter(), collections.Counter()
        transitions = collections.Counter()
        excluded = 0
        for record in records:
            state = captured_state(record)
            if state is None:
                excluded += 1
                continue
            rules = validate_rules(
                record["evaluation_metadata"]["active_policy"].get("rules", [])
            )
            old = baseline(
                rules,
                captured_state(record, captured_sources=True),
                profile,
                assessed=True,
            )
            new = evaluate_policy(rules, state, profile, assessed=True)
            before[old.outcome] += 1
            after[new.outcome] += 1
            transitions[old.outcome + " -> " + new.outcome] += 1
        result[profile] = {
            "before": dict(before),
            "after": dict(after),
            "transitions": dict(transitions),
            "excluded_without_action_evidence": excluded,
        }
    return result


def baseline_policy(revision: str):
    source = subprocess.check_output(
        ["git", "show", f"{revision}:intaris/hybrid.py"], text=True
    )
    name = "intaris._offline_baseline"
    module = types.ModuleType(name)
    module.__package__ = "intaris"
    sys.modules[name] = module
    exec(compile(source, name, "exec"), module.__dict__)
    return module.evaluate_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--baseline", default="HEAD")
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text())
    print(
        json.dumps(
            {
                "scope": "Stored L1 outputs, reconstructed source registration, no new classification",
                "not_claimed": "Ground-truth accuracy or a production escalation reduction",
                "profiles": replay(capture["records"], baseline_policy(args.baseline)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
