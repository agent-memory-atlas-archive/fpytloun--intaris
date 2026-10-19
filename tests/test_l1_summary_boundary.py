from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from intaris.hybrid_runtime import HybridRuntime


@pytest.mark.parametrize(
    "artifact,expected",
    [
        ({"status": "ready", "compact_intention": "", "items": []}, ""),
        (None, "Generated summary mentioning an unrelated human approval"),
        (
            {
                "status": "not_required",
                "fallback": "mixed_no_user_evidence",
                "compact_intention": "",
                "items": [],
            },
            "Generated summary mentioning an unrelated human approval",
        ),
    ],
)
def test_intentionally_empty_rich_summary_is_not_repopulated(
    monkeypatch, artifact, expected
):
    request = Mock(return_value={"prompt": "Classify action"})
    monkeypatch.setattr("intaris.hybrid_runtime.policy_assessment.request", request)
    runtime = object.__new__(HybridRuntime)
    runtime.projection = "lines"
    runtime.strict_output = False
    runtime._l1_request(
        SimpleNamespace(observations={}),
        None,
        session={
            "intention": "Generated summary mentioning an unrelated human approval"
        },
        tool="email.send",
        args={},
        context=None,
        agent_id=None,
        parent_intention=None,
        artifact=artifact,
        overlay={},
    )
    assert request.call_args.kwargs["task_context"]["compact_intention"] == expected
