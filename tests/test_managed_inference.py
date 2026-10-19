"""Managed inference starts before Cognis and follows credential rotation."""

import json

import httpx
import pytest
from openai import OpenAI

from intaris.config import load_config
from intaris.llm import LLMClient
from intaris.search.embeddings import _strip_provider_prefix


def test_managed_startup_and_rotation(monkeypatch, tmp_path):
    token_file = tmp_path / "token.jwt"
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://cognis:8080/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LLM_BASE_URL", "https://unrelated-upstream.invalid/v1")
    config = load_config()
    assert config.llm.model == "routes/intaris_evaluation"
    assert config.llm_analysis.model == "routes/intaris_analysis"
    assert config.llm_l3_analysis.model == "routes/intaris_analysis_deep"
    assert config.llm_judge.model == "routes/intaris_judge"
    assert config.llm.base_url == "http://cognis:8080/v1"
    assert config.llm.reasoning_effort is None
    LLMClient(config.llm)
    assert (
        _strip_provider_prefix(config.search.embedding_model)
        == "routes/intaris_embedding"
    )
    supplier = config.search.resolve_embedding_api_key()
    with pytest.raises(RuntimeError, match="not available yet"):
        supplier()
    observed = []

    def handle(request):
        observed.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "routes/intaris_evaluation",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with OpenAI(
        api_key=config.llm.api_key,
        base_url=config.llm.base_url,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        for token in ("service-token-before-rotation", "service-token-after-rotation"):
            token_file.write_text(token)
            client.chat.completions.create(
                model=config.llm.model, messages=[{"role": "user", "content": "Check"}]
            )
    assert observed == [
        "Bearer service-token-before-rotation",
        "Bearer service-token-after-rotation",
    ]


def test_partial_managed_configuration_is_rejected(monkeypatch, tmp_path):
    monkeypatch.delenv("COGNIS_INFERENCE_URL", raising=False)
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(tmp_path / "token.jwt"))
    with pytest.raises(ValueError, match="Managed inference requires"):
        load_config()


@pytest.mark.parametrize("explicit", [False, True])
def test_all_managed_roles_omit_implicit_effort_on_sdk_wire(
    monkeypatch, tmp_path, explicit
):
    from intaris import llm as module

    monkeypatch.setenv("COGNIS_INFERENCE_URL", "http://cognis:8080/v1")
    token = tmp_path / "token.jwt"
    token.write_text("local-test-credential")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    for key in (
        "LLM_REASONING_EFFORT",
        "ANALYSIS_LLM_REASONING_EFFORT",
        "ANALYSIS_L3_LLM_REASONING_EFFORT",
        "JUDGE_LLM_REASONING_EFFORT",
    ):
        if explicit:
            monkeypatch.setenv(key, "high")
        else:
            monkeypatch.delenv(key, raising=False)
    observed = []

    def handle(request):
        observed.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-effort",
                "object": "chat.completion",
                "created": 0,
                "model": observed[-1]["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    monkeypatch.setattr(
        module,
        "OpenAI",
        lambda **kwargs: OpenAI(
            **kwargs, http_client=httpx.Client(transport=httpx.MockTransport(handle))
        ),
    )
    cfg = load_config()
    for role in (cfg.llm, cfg.llm_analysis, cfg.llm_l3_analysis, cfg.llm_judge):
        client = LLMClient(role)
        try:
            client.generate([{"role": "user", "content": "Check"}])
        finally:
            client.close()
    assert len(observed) == 4
    for request in observed:
        if explicit:
            assert request["reasoning_effort"] == "high"
        else:
            assert "reasoning_effort" not in request
