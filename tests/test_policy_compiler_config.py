"""Compiler routing is independent of L1, lazy, and part of cache identity."""

import json
import os
from unittest.mock import Mock

import pytest

from intaris.config import LLMConfig, build_policy_compiler_llm_config
from intaris.llm import LLMClient
from intaris.policy_activation import HybridError, PolicyCompiler, prepare_policy


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    for name in os.environ:
        if name.startswith(
            (
                "LLM_",
                "ANALYSIS_LLM_",
                "POLICY_COMPILER_LLM_",
                "COGNIS_INFERENCE_",
                "OPENAI_",
            )
        ):
            monkeypatch.delenv(name)


def response():
    return json.dumps(
        {
            "supported": True,
            "reason": "",
            "rules": [
                {
                    "id": "deny_policies.0",
                    "effect": "deny",
                    "when": {"op": "eq", "field": "operation", "value": "write"},
                }
            ],
        }
    )


def activate(compiler):
    return prepare_policy(
        {"evaluation_policy_version": "hybrid_v1", "deny_policies": ["Do not write"]},
        tenant="owner",
        compiler=compiler,
    )


@pytest.mark.parametrize("backend", ["llm", "jev"])
def test_compiler_dispatch_uses_analysis_not_l1(monkeypatch, backend):
    monkeypatch.setenv("EVALUATOR_BACKEND", backend)
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_API_KEY", "l1-test-key")
    monkeypatch.setenv("ANALYSIS_LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("ANALYSIS_LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("ANALYSIS_LLM_API_KEY", "analysis-test-key")
    monkeypatch.setenv("ANALYSIS_LLM_REASONING_EFFORT", "low")
    monkeypatch.setenv("ANALYSIS_LLM_TIMEOUT_MS", "45000")
    dispatched = []
    client = Mock()
    client.generate.return_value = response()

    def factory(cfg):
        dispatched.append(cfg)
        return client

    monkeypatch.setattr("intaris.llm.LLMClient", factory)
    compiler = PolicyCompiler()
    activate(compiler)
    activate(compiler)
    assert len(dispatched) == 1
    cfg = dispatched[0]
    assert cfg.model == "openai/gpt-oss-120b"
    assert cfg.base_url == "https://api.groq.com/openai/v1"
    assert cfg.api_key == "analysis-test-key"
    assert cfg.reasoning_effort == "low" and cfg.timeout_ms == 45000
    assert LLMConfig().model == "openai/gpt-oss-20b"
    assert client.generate.call_count == client.close.call_count == 1


def test_explicit_overrides_and_managed_analysis_route(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYSIS_LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("POLICY_COMPILER_LLM_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("POLICY_COMPILER_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("POLICY_COMPILER_LLM_API_KEY", "compiler-test-key")
    monkeypatch.setenv("POLICY_COMPILER_LLM_REASONING_EFFORT", "high")
    monkeypatch.setenv("POLICY_COMPILER_LLM_TIMEOUT_MS", "60000")
    cfg = build_policy_compiler_llm_config()
    assert (cfg.model, cfg.api_key, cfg.reasoning_effort, cfg.timeout_ms) == (
        "openai/gpt-oss-20b",
        "compiler-test-key",
        "high",
        60000,
    )
    assert cfg.base_url == "https://openrouter.ai/api/v1"
    monkeypatch.delenv("POLICY_COMPILER_LLM_MODEL")
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv(
        "COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "not-created-token")
    )
    cfg = build_policy_compiler_llm_config()
    assert cfg.model == "routes/intaris_analysis"
    assert cfg.base_url == "http://127.0.0.1:8000/v1"
    assert callable(cfg.api_key)  # No credential read at configuration time.
    monkeypatch.setenv("POLICY_COMPILER_LLM_MODEL", "routes/intaris_judge")
    assert build_policy_compiler_llm_config().model == "routes/intaris_judge"


def test_missing_compiler_does_not_affect_structured_activation(monkeypatch):
    factory = Mock(side_effect=AssertionError("No client required"))
    monkeypatch.setattr("intaris.llm.LLMClient", factory)
    ready = prepare_policy(
        {"evaluation_policy_version": "hybrid_v1", "rules": []},
        tenant="owner",
    )
    assert ready["profile"] == "balanced"
    with pytest.raises(HybridError) as failure:
        activate(PolicyCompiler())
    assert failure.value.status == 503
    assert failure.value.detail()["execution_state"] == "not_dispatched"
    assert not factory.called


def test_injected_client_identity_not_environment_controls_cache(monkeypatch):
    client = LLMClient(LLMConfig(model="openai/gpt-oss-120b", api_key="test-key"))
    monkeypatch.setattr(client, "generate", Mock(return_value=response()))
    compiler = PolicyCompiler(client, route="analysis")
    try:
        activate(compiler)
        monkeypatch.setenv("LLM_MODEL", "changed-evaluator")
        activate(compiler)
        assert client.generate.call_count == 1
        client._model = "openai/gpt-oss-20b"
        activate(compiler)
        assert client.generate.call_count == 2
        assert not client._closed
    finally:
        client.close()


def test_managed_compiler_dispatch_is_conventional_and_lazy(monkeypatch, tmp_path):
    monkeypatch.setenv("EVALUATOR_BACKEND", "jev")
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:8000/v1")
    token = tmp_path / "token"
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    client = Mock()
    client.generate.return_value = response()
    factory = Mock(return_value=client)
    monkeypatch.setattr("intaris.llm.LLMClient", factory)
    activate(PolicyCompiler())
    cfg = factory.call_args.args[0]
    assert cfg.model == "routes/intaris_analysis"
    token.write_text("service-test-token")
    assert cfg.api_key() == "service-test-token"


@pytest.mark.parametrize("credential", ["missing", "empty"])
def test_real_managed_client_missing_credentials_never_dispatches(
    monkeypatch, tmp_path, credential
):
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://127.0.0.1:8000/v1")
    token = tmp_path / "token"
    if credential == "empty":
        token.write_text("")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    transport = Mock(
        side_effect=AssertionError("Missing credential must never dispatch")
    )
    monkeypatch.setattr("httpx.Client.send", transport)
    closed = []
    original_close = LLMClient.close

    def close(client):
        original_close(client)
        closed.append(client._closed)

    monkeypatch.setattr(LLMClient, "close", close)
    with pytest.raises(HybridError) as failure:
        activate(PolicyCompiler())
    assert failure.value.status == 503
    assert failure.value.detail()["execution_state"] == "not_dispatched"
    assert not transport.called
    assert closed == [True]
