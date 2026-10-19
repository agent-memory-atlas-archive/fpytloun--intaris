"""Read an authorized capture offline and print only structural model-view evidence.

Run with the repository on PYTHONPATH. No provider/client is instantiated.
The session envelope may be current: historical profile comes from audit metadata.
"""

import argparse
import json
from pathlib import Path

from intaris.hybrid import PolicyResult
from intaris.judge_simulator import request
from intaris.policy_evidence import Evidence


def reconstruct(path):
    captured = json.loads(Path(path).read_text())
    record = captured["record"]
    metadata = record["evaluation_metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    state = Evidence.model_validate(metadata["policy_evidence"]).model_copy(
        update={"review": None}
    )
    result = PolicyResult.model_validate(metadata["policy_result"])
    question = request(
        record,
        {"policy": {"profile": metadata["profile"]}},
        state,
        result,
        metadata["task_context"],
        reason=metadata.get("agent_reason"),
    )
    view = question["model_view"]
    output = {
        "call_id": record["call_id"],
        "profile_source": "historical audit metadata",
        "current_session_policy_used": False,
        "missing_historical_arguments": [
            "latest_assistant",
            "latest_user",
            "human_decisions",
            "authority_refs",
            "outcomes",
        ],
        "reconstructed_bytes": view["bytes"],
        "source_coverage": view.get("source_coverage"),
        "trimmed_sections": question["trimmed_sections"],
        "citable_count": len(question["citable"]),
        "preflight_overflow": question["overflow"],
        "sources": len(metadata["task_context"].get("originals", [])),
        "provider_calls": 0,
    }
    try:
        from intaris.input_budget import ContextInsufficientError, fit
    except ImportError:
        pass  # Allows comparison using this script against the untouched baseline.
    else:
        from intaris.judge_simulator import classify

        class OfflineCapture:
            judge_response_mode = "json_schema"
            input_view = None

            def generate(self, messages, **kwargs):
                payload = {"messages": messages, **kwargs}
                _, self.input_view = fit(payload)
                self.input_view["boundary"] = (
                    "offline llm.generate arguments; transport model/options not recovered"
                )
                # Fixed non-authorizing test answer; never persisted or reported as evidence.
                return json.dumps(
                    {
                        "user_would": "ask",
                        "risk": "low",
                        "confidence": "high",
                        "basis": "missing_permission",
                        "cites": [],
                        "question": "Offline request capture only.",
                    }
                )

        client = OfflineCapture()
        try:
            classify(client, question)
            output["final_request"] = client.input_view
        except ContextInsufficientError as exc:
            output["final_request"] = exc.diagnostics
    return question, output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture")
    args = parser.parse_args()
    _, summary = reconstruct(args.capture)
    print(json.dumps(summary, sort_keys=True, indent=2))
