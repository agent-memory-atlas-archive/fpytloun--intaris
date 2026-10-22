"""Offline prompt comparison against the approved, pinned runtime revision.

Run: uv run --extra dev --with tiktoken python -m tools.l1_projection_sizes --output /tmp/intaris-l1-projection-sizes.json
Token counts use GPT-OSS's o200k_harmony encoding, excluding chat framing.
"""

from __future__ import annotations

import argparse
import builtins
import json
import subprocess
import types
from pathlib import Path
from unittest.mock import Mock

from intaris import policy_assessment
from intaris.classifier import observe_hybrid
from intaris.hybrid import digest
from intaris.policy_evidence import Obligation, from_observations
from intaris.read_only import observe

BASE = "c7c60f6bd5dd76451584353769e0bbef86d0f543"
ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "risk",
    "effects",
    "mutation",
    "relationship",
    "authorization",
    "authorization_source",
    "authorization_scope",
    "contextual_restrictions",
    "restriction_reference",
    "target_scope",
    "target_reference",
    "consequence",
)


def specimen(case_id: str) -> tuple[dict, object, object]:
    """Build representative selected evidence from versioned benchmark fixtures."""
    cases = json.loads(
        (ROOT / "tools/benchmark/fixtures/source-context-focused-v1.json").read_text()
    )["cases"]
    case = next(case for case in cases if case["id"] == case_id)
    content = case["events"][0]["text"]
    restrictions = case["extraction_reference"]["restrictions"]
    request = content
    for quote in restrictions:
        request = request.replace(quote, "")
    request = request.strip()
    selected = [("request", request)]
    selected.extend(("restriction", quote) for quote in restrictions)
    if case["extraction_reference"]["approval_quotes"]:
        # Full same statement is a request and explicit bounded approval.
        selected.append(("approval", request))
    rows = [
        {
            "ref": "session_intention",
            "origin": "generated_summary",
            "scope_refs": [],
            "chronology": None,
            "truncated": False,
            "content": case["intention"],
        },
        {
            "ref": "latest_user_message",
            "origin": "client_user_claim",
            "scope_refs": [],
            "chronology": "1",
            "truncated": False,
            "content": content,
        },
    ]
    items = [
        {
            "kind": kind,
            "quote": quote,
            "source_ref": "latest_user_message",
            "start": content.index(quote),
            "end": content.index(quote) + len(quote),
            "scope_refs": [],
            "horizon": "short",
            "explicitness": "explicit",
            "temporal_status": "current",
            "status": "current",
            "relation": "none",
            "reference_resolution": "direct",
            "related_source_refs": [],
            "polarity": "negative" if kind == "restriction" else "positive",
        }
        for kind, quote in selected
    ]
    artifact = {
        "version": "task-context-single-v1",
        "status": "ready",
        "coverage": {"complete": True, "missing": []},
        "compact_intention": "",
        "missing_horizons": ["mid", "long"],
        "policy_clues": {},
        "items": items,
        "current_request": {"status": "supported", "item_indices": [0]},
        "source_metadata": [
            {k: v for k, v in r.items() if k != "content"} for r in rows
        ],
        "originals": rows,
        "source_digest": digest(rows),
        "artifact_digest": digest(items),
        "watermarks": {},
        "legacy_intention_diagnostic": case["intention"],
    }
    observations = observe_hybrid(case["tool"], case["args"], policy={})
    observations.update(
        arguments=case["args"], mutation=observe(case["tool"], case["args"])
    )
    state = from_observations(
        observations,
        sources={
            "session_intention": {
                "origin": "generated_summary",
                "content": case["intention"],
            },
            "latest_user_message": {
                "origin": "recorded_client_user_message",
                "content": content,
            },
        },
    )
    result = types.SimpleNamespace(
        outstanding_obligations=[
            Obligation(
                id="context.compatibility", basis="context.compatibility", fields=FIELDS
            )
        ]
    )
    return artifact, state, result


def valid_answer(schema: dict) -> dict:
    """Shape-only local answer; never used as evidence of model correctness."""
    return {
        key: (
            [prop["items"]["enum"][0]]
            if prop["type"] == "array"
            else prop.get("enum", [""])[0]
        )
        for key, prop in schema["properties"].items()
    }


def base_messages(artifact, state, result) -> list[dict]:
    """Execute pinned assembly and renderer with isolated module-local imports."""
    source = subprocess.check_output(
        ["git", "show", f"{BASE}:intaris/policy_assessment.py"], cwd=ROOT, text=True
    )
    renderer = subprocess.check_output(
        ["git", "show", f"{BASE}:intaris/task_context.py"], cwd=ROOT, text=True
    )
    pinned_renderer = types.ModuleType("pinned_task_context")
    exec(
        compile(renderer, f"{BASE}:intaris/task_context.py", "exec"),
        pinned_renderer.__dict__,
    )

    def pinned_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "intaris.task_context" and level == 0:
            return pinned_renderer
        return builtins.__import__(name, globals, locals, fromlist, level)

    module = types.ModuleType("pinned_policy_assessment")
    # Do not swap sys.modules: other tests/worker threads retain live imports.
    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": pinned_import}
    exec(
        compile(source, f"{BASE}:intaris/policy_assessment.py", "exec"), module.__dict__
    )
    module.PolicyAssessment.model_rebuild(_types_namespace=module.__dict__)
    question = module.request(state, result, "", task_context=artifact)
    llm = Mock()
    llm.generate.side_effect = lambda messages, **kw: json.dumps(
        valid_answer(kw["json_schema"]["schema"])
    )
    module.classify(llm, None, question)
    return llm.generate.call_args.args[0]


def measure() -> dict:
    """Measure characters and local token counts, with exact rendered messages."""
    import tiktoken

    encoding = tiktoken.get_encoding("o200k_harmony")
    cases = {}
    for case_id in (
        "inbox-inspection-only",
        "inbox-bounded-request",
        "email-requested",
        "database-wipe-shell",
    ):
        artifact, state, result = specimen(case_id)
        baseline = base_messages(artifact, state, result)
        question = policy_assessment.request(
            state, result, "", task_context=artifact, projection="lines"
        )
        projected = [
            {"role": "system", "content": policy_assessment.system_prompt(question)},
            {"role": "user", "content": question["prompt"]},
        ]
        cases[case_id] = {}
        for mode, messages in (("base_json", baseline), ("lines", projected)):
            counts = {
                message["role"]: {
                    "characters": len(message["content"]),
                    "tokens": len(
                        encoding.encode(message["content"], disallowed_special=())
                    ),
                }
                for message in messages
            }
            counts["total"] = {
                key: sum(row[key] for row in counts.values())
                for key in ("characters", "tokens")
            }
            cases[case_id][mode] = {
                "counts": counts,
                "tool_name_offset": messages[1]["content"].index(state.value("tool")),
                "messages": messages,
            }
    return {
        "base": BASE,
        "tokenizer": "tiktoken/o200k_harmony",
        "chat_framing_included": False,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(measure(), ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
