import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tests.test_hybrid_api import api as _api
from tools.benchmark.release_proxy import (
    BRIDGE,
    Proxy,
    RecordedClient,
    completeness,
    continued_budget,
    load_previous_completion,
    startup_failure_budget,
    verified_proxy_url,
)

api = _api


@pytest.fixture(autouse=True)
def no_live_replica_discovery(monkeypatch):
    monkeypatch.setenv("INTARIS_BENCHMARK_NAMESPACE", "example-app")
    monkeypatch.setenv("INTARIS_BENCHMARK_COGNIS_POD", "controller-0")
    monkeypatch.setenv("INTARIS_BENCHMARK_DEPLOYMENT", "guardrails")
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.verified_proxy_url",
        lambda _: "http://127.0.0.1:8080/v1",
    )


def metadata():
    rows = []
    binding = {}
    for role in ("evaluation", "analysis", "judge"):
        route = f"routes/intaris_{role}"
        native = role == "evaluation"
        binding[route] = {
            "provider_id": "openrouter" if native else "groq",
            "model": "typesafe/jev-1.13" if native else "openai/gpt-oss-120b",
            "resolution_fingerprint": "test-bound-route",
        }
        rows.append(
            {
                "id": route,
                "resolution_fingerprint": "test-bound-route",
                "cognis": {"operations": ["systemone" if native else "chat"]},
                "max_input_tokens": 32000 if native else 131072,
                "max_output_tokens": 28800 if native else 65536,
            }
        )
    return {"status": 200, "body": {"data": rows}, "proxy_ms": 0}, binding


def proxy(monkeypatch, tmp_path, replies=()):
    models, binding = metadata()
    process = Mock()
    process.stdin = io.StringIO()
    process.stdout = io.StringIO(
        "\n".join(json.dumps(r) for r in [models, *replies]) + "\n"
    )
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.subprocess.Popen", Mock(return_value=process)
    )
    return Proxy(tmp_path, binding), process


def test_bridge_uses_explicit_deployment(monkeypatch):
    process = Mock()
    launch = Mock(return_value=process)
    monkeypatch.setattr("tools.benchmark.release_proxy.subprocess.Popen", launch)
    proxy = object.__new__(Proxy)
    proxy.start_bridge("http://127.0.0.1:8080/v1")
    assert launch.call_args.args[0][:7] == [
        "kubectl",
        "-n",
        "example-app",
        "exec",
        "-i",
        "deployment/guardrails",
        "--",
    ]


@pytest.mark.parametrize(
    "setting",
    ["INTARIS_BENCHMARK_NAMESPACE", "INTARIS_BENCHMARK_DEPLOYMENT"],
)
def test_bridge_requires_explicit_deployment(monkeypatch, setting):
    monkeypatch.delenv(setting)
    launch = Mock()
    monkeypatch.setattr("tools.benchmark.release_proxy.subprocess.Popen", launch)
    proxy = object.__new__(Proxy)
    with pytest.raises(KeyError, match=setting):
        proxy.start_bridge("http://127.0.0.1:8080/v1")
    launch.assert_not_called()


def test_model_resolution_and_priced_limits_are_verified():
    models, binding = metadata()
    Proxy.verify_binding(models, binding)
    binding["routes/intaris_judge"]["model"] = "unpriced-model"
    with pytest.raises(ValueError):
        Proxy.verify_binding(models, binding)
    models, binding = metadata()
    models["body"]["data"][0]["resolution_fingerprint"] = "changed-route"
    with pytest.raises(ValueError):
        Proxy.verify_binding(models, binding)


def test_proxy_rejects_wrong_route_or_auth_without_spend(monkeypatch, tmp_path):
    p, process = proxy(monkeypatch, tmp_path)
    try:
        assert httpx.post(p.url + "/judge/chat/completions", json={}).status_code == 403
        assert (
            httpx.post(
                p.url + "/unapproved/chat/completions",
                headers={"Authorization": f"Bearer {p.token}"},
                json={},
            ).status_code
            == 403
        )
        assert p.budget.snapshot()["attempts"] == 0
        assert len(process.stdin.getvalue().splitlines()) == 1
    finally:
        p.close()


def test_metadata_query_is_preserved_without_spend(monkeypatch, tmp_path):
    models, _ = metadata()
    p, process = proxy(monkeypatch, tmp_path, [models])
    try:
        response = httpx.get(
            p.url + "/jev/v1/models?operation=systemone",
            headers={"Authorization": f"Bearer {p.token}"},
        )
        assert response.json() == models["body"]
        sent = json.loads(process.stdin.getvalue().splitlines()[-1])
        assert sent["path"] == "/models?operation=systemone"
        assert p.budget.snapshot()["attempts"] == 0
    finally:
        p.close()


