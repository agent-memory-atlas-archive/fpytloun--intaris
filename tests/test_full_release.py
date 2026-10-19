"""Full-corpus planning and public lifecycle reuse; no paid inference."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.benchmark.full_release import (
    SourceFactory,
    planned_jobs,
    qualification,
    run_history_story,
)
from tools.benchmark.release_proxy import continued_budget
from tools.benchmark.source_context import schedule
from tools.benchmark.source_history_manifest import load_manifest
from tools.benchmark.source_local_factory import LocalFactory


def test_full_candidate_retains_every_case_and_all_three_trials():
    path = Path("tools/benchmark/fixtures/source-context-comparison-v1.json")
    manifest = json.loads(path.read_text())
    jobs = planned_jobs(manifest)
    assert sum(job["expected_calls"] for job in jobs) == 225
    assert {job["trial"] for job in jobs} == {1, 2, 3}
    assert {job["lane"] for job in jobs} == {"focused", "original47", "git"}
    assert {(j["lane"], j["case_id"]) for j in jobs} == {
        (j["lane"], j["case_id"]) for j in schedule(manifest)
    }
    assert all(j["backend"] == "jev" and j["arm"] == "source_context" for j in jobs)
    assert sum(len(s["checkpoints"]) for s in load_manifest()["stories"]) == 48
    assert SourceFactory.replay is LocalFactory.replay


def test_factory_rejects_unavailable_or_unrequested_arms():
    factory = object.__new__(SourceFactory)
    for args in (
        ("baseline", "jev", "advisory"),
        ("source_context", "llm", "advisory"),
    ):
        with pytest.raises(ValueError):
            factory._runtime(*args)


def test_public_lifecycle_keeps_packet_policy_and_uses_recorded_client(
    monkeypatch, tmp_path
):
    factory = object.__new__(SourceFactory)
    factory.directory = tmp_path / "runtime"
    runtime = SimpleNamespace(url="http://127.0.0.1:8000", api_key="test-local")
    factory._runtime = Mock(return_value=runtime)
    client = Mock()
    constructor = Mock(return_value=client)
    monkeypatch.setattr("tools.benchmark.full_release.SourceClient", constructor)
    policy = {"deny_paths": ["/private/*"]}
    lifecycle = factory.open_history(
        arm="source_context",
        trial=1,
        backend="jev",
        judge="advisory",
        relay=Mock(),
        initial_assignment="Read docs",
        policy=policy,
        session_id="test-source",
    )
    client.create_session.assert_called_once_with(
        "test-source",
        "Read docs",
        policy=policy,
        details=None,
    )
    assert lifecycle.client is client
    assert constructor.call_args.kwargs["request_retries"] == 0


def test_stopped_or_failing_continuation_never_resets_stop_state():
    previous = continued_budget(None).snapshot()
    for delta in (
        {"stop_reason": "three_consecutive_upstream_failures"},
        {"consecutive_failures": 2},
    ):
        with pytest.raises(ValueError, match="explicit review"):
            continued_budget(previous | delta)


def test_failed_rows_or_missing_probe_are_not_successful_collection():
    plan = {"primary_observations": 1, "history_checkpoints": 1}
    primary = [{"result": {"terminal": {"state": "terminal"}}}]
    history = [{"snapshot": {"generation_status": "ready"}, "probe_expected": True}]
    result = qualification(primary, history, plan, None)
    assert result["denominator_complete"]
    assert not result["collection_complete"]
    assert result["history_unestablished"] == 1
    history[0]["probe_observation"] = {"terminal_verified": True}
    assert qualification(primary, history, plan, None)["collection_complete"]
    history[0]["recording_omissions"] = [{"kind": "missing Judge-origin decision"}]
    assert not qualification(primary, history, plan, None)["collection_complete"]


def test_every_real_history_checkpoint_reaches_probe_with_frozen_coverage(
    monkeypatch, tmp_path
):
    data = load_manifest()
    monkeypatch.setattr(
        "tools.benchmark.full_release.read_terminal",
        lambda *a, **k: {"state": "terminal", "decision": "escalate"},
    )
    checkpoints = probes = 0
    for story in data["stories"]:
        lifecycle = Mock()
        lifecycle.records = {}
        lifecycle.omissions = []
        lifecycle.hooks.capture.return_value = {
            "generation_status": "ready",
            "coverage": {},
        }
        lifecycle.client.evaluate.return_value = {"decision": "escalate"}
        factory = Mock()
        factory.open_history.return_value = lifecycle
        proxy = SimpleNamespace(context={}, stopped=False)
        rows = list(
            run_history_story(
                factory, proxy, data, story, 1, tmp_path / "history.jsonl"
            )
        )
        assert all("error" not in row for row in rows)
        assert all("declared_coverage" in row for row in rows)
        expected = sum("probe" in c for c in story["checkpoints"])
        assert lifecycle.client.evaluate.call_count == expected
        checkpoints += len(rows)
        probes += expected
    assert checkpoints == 48
    assert probes == 12


def test_all_primary_packets_replay_including_untrusted_agent_role(monkeypatch):
    manifest = json.loads(
        Path("tools/benchmark/fixtures/source-context-comparison-v1.json").read_text()
    )
    monkeypatch.setattr(
        "tools.benchmark.source_local_factory.read_terminal",
        lambda *a, **k: {"state": "terminal", "decision": "approve"},
    )
    monkeypatch.setattr(
        "tools.benchmark.source_local_factory.api_artifact", lambda *a, **k: {}
    )
    seen_agent = False
    for job in planned_jobs(manifest):
        lifecycle = Mock()
        lifecycle.session_id = "test-session"
        lifecycle.client.evaluate.return_value = {"call_id": "test-call"}
        lifecycle.client.last_evaluation = None
        lifecycle.record.return_value = {"call_id": "test-control"}
        factory = object.__new__(SourceFactory)
        factory.open_history = Mock(return_value=lifecycle)
        relay = SimpleNamespace(
            records=[],
            context={},
            validation_errors=[],
            stopped=False,
            budget=continued_budget(None),
            drain=lambda: None,
        )
        rows = factory.replay(
            packet=job["packet"],
            arm=job["arm"],
            backend=job["backend"],
            judge=job["judge"],
            trial=job["trial"],
            relay=relay,
            session_id="test-session",
        )
        assert len(rows) == job["expected_calls"]
        if any(e.get("role") == "agent" for e in job["packet"].get("events", [])):
            assert any(
                c.kwargs.get("origin") == "agent"
                for c in lifecycle.record.call_args_list
            )
            seen_agent = True
    assert seen_agent


def test_reviewed_transport_restart_preserves_failure_and_reserved_spend():
    previous = continued_budget(None).snapshot() | {
        "attempts": 68,
        "stop_reason": "proxy_bridge_failed",
        "consecutive_failures": 1,
        "unknown_reserved_usd": 0.0589824,
    }
    resumed = continued_budget(
        previous,
        reviewed_transport_restart="Reviewed interrupted bridge after harness crash",
    )
    assert resumed.snapshot()["consecutive_failures"] == 1
    assert resumed.max_attempts == 1932
    assert resumed.max_usd == pytest.approx(10 - 0.0589824)
    for _ in range(2):
        attempt = resumed.reserve()
        resumed.finish(
            attempt,
            {
                "provider": "groq",
                "model": "openai/gpt-oss-120b",
                "status": 502,
            },
        )
    assert resumed.snapshot()["stop_reason"] == "three_consecutive_upstream_failures"
    assert resumed.reserve() is None
    with pytest.raises(ValueError, match="exhausted"):
        continued_budget(
            previous | {"known_or_estimated_usd": 10},
            reviewed_transport_restart="Transport review cannot increase dollars",
        )
    for reason in (
        "three_consecutive_upstream_failures",
        "verified_price_bound_exceeded",
        "cumulative_attempt_cap",
    ):
        with pytest.raises(ValueError):
            continued_budget(
                previous | {"stop_reason": reason},
                reviewed_transport_restart="Review cannot override this stop",
            )
