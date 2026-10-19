"""Exact outgoing request tests: structural reduction, not relaxed model limits."""

import json

import pytest

from intaris import policy_assessment
from intaris.input_budget import Capacity, measure
from tests.test_jev import _client
from tests.test_raw_projection_budget import question


@pytest.mark.parametrize("capacity", [Capacity(), Capacity(max_input_tokens=32000)])
def test_jev_sends_each_schema_once_and_retains_protected_evidence(
    capacity, monkeypatch
):
    q = question("lines")
    instruction = "User: inspect /repo/file; do not push. "
    q["prompt"] = instruction
    client, requests = _client({"answers": {}})
    try:
        monkeypatch.setattr(client, "input_capacity", lambda: capacity)
        # Find the largest protected body fitting the real transport, then
        # demonstrate the previous schema duplication crossing the same cap.
        captured = []
        real_request = client._request

        def capture(*, state, questions):
            captured.append(
                {"state": state, "model": client._model, "questions": questions}
            )
            raise RuntimeError("captured")

        monkeypatch.setattr(client, "_request", capture)
        with pytest.raises(RuntimeError, match="captured"):
            policy_assessment.classify(None, client, q)
        template = captured[0]
        low, high = 0, 120000
        prefix = template["state"]["prompt"]
        while low < high:
            mid = (low + high + 1) // 2
            candidate = {**template, "state": {"prompt": prefix + "x" * mid}}
            if measure(candidate, capacity, jev=True)["overflow"]:
                high = mid - 1
            else:
                low = mid
        q["prompt"] = instruction + "x" * max(0, low - 20)
        monkeypatch.setattr(client, "_request", real_request)
        with pytest.raises(ValueError, match="missing answer"):
            policy_assessment.classify(None, client, q)
    finally:
        client.close()
    assert len(requests) == 1
    actual = json.loads(requests[0].content)
    assert instruction in actual["state"]["prompt"]
    assert "selective_options" not in actual["state"]
    assert "Output schema:" not in actual["state"]["prompt"]
    options_by_name = {
        name: tuple(spec["criteria"]) for name, spec in template["questions"].items()
    }
    for name, options in options_by_name.items():
        assert tuple(actual["questions"][name]["criteria"]) == options
    assert not measure(actual, capacity, jev=True)["overflow"]
    old_state = {
        **actual["state"],
        "prompt": actual["state"]["prompt"]
        + "\nOutput schema: "
        + json.dumps(q["schema"], separators=(",", ":")),
        "selective_options": options_by_name,
    }
    assert measure({**actual, "state": old_state}, capacity, jev=True)["overflow"]


def test_llm_system_prompt_keeps_required_output_schema():
    q = question("lines")
    assert "Output schema:" in policy_assessment.system_prompt(q)
    assert "Output schema:" not in policy_assessment.system_prompt(
        q, include_schema=False
    )