def test_retry_error_fields_and_unknown_cost_are_preserved(monkeypatch, tmp_path):
    reply = {
        "status": 429,
        "headers": {"retry-after": "5"},
        "body": {"error": {"code": "rate_limit", "message": "slow down"}},
        "proxy_ms": 4,
    }
    p, process = proxy(monkeypatch, tmp_path, [reply, reply, reply])
    monkeypatch.setattr(p, "recover", lambda **kwargs: None)
    monkeypatch.setattr("tools.benchmark.release_proxy.time.sleep", lambda _: None)
    try:
        response = httpx.post(
            p.url + "/judge/chat/completions",
            headers={"Authorization": f"Bearer {p.token}"},
            json={"messages": [], "max_tokens": 16},
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "5"
        assert response.json() == reply["body"]
        assert p.budget.snapshot()["unknown_attempts"] == 3
        assert p.budget.snapshot()["unknown_reserved_usd"] > 0
        sent = json.loads(process.stdin.getvalue().splitlines()[-1])
        assert sent["body"]["model"] == "routes/intaris_judge"
        assert sent["fingerprint"] == "test-bound-route"
    finally:
        p.close()


def test_partial_budget_stop_retains_planned_denominator():
    manifest = {"scenarios": [{"id": "scenario", "steps": [{"id": "a"}, {"id": "b"}]}]}
    result = completeness(
        manifest,
        3,
        [{"trial": 1, "scenario": "scenario", "case": "a"}],
        {"stop_reason": "cumulative_price_cap"},
        None,
    )
    assert result["planned_observations"] == 6
    assert result["collected_observations"] == 1
    assert len(result["missing_observations"]) == 5
    assert result["collection_complete"] is False


def test_continuation_preserves_all_reservations_and_startup_chain():
    previous = continued_budget(None).snapshot()
    previous.update(
        attempts=88,
        known_or_estimated_usd=2,
        unknown_reserved_usd=1,
        active_reservation_usd=0.5,
    )
    current = continued_budget(previous).snapshot()
    assert current["max_attempts"] == 1912
    assert current["max_usd"] == 6.5
    failed = startup_failure_budget(previous)
    assert failed == previous
    assert continued_budget(failed).snapshot()["max_usd"] == 6.5
    with pytest.raises(ValueError, match="Incomplete"):
        continued_budget({"attempts": 0})
    with pytest.raises(ValueError, match="exhausted"):
        continued_budget({**previous, "attempts": 2000})
    with pytest.raises(ValueError, match="exhausted"):
        continued_budget({**previous, "known_or_estimated_usd": 10})
    with pytest.raises(ValueError, match="Invalid"):
        continued_budget({**previous, "known_or_estimated_usd": float("nan")})


def test_null_completion_cannot_reset_budget(tmp_path):
    previous = tmp_path / "completion.json"
    for value in ({"budget": None}, {}, [], {"budget": {"attempts": 0}}):
        previous.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load_previous_completion(str(previous))


def test_managed_config_preflight_without_any_inference(monkeypatch, tmp_path):
    from intaris.config import load_config
    from intaris.inference import managed_endpoint

    token = tmp_path / "local-token"
    token.write_text("local-test-token")
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:18767/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    monkeypatch.setenv("EVALUATOR_BACKEND", "auto")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = load_config()
    assert managed_endpoint() == "http://127.0.0.1:18767/v1"
    assert cfg.jev.enabled
    assert cfg.jev.base_url == "http://127.0.0.1:18767"
    assert cfg.llm.model == "routes/intaris_evaluation"
    assert cfg.llm_analysis.model == "routes/intaris_analysis"
    assert cfg.llm_judge.model == "routes/intaris_judge"


@pytest.mark.parametrize(
    "setting", ["INTARIS_BENCHMARK_NAMESPACE", "INTARIS_BENCHMARK_COGNIS_POD"]
)
def test_replica_discovery_requires_configuration(monkeypatch, setting):
    monkeypatch.delenv(setting)
    discovery = Mock()
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.subprocess.check_output", discovery
    )
    with pytest.raises(KeyError, match=setting):
        verified_proxy_url({})
    discovery.assert_not_called()


def test_pinned_replica_uses_only_live_kubernetes_identity(monkeypatch):
    monkeypatch.setenv("INTARIS_BENCHMARK_NAMESPACE", "example-app")
    monkeypatch.setenv("INTARIS_BENCHMARK_COGNIS_POD", "controller-0")
    pod = {
        "metadata": {"uid": "test-pod"},
        "status": {"podIP": "10.0.0.2"},
        "spec": {"containers": [{"ports": [{"name": "http", "containerPort": 8080}]}]},
    }
    discovery = Mock(return_value=json.dumps(pod))
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.subprocess.check_output", discovery
    )
    binding = {
        "_transport": {"pod_uid": "test-pod", "base_url": "http://10.0.0.2:8080/v1"}
    }
    assert verified_proxy_url(binding) == "http://10.0.0.2:8080/v1"
    discovery.assert_called_once_with(
        ["kubectl", "-n", "example-app", "get", "pod", "controller-0", "-o", "json"],
        text=True,
    )
    binding["_transport"]["base_url"] = "https://unauthorized.test/v1"
    with pytest.raises(ValueError, match="replica changed"):
        verified_proxy_url(binding)


