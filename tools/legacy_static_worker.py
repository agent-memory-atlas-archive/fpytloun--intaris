"""Isolated exact-tree legacy classifier runner; no LLM client or network."""

import json
import sys

sys.path.insert(0, sys.argv[1])
from intaris.classifier import Classification, classify
from intaris.evaluator import _CONTEXT_ARG_KEY, _effective_working_directory


def main():
    for line in sys.stdin:
        row = json.loads(line)
        args = row["args"]
        context = args.pop(_CONTEXT_ARG_KEY, {})
        if args.get("context") == context:
            args.pop("context", None)
        classification = classify(
            row["tool"],
            args,
            session_policy=row["policy"],
            working_directory=_effective_working_directory(
                row["tool"],
                args,
                context,
                row["details"],
            ),
        )
        # Every non-READ reaches a fake escalation, never a model approval.
        approved = classification == Classification.READ and row.get(
            "minimum_outcome"
        ) not in {"escalate", "deny"}
        print(
            json.dumps(
                {
                    "call_id": row["call_id"],
                    "legacy_static": approved,
                    "legacy_classification": classification.value,
                }
            )
        )


if __name__ == "__main__":
    main()
