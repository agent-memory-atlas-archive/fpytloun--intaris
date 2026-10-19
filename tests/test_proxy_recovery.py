import json
from unittest.mock import Mock

import pytest

from tools.benchmark.proxy_recovery import BindingChanged, discover_unchanged_binding
from tools.benchmark.release_proxy import Proxy, continued_budget


def instance(tmp_path, responses):
    proxy = object.__new__(Proxy)
    proxy.output = tmp_path
    proxy.binding = {"routes/intaris_analysis": {"resolution_fingerprint": "before"}}
    proxy.budget = continued_budget(None)
    proxy.context = {}
    proxy.records = []
    proxy.exchange = Mock(side_effect=responses)
    proxy.recover = Mock()
    return proxy


def test_broken_connection_retries_with_separate_cost_reservation(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    proxy = instance(
        tmp_path,
        [
            BrokenPipeError(),
            {
                "status": 200,
                "body": {"usage": {"prompt_tokens": 10, "completion_tokens": 10}},
                "proxy_ms": 2,
            },
        ],
    )
    answer = proxy.forward("intention", "routes/intaris_analysis", {"messages": []})
    assert answer["status"] == 200
    assert proxy.exchange.call_count == 2
    proxy.recover.assert_called_once()
    budget = proxy.budget.snapshot()
    assert budget["attempts"] == 2 and budget["unknown_attempts"] == 1
    assert budget["unknown_reserved_usd"] > 0
    assert budget["consecutive_failures"] == 0
    assert len((tmp_path / "provider-attempts.jsonl").read_text().splitlines()) == 2


def test_permanent_failure_and_attempt_caps_are_not_reset(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    proxy = instance(
        tmp_path, [BrokenPipeError(), BrokenPipeError(), BrokenPipeError()]
    )
    assert proxy.forward("intention", "routes/intaris_analysis", {})["status"] == 502
    assert (
        proxy.budget.snapshot()["stop_reason"] == "three_consecutive_upstream_failures"
    )
    assert proxy.forward("intention", "routes/intaris_analysis", {})["status"] == 503
    assert proxy.exchange.call_count == 3


def test_non_transient_errors_are_not_retried(tmp_path):
    proxy = instance(
        tmp_path, [{"status": 400, "body": {"error": {"code": "bad_request"}}}]
    )
    assert proxy.forward("intention", "routes/intaris_analysis", {})["status"] == 400
    proxy.recover.assert_not_called()
    assert proxy.budget.snapshot()["attempts"] == 1


@pytest.mark.parametrize(
    "setting", ["INTARIS_BENCHMARK_NAMESPACE", "INTARIS_BENCHMARK_COGNIS_POD"]
)
def test_recovery_requires_configuration_before_discovery(monkeypatch, setting):
    monkeypatch.setenv("INTARIS_BENCHMARK_NAMESPACE", "example-app")
    monkeypatch.setenv("INTARIS_BENCHMARK_COGNIS_POD", "controller-0")
    monkeypatch.delenv(setting)
    discovery = Mock()
    monkeypatch.setattr(
        "tools.benchmark.proxy_recovery.subprocess.check_output", discovery
    )
    with pytest.raises(KeyError, match=setting):
        discover_unchanged_binding({})
    discovery.assert_not_called()


def test_discovery_rejects_reasoning_change_before_using_credentials(monkeypatch):
    monkeypatch.setenv("INTARIS_BENCHMARK_NAMESPACE", "example-app")
    monkeypatch.setenv("INTARIS_BENCHMARK_COGNIS_POD", "controller-0")
    pod = {
        "metadata": {"uid": "replacement"},
        "status": {
            "podIP": "10.0.0.3",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
        "spec": {"containers": [{"ports": [{"name": "http", "containerPort": 8080}]}]},
    }
    rows = [
        {
            "task_type": "intaris_analysis",
            "provider_id": "groq",
            "model": "openai/gpt-oss-120b",
            "reasoning_effort": "high",
        }
    ]
    monkeypatch.setattr(
        "tools.benchmark.proxy_recovery.subprocess.check_output",
        Mock(side_effect=[json.dumps(pod), json.dumps(rows)]),
    )
    with pytest.raises(BindingChanged):
        discover_unchanged_binding(
            {
                "routes/intaris_analysis": {
                    "provider_id": "groq",
                    "model": "openai/gpt-oss-120b",
                    "reasoning_effort": "low",
                }
            }
        )


@pytest.mark.parametrize(
    "review",
    [
        "verified_replica_restart: user authorized recovery",
        "verified_request_correction: managed route requires json_object mode",
    ],
)
def test_explicit_reviewed_restart_keeps_all_prior_spending(review):
    previous = continued_budget(None).snapshot() | {
        "attempts": 195,
        "stop_reason": "three_consecutive_upstream_failures",
        "consecutive_failures": 3,
        "known_or_estimated_usd": 0.1,
        "unknown_reserved_usd": 0.2,
    }
    with pytest.raises(ValueError):
        continued_budget(previous)
    result = continued_budget(
        previous,
        reviewed_transport_restart=review,
    ).snapshot()
    assert result["max_attempts"] == 1805
    assert result["max_usd"] == pytest.approx(9.7)
    assert result["consecutive_failures"] == 0


@pytest.mark.parametrize(
    "review", ["verified_request_correction:", "verified_request_correction:  "]
)
def test_request_correction_requires_review_evidence(review):
    previous = continued_budget(None).snapshot() | {
        "stop_reason": "three_consecutive_upstream_failures",
        "consecutive_failures": 3,
    }
    with pytest.raises(ValueError, match="explicit review"):
        continued_budget(previous, reviewed_transport_restart=review)


def test_reviewed_interrupted_run_preserves_nonterminal_failure_count():
    previous = continued_budget(None).snapshot() | {
        "attempts": 16,
        "consecutive_failures": 1,
        "unknown_reserved_usd": 0.0589824,
    }
    with pytest.raises(ValueError):
        continued_budget(previous)
    result = continued_budget(
        previous,
        reviewed_transport_restart="Reviewed caller deadline interruption",
    ).snapshot()
    assert result["consecutive_failures"] == 1
    assert result["max_attempts"] == 1984
    assert result["max_usd"] == pytest.approx(10 - 0.0589824)


def test_explicit_attempt_extension_does_not_reset_prior_spend(monkeypatch):
    previous = continued_budget(None).snapshot() | {
        "max_attempts": 2580,
        "attempts": 496,
        "known_or_estimated_usd": 0.178590018,
        "max_usd": 8.975841778,
    }
    with pytest.raises(ValueError):
        continued_budget(previous)
    monkeypatch.setenv("INTARIS_BENCHMARK_APPROVED_ATTEMPT_CAP", "4000")
    resumed = continued_budget(previous).snapshot()
    assert resumed["max_attempts"] == 2084
    assert resumed["max_usd"] == pytest.approx(8.79725176)


def test_recovery_window_reserves_complete_round_and_retry(monkeypatch, tmp_path):
    now = [0.0]
    monkeypatch.setattr("tools.benchmark.release_proxy.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.time.sleep",
        lambda delay: now.__setitem__(0, now[0] + delay),
    )

    def unavailable(_):
        now[0] += 20
        raise RuntimeError("Replica not ready")

    monkeypatch.setattr(
        "tools.benchmark.release_proxy.discover_unchanged_binding", unavailable
    )
    proxy = instance(tmp_path, [])
    del proxy.recover
    with pytest.raises(RuntimeError, match="exhausted"):
        proxy.recover(deadline=150)
    assert now[0] < 150
    assert proxy.budget.snapshot()["attempts"] == 0


def test_forward_reserves_next_inference_window(monkeypatch, tmp_path):
    now = [0.0]
    monkeypatch.setattr("tools.benchmark.release_proxy.time.monotonic", lambda: now[0])
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    proxy = instance(tmp_path, [BrokenPipeError()])

    def recovery(*, deadline):
        assert deadline == 410
        now[0] = deadline
        # Account for the caller's retry-after and potential scheduling delay.
        now[0] += 16

    proxy.recover = recovery
    assert proxy.forward("intention", "routes/intaris_analysis", {})["status"] == 503
    assert proxy.exchange.call_count == 1


def test_exhausted_recovery_continuation_is_explicit_and_budget_preserving():
    previous = continued_budget(None).snapshot() | {
        "stop_reason": "proxy_recovery_exhausted",
        "consecutive_failures": 1,
        "attempts": 391,
        "known_or_estimated_usd": 0.2,
        "unknown_reserved_usd": 0.1,
    }
    with pytest.raises(ValueError):
        continued_budget(previous)
    resumed = continued_budget(
        previous,
        reviewed_transport_restart="verified_replica_restart: reviewed ready replacement",
    ).snapshot()
    assert resumed["max_attempts"] == 1609
    assert resumed["max_usd"] == pytest.approx(9.7)


def test_recovery_rotates_fingerprints_not_routes_or_budget(monkeypatch, tmp_path):
    from tests.test_release_proxy import metadata

    models, binding = metadata()
    binding["_transport"] = {
        "pod_uid": "previous",
        "base_url": "http://10.0.0.2:8080/v1",
    }
    candidate = json.loads(json.dumps(binding))
    candidate["_transport"] = {
        "pod_uid": "replacement",
        "base_url": "http://10.0.0.3:8080/v1",
    }
    for row in models["body"]["data"]:
        row["resolution_fingerprint"] = "replacement-fingerprint"
    proxy = instance(tmp_path, [models])
    proxy.binding = binding
    del proxy.recover
    proxy.stop_bridge = Mock()
    proxy.start_bridge = Mock()
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.discover_unchanged_binding",
        Mock(side_effect=[candidate, candidate]),
    )
    before = proxy.budget.snapshot()
    proxy.recover()
    assert proxy.budget.snapshot() == before
    assert proxy.binding["_transport"]["pod_uid"] == "replacement"
    assert (
        proxy.binding["routes/intaris_analysis"]["resolution_fingerprint"]
        == "replacement-fingerprint"
    )
    proxy.start_bridge.assert_called_once_with("http://10.0.0.3:8080/v1")
    assert (tmp_path / "transport-recovery.jsonl").exists()


def test_recovery_fails_closed_on_changed_route(monkeypatch, tmp_path):
    proxy = instance(tmp_path, [])
    del proxy.recover
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.discover_unchanged_binding",
        Mock(side_effect=BindingChanged("model changed")),
    )
    with pytest.raises(BindingChanged):
        proxy.recover()
    assert proxy.budget.snapshot()["stop_reason"] == "proxy_route_changed"
    proxy.exchange.assert_not_called()


@pytest.mark.parametrize(
    "initial",
    [
        {"status": 503, "body": {"error": {"code": "unavailable"}}},
        BrokenPipeError(),
    ],
)
def test_startup_and_final_metadata_share_bounded_recovery(tmp_path, initial):
    from tests.test_release_proxy import metadata

    models, binding = metadata()
    proxy = instance(tmp_path, [initial, models])
    proxy.binding = binding
    assert proxy.checked_metadata() == models
    proxy.recover.assert_called_once()
    assert proxy.budget.snapshot()["attempts"] == 0


def test_benchmark_callers_outlive_transport_recovery():
    from tools.benchmark.full_release import SourceClient, SourceFactory

    assert SourceClient.review_timeout_s >= 600
    assert SourceFactory.review_timeout_s >= 600