def test_session_mode_is_explicit_and_verified_before_evaluation(monkeypatch, tmp_path):
    calls = []
    client = RecordedClient(
        "http://127.0.0.1:18767",
        "local-test",
        "bench-user",
        "bench-agent",
        evidence_path=tmp_path / "sessions.jsonl",
        request_retries=0,
    )

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            return {"ok": True}
        return {
            "policy": {"evaluation_policy_version": "hybrid_v1", "profile": "balanced"}
        }

    monkeypatch.setattr(client, "_request", request)
    try:
        client.create_session(
            "test-session", "Read docs", policy={"deny_paths": ["/secrets/*"]}
        )
        policy = calls[0][2]["json"]["policy"]
        assert policy["evaluation_policy_version"] == "hybrid_v1"
        assert policy["deny_paths"] == ["/secrets/*"]
        monkeypatch.setattr(client, "get_session", lambda _: {"policy": None})
        with pytest.raises(RuntimeError, match="did not activate"):
            client.create_session("test-session-2", "Read docs")
    finally:
        client.close()


def test_real_session_admission_does_not_default_to_legacy(api):
    client = api[0]
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    result = client.post(
        "/api/v1/intention",
        json={
            "session_id": "release-mode-regression",
            "intention": "Read project documentation.",
            "policy": {
                "evaluation_policy_version": "hybrid_v1",
                "profile": "balanced",
                "interaction_mode": "step_requests",
            },
        },
    )
    assert result.status_code == 200, result.text
    observed = client.get("/api/v1/session/release-mode-regression").json()
    assert observed["policy"]["evaluation_policy_version"] == "hybrid_v1"
    assert observed["policy"]["profile"] == "balanced"


def test_startup_failure_closes_bridge(monkeypatch, tmp_path):
    process = Mock()
    process.stdin = io.StringIO()
    process.stdout = io.StringIO("not json\n")
    monkeypatch.setattr(
        "tools.benchmark.release_proxy.subprocess.Popen", Mock(return_value=process)
    )
    with pytest.raises(ValueError):
        Proxy(tmp_path, {})
    process.terminate.assert_called_once()
    process.wait.assert_called_once()


def run_bridge(monkeypatch, client, request):
    client.with_options.return_value = client
    monkeypatch.setattr("intaris.config.load_config", lambda: SimpleNamespace(llm=None))
    monkeypatch.setattr(
        "intaris.llm.LLMClient", lambda *a: SimpleNamespace(_client=client)
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr("sys.argv", ["bridge", "http://127.0.0.1:8080/v1"])
    output = io.StringIO()
    with redirect_stdout(output):
        exec(compile(BRIDGE, "<benchmark-bridge>", "exec"), {})
    return json.loads(output.getvalue())


def test_bridge_refuses_changed_fingerprint_before_inference(monkeypatch):
    client = Mock()
    models, _ = metadata()
    client.get.return_value = models["body"]
    result = run_bridge(
        monkeypatch,
        client,
        {
            "path": "/systemone",
            "body": {"model": "routes/intaris_evaluation"},
            "fingerprint": "outdated-binding",
        },
    )
    assert result["status"] == 409
    client.post.assert_not_called()
    client.with_options.assert_called_once_with(
        max_retries=0, base_url="http://127.0.0.1:8080/v1"
    )


def test_bridge_preserves_recoverable_error_without_unrelated_fields(monkeypatch):
    class UpstreamError(Exception):
        status_code = 400
        body = {
            "error": {
                "message": "Invalid JSON",
                "code": "json_validate_failed",
                "failed_generation": '{"answer": true}',
                "unrelated": "not forwarded",
            }
        }
        response = SimpleNamespace(
            headers={"retry-after": "3", "authorization": "not forwarded"}
        )

    client = Mock()
    models, _ = metadata()
    client.get.return_value = models["body"]
    client.post.side_effect = UpstreamError()
    result = run_bridge(
        monkeypatch,
        client,
        {
            "path": "/chat/completions",
            "body": {"model": "routes/intaris_judge"},
            "fingerprint": "test-bound-route",
        },
    )
    assert result["status"] == 400
    assert result["body"]["error"]["failed_generation"] == '{"answer": true}'
    assert "unrelated" not in result["body"]["error"]
    assert result["headers"] == {"retry-after": "3"}
