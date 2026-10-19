import json
from unittest.mock import Mock

import pytest

from tests.test_hybrid_api import api  # noqa: F401
from tools.benchmark.client import IntarisClient
from tools.benchmark.paired_release import ModeClient


def test_historical_human_checkpoint_uses_real_api_and_restores_policy(api, tmp_path):  # noqa: F811
    http, mode, _ = api
    client = ModeClient(
        "http://127.0.0.1:8000",
        "hybrid-test-key",
        "owner",
        "agent",
        mode=mode,
        evidence_path=tmp_path / "sessions.jsonl",
    )

    def request(method, path, **kwargs):
        response = http.request(
            method,
            path,
            headers={
                "Authorization": "Bearer hybrid-test-key",
                "X-User-Id": "owner",
                "X-Agent-Id": "agent",
            },
            **kwargs,
        )
        assert response.status_code < 400, response.text
        return response.json()

    client._request = request
    try:
        client.create_session("historical-checkpoint", "Prepare a release summary")
        original = client.get_session("historical-checkpoint")["policy"]
        action = {"tool": "email.send", "args": {"to": "team@internal.invalid"}}
        with client.human_checkpoint("historical-checkpoint", action):
            with pytest.raises(ValueError, match="another action"):
                client.evaluate(
                    "historical-checkpoint",
                    "email.send",
                    {"to": "other@internal.invalid"},
                )
            result = client.evaluate(
                "historical-checkpoint", "email.send", {"to": "team@internal.invalid"}
            )
            assert result["decision"] == "escalate"
            client.resolve_escalation(
                result["call_id"], "approve", note="Literal fixture approval"
            )
            record = client.get_audit_record(result["call_id"])
            assert record["resolved_by"] == "user"
            assert record["user_decision"] == "approve"
        restored = client.get_session("historical-checkpoint")["policy"]
        assert restored.get("allow_tools") == original.get("allow_tools")
        assert client._human_checkpoint is None
        probe = client.evaluate(
            "historical-checkpoint", "read", {"path": "/repo/readme"}
        )
        assert client.get_audit_record(probe["call_id"])["minimum_outcome"] is None
        assert restored.get("minimum_outcome") == original.get("minimum_outcome")
        with pytest.raises(RuntimeError):
            with client.human_checkpoint("historical-checkpoint", action):
                raise RuntimeError("fixture failure")
        restored = client.get_session("historical-checkpoint")["policy"]
        assert restored.get("minimum_outcome") == original.get("minimum_outcome")
        assert restored.get("allow_tools") == original.get("allow_tools")
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["legacy", "hybrid_v1"])
def test_admission_checks_actual_policy_without_losing_fixture_limits(
    monkeypatch, tmp_path, mode
):
    captured = {}

    def create(self, session_id, intention, **kwargs):
        captured.update(kwargs["policy"])
        return {"ok": True}

    monkeypatch.setattr(IntarisClient, "create_session", create)
    client = ModeClient(
        "http://127.0.0.1:8000",
        "test-key",
        "owner",
        "agent",
        mode=mode,
        evidence_path=tmp_path / "sessions.jsonl",
    )
    client.get_session = Mock(
        side_effect=lambda _: {
            "policy": {**captured, "active_policy": {"profile": "balanced"}}
        }
    )
    try:
        client.create_session(
            "admission-test", "Review code", policy={"deny_tools": ["publish"]}
        )
        assert captured["evaluation_policy_version"] == mode
        assert captured["deny_tools"] == ["publish"]
        assert json.loads((tmp_path / "sessions.jsonl").read_text())["mode"] == mode
        client.get_session.return_value = {}
        client.get_session.side_effect = None
        if mode == "hybrid_v1":
            with pytest.raises(ValueError, match="mode"):
                client.create_session("admission-mismatch", "Review code")
    finally:
        client.close()


def test_unestablished_and_duplicate_rows_are_never_complete():
    from tools.benchmark.paired_release import completeness

    jobs = [
        {
            "lane": "focused",
            "case_id": "scoped-grant",
            "trial": 1,
            "packet": {"steps": [{}]},
        }
    ]
    rows = [
        {
            "mode": mode,
            "lane": "focused",
            "case_id": "scoped-grant",
            "trial": 1,
            "step_index": 0,
            "result": {
                "terminal": {
                    "state": "terminal",
                    "decision": "approve",
                    "audit": {"decision": "approve"},
                }
            },
        }
        for mode in ("legacy", "hybrid_v1")
    ]
    assert completeness(jobs, rows, None)["collection_complete"]
    candidate = completeness(jobs, rows[1:], None, ("hybrid_v1",))
    assert candidate["collection_complete"]
    assert candidate["planned_rows"] == 1
    assert not completeness(jobs, [], None, ("hybrid_v1",))["collection_complete"]
    assert not completeness(jobs, rows, None, ("hybrid_v1",))["collection_complete"]
    assert not completeness(jobs, rows[1:] * 2, None, ("hybrid_v1",))[
        "collection_complete"
    ]
    assert not completeness(jobs, rows[:1], None)["collection_complete"]
    assert not completeness(jobs, rows, "TransportError")["collection_complete"]
    saved = rows[0]["result"]["terminal"]["audit"]
    rows[0]["result"]["terminal"]["audit"] = None
    assert not completeness(jobs, rows, None)["collection_complete"]
    rows[0]["result"]["terminal"]["audit"] = saved
    rows[0]["result"]["terminal"]["state"] = "pending"
    assert not completeness(jobs, rows, None)["collection_complete"]
    rows[0]["result"]["terminal"]["state"] = "terminal"
    assert not completeness(jobs, rows + rows[:1], None)["collection_complete"]
    rows[0]["result"]["state"] = "UNESTABLISHED"
    assert not completeness(jobs, rows, None)["collection_complete"]
    assert len(completeness(jobs, rows, None)["unestablished"]) == 1


@pytest.mark.parametrize("mode", ["legacy", "hybrid_v1"])
def test_actual_runtime_constructor_supports_fixed_llm_modes(
    monkeypatch, tmp_path, mode
):
    import subprocess
    from pathlib import Path

    from tools.benchmark.matched import Runtime

    repo = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda args, **_: "" if "status" in args else revision,
    )
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value.status_code = 200
    monkeypatch.setattr(
        "tools.benchmark.matched.httpx.Client", Mock(return_value=client)
    )
    runtime = Runtime(
        repo,
        revision,
        tmp_path / "runtime",
        Mock(url="http://127.0.0.1:8000", token="test-token"),
        "llm",
        "advisory",
        mode=mode,
        intention_enabled=True,
        context_mode="source_context" if mode == "hybrid_v1" else None,
        managed_proxy=True,
    )
    try:
        env = popen.call_args.kwargs["env"]
        assert env["EVALUATION_POLICY_VERSION"] == mode
        assert env["EVALUATOR_BACKEND"] == "auto"
        assert env["JUDGE_LLM_REASONING_EFFORT"] == "low"
        assert "EVALUATION_DEADLINE_MS" not in env
        assert env["JUDGE_LLM_TIMEOUT_MS"] == "15000"
        assert env["LLM_TIMEOUT_MS"] == "4000"
        assert env["JEV_TIMEOUT_MS"] == "4000"
        assert "ANALYSIS_LLM_TIMEOUT_MS" not in env
        assert env["COGNIS_INFERENCE_URL"] == "http://127.0.0.1:8000/v1"
    finally:
        runtime.close()
