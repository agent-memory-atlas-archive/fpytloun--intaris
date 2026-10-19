"""Per-call evaluator backend selection against a Cognis-shaped fake."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI

from intaris.config import JevConfig, LLMConfig, evaluator_backend_mode, load_config
from intaris.evaluator import Evaluator
from intaris.evaluator_backend import (
    CACHE_SECONDS,
    EVALUATION_ROUTE,
    EvaluatorBackend,
    EvaluatorBackendError,
)
from intaris.inference import token_supplier
from intaris.jev import JevClient
from intaris.llm import LLMClient

ROUTES = (
    "routes/intaris_evaluation",
    "routes/intaris_judge",
    "routes/intaris_analysis",
)


def _jev_answers() -> dict:
    return {
        "aligned": {"type": "noul", "noul": 0.94},
        "risk": {
            "type": "choice",
            "choice": "low",
            "confidence": 0.88,
            "probabilities": {
                "low": 0.9,
                "medium": 0.05,
                "high": 0.03,
                "critical": 0.02,
            },
        },
        "decision": {
            "type": "choice",
            "choice": "approve",
            "confidence": 0.9,
            "probabilities": {"approve": 0.9, "deny": 0.05, "escalate": 0.05},
        },
    }


class FakeCognis:
    """Mimic the Cognis inference contract for the Intaris companion identity."""

    def __init__(self, protocol: str = "chat") -> None:
        self.protocol = protocol
        self.metadata: object = "operations"
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.models_gate: threading.Event | None = None

    def _models(self) -> dict:
        data = []
        for route in ROUTES:
            entry = {"id": route, "object": "model", "created": 0, "owned_by": "cognis"}
            operations = [self.protocol] if route == EVALUATION_ROUTE else ["chat"]
            if self.metadata == "operations":
                entry["cognis"] = {"operations": operations}
            elif self.metadata != "absent" and route == EVALUATION_ROUTE:
                entry["cognis"] = self.metadata
            data.append(entry)
        return {"object": "list", "data": data}

    @staticmethod
    def _mismatch() -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Model does not support this operation",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_model",
                }
            },
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(request)
        self.calls.append((path, request.headers.get("authorization", "")))
        if path == "/v1/models":
            if self.models_gate is not None:
                assert self.models_gate.wait(timeout=5)
            return httpx.Response(200, json=self._models())
        if path == "/v1/chat/completions":
            if self.protocol != "chat":
                return self._mismatch()
            content = json.dumps(
                {
                    "aligned": True,
                    "risk": "low",
                    "reasoning": "Chat evaluator approved.",
                    "decision": "approve",
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": EVALUATION_ROUTE,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        if path == "/v1/systemone":
            if self.protocol != "systemone":
                return self._mismatch()
            return httpx.Response(
                200,
                json={
                    "model": "typesafe/jev-1.13",
                    "answers": _jev_answers(),
                    "usage": {"input_tokens": 275, "output_tokens": 20, "cost": 3e-05},
                    "provider": "TypeSafe",
                    "id": "provider-decision",
                },
            )
        return httpx.Response(404)

    def count(self, path: str) -> int:
        return sum(1 for called, _ in self.calls if called == path)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def managed(monkeypatch, tmp_path):
    token = tmp_path / "token.jwt"
    token.write_text("token-1")
    monkeypatch.setenv("COGNIS_INFERENCE_URL", "https://cognis.test/v1")
    monkeypatch.setenv("COGNIS_INFERENCE_TOKEN_FILE", str(token))
    monkeypatch.delenv("EVALUATOR_BACKEND", raising=False)
    return token


def _stack(cognis: FakeCognis, mode: str = "auto", clock: Clock | None = None):
    """Wire selector, Jev client and chat client the way server.py does."""
    transport = httpx.MockTransport(cognis.handler)
    backend: EvaluatorBackend | None = None

    def jev_factory() -> JevClient:
        return JevClient(
            JevConfig(enabled=True),
            transport=transport,
            on_protocol_mismatch=backend.protocol_mismatch,
        )

    backend = EvaluatorBackend(
        mode,
        jev_factory=jev_factory,
        models_url="https://cognis.test/v1/models" if mode == "auto" else None,
        token=token_supplier() if mode == "auto" else None,
        transport=transport,
        clock=clock or Clock(),
    )
    llm = LLMClient(LLMConfig(), on_protocol_mismatch=backend.protocol_mismatch)
    llm._client = OpenAI(
        api_key=token_supplier(),
        base_url="https://cognis.test/v1",
        http_client=httpx.Client(transport=transport),
        max_retries=0,
    )
    audit = MagicMock()
    audit.get_recent.return_value = []
    audit.get_user_decisions.return_value = []
    evaluator = Evaluator(
        llm=llm,
        evaluator_backend=backend,
        session_store=MagicMock(),
        audit_store=audit,
    )
    return backend, evaluator


def _evaluate(evaluator: Evaluator):
    return evaluator._llm_evaluate(
        session={
            "session_id": "session-auto",
            "user_id": "user-auto",
            "intention": "Update project documentation",
            "policy": None,
        },
        tool="write",
        args_redacted={"file_path": "docs/usage.md"},
        agent_id="agent-auto",
    )


# ── Configuration ───────────────────────────────────────────────────


def test_auto_is_default_only_with_managed_inference(monkeypatch, managed):
    assert evaluator_backend_mode() == "auto"
    config = JevConfig()
    assert config.enabled and config.model == EVALUATION_ROUTE
    assert config.base_url == "https://cognis.test"
    monkeypatch.setenv("EVALUATOR_BACKEND", "LLM")
    assert evaluator_backend_mode() == "llm"
    monkeypatch.setenv("EVALUATOR_BACKEND", "")
    assert evaluator_backend_mode() == "auto"
    monkeypatch.delenv("COGNIS_INFERENCE_URL")
    monkeypatch.delenv("EVALUATOR_BACKEND")
    assert evaluator_backend_mode() == "llm"


def test_auto_requires_managed_inference(monkeypatch):
    monkeypatch.delenv("COGNIS_INFERENCE_URL", raising=False)
    monkeypatch.delenv("COGNIS_INFERENCE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("EVALUATOR_BACKEND", "auto")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    with pytest.raises(ValueError, match="EVALUATOR_BACKEND=auto requires"):
        load_config()


def test_standalone_jev_keeps_typesafe_settings(monkeypatch):
    monkeypatch.delenv("COGNIS_INFERENCE_URL", raising=False)
    monkeypatch.delenv("COGNIS_INFERENCE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("EVALUATOR_BACKEND", "jev")
    monkeypatch.setenv("JEV_API_KEY", "standalone-key")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    cfg = load_config()
    assert cfg.jev.base_url == "https://api.typesafe.ai"
    assert cfg.jev.model == "jev-1.13.0"
    assert cfg.jev.api_key == "standalone-key"
    backend = EvaluatorBackend("jev", jev_factory=lambda: JevClient(cfg.jev))
    assert isinstance(backend.select(), JevClient)
    backend.close()


def test_judge_intention_and_analysis_stay_on_chat_routes(managed, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "unused")
    cfg = load_config()
    assert cfg.llm.model == EVALUATION_ROUTE
    assert cfg.llm_judge.model == "routes/intaris_judge"
    assert cfg.llm_analysis.model == "routes/intaris_analysis"

    # Hybrid Judge questions are answered by the chat client even when Jev
    # evaluates the call.
    from intaris import policy_assessment

    llm, jev = MagicMock(), MagicMock()
    llm.generate.side_effect = RuntimeError("chat client used")
    question = {
        "judge": True,
        "schema": {"properties": {}},
        "prompt": "",
        "fields": [],
    }
    with pytest.raises(RuntimeError, match="chat client used"):
        policy_assessment.classify(llm, jev, question)
    jev.classify_hybrid.assert_not_called()


# ── Discovery, caching and coalescing ───────────────────────────────


def test_discovery_selects_backend_and_caches(managed):
    clock = Clock()
    cognis = FakeCognis("chat")
    backend, _ = _stack(cognis, clock=clock)
    for _ in range(20):
        assert backend.select() is None
    assert cognis.count("/v1/models") == 1
    cognis.protocol = "systemone"
    clock.now += CACHE_SECONDS - 1
    assert backend.select() is None
    clock.now += 2
    assert isinstance(backend.select(), JevClient)
    assert cognis.count("/v1/models") == 2
    assert backend.active() == {"mode": "auto", "active": "jev", "error": None}


def test_concurrent_lookups_are_coalesced(managed):
    cognis = FakeCognis("systemone")
    cognis.models_gate = threading.Event()
    backend, _ = _stack(cognis)
    results: list[object] = []

    def run() -> None:
        results.append(backend.select())

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    cognis.models_gate.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(results) == 8 and all(isinstance(r, JevClient) for r in results)
    assert len({id(r) for r in results}) == 1
    assert cognis.count("/v1/models") == 1
    assert backend.lookups == 1


def test_lookup_reads_rotated_token(managed):
    clock = Clock()
    cognis = FakeCognis("systemone")
    backend, evaluator = _stack(cognis, clock=clock)
    assert _evaluate(evaluator).decision == "approve"
    managed.write_text("token-2")
    clock.now += CACHE_SECONDS + 1
    assert _evaluate(evaluator).decision == "approve"
    assert [auth for path, auth in cognis.calls] == [
        "Bearer token-1",
        "Bearer token-1",
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-2",
        "Bearer token-2",
    ]


def test_unavailable_token_fails_closed(managed):
    managed.unlink()
    backend, evaluator = _stack(FakeCognis("chat"))
    with pytest.raises(EvaluatorBackendError, match="unavailable"):
        backend.select()
    with pytest.raises(EvaluatorBackendError):
        _evaluate(evaluator)
    assert backend.lookups == 1


# ── Switching (fail closed once, then follow the route) ──────────────


@pytest.mark.parametrize(
    ("before", "after", "stale_path"),
    [
        ("chat", "systemone", "/v1/chat/completions"),
        ("systemone", "chat", "/v1/systemone"),
    ],
)
def test_route_switch_fails_one_call_closed_then_follows(
    managed, before, after, stale_path
):
    cognis = FakeCognis(before)
    backend, evaluator = _stack(cognis)
    assert _evaluate(evaluator).decision == "approve"
    served = "/v1/chat/completions" if before == "chat" else "/v1/systemone"
    assert cognis.count(served) == 1

    cognis.protocol = after
    with pytest.raises(EvaluatorBackendError, match="failed closed"):
        _evaluate(evaluator)
    # No replay on the other protocol within the failed evaluation.
    new_path = "/v1/systemone" if after == "systemone" else "/v1/chat/completions"
    assert cognis.count(new_path) == 0
    assert cognis.count(stale_path) == 2

    assert _evaluate(evaluator).decision == "approve"
    assert cognis.count(new_path) == 1
    assert cognis.count("/v1/models") == 2


# ── Metadata handling ───────────────────────────────────────────────


def test_absent_metadata_falls_back_to_llm_and_logs_once(managed, caplog):
    clock = Clock()
    cognis = FakeCognis("chat")
    cognis.metadata = "absent"
    backend, evaluator = _stack(cognis, clock=clock)
    caplog.set_level(logging.WARNING, logger="intaris.evaluator_backend")
    assert backend.select() is None
    clock.now += CACHE_SECONDS + 1
    assert backend.select() is None
    assert _evaluate(evaluator).decision == "approve"
    assert backend.lookups == 2
    assert sum("older Cognis" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "systemone",
        {"operations": "systemone"},
        {"operations": [1]},
        {"ops": ["chat"]},
        {"operations": []},
        {"operations": ["embeddings"]},
        {"operations": ["chat", "systemone"]},
        {"operations": ["chat", "responses"]},
    ],
)
def test_malformed_or_unknown_metadata_fails_closed(managed, metadata):
    cognis = FakeCognis("chat")
    cognis.metadata = metadata
    backend, evaluator = _stack(cognis)
    with pytest.raises(EvaluatorBackendError):
        backend.select()
    with pytest.raises(EvaluatorBackendError):
        _evaluate(evaluator)
    assert cognis.count("/v1/chat/completions") == 0
    assert cognis.count("/v1/models") == 1
    assert backend.active()["active"] is None


@pytest.mark.parametrize("payload", [[], {"data": {}}, {"object": "list"}])
def test_malformed_model_list_fails_closed(managed, payload):
    backend = EvaluatorBackend(
        "auto",
        jev_factory=MagicMock(),
        models_url="https://cognis.test/v1/models",
        token=token_supplier(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    with pytest.raises(EvaluatorBackendError, match="malformed"):
        backend.select()


def test_missing_route_fails_closed(managed):
    cognis = FakeCognis("chat")
    backend, _ = _stack(cognis)
    original = cognis._models
    cognis._models = lambda: {
        "object": "list",
        "data": [e for e in original()["data"] if e["id"] != EVALUATION_ROUTE],
    }
    with pytest.raises(EvaluatorBackendError, match="not available"):
        backend.select()


# ── Explicit pins ───────────────────────────────────────────────────


@pytest.mark.parametrize(("mode", "route"), [("llm", "systemone"), ("jev", "chat")])
def test_explicit_pin_mismatch_fails_with_clear_error(managed, mode, route):
    clock = Clock()
    cognis = FakeCognis(route)
    backend, evaluator = _stack(cognis, mode=mode, clock=clock)
    assert backend.active() == {"mode": mode, "active": mode, "error": None}
    for _ in range(2):
        with pytest.raises(EvaluatorBackendError, match=f"EVALUATOR_BACKEND={mode}"):
            _evaluate(evaluator)
    assert cognis.count("/v1/models") == 0
    other = "/v1/chat/completions" if route == "systemone" else "/v1/systemone"
    assert cognis.count(other) == 2
    # The pin is kept; diagnostics report the recent rejection, then age out.
    status = backend.active()
    assert status["mode"] == mode and status["active"] == mode
    assert f"EVALUATOR_BACKEND={mode} is pinned" in status["error"]
    clock.now += CACHE_SECONDS + 1
    assert backend.active() == {"mode": mode, "active": mode, "error": None}


def test_explicit_pin_matching_route_works(managed):
    backend, evaluator = _stack(FakeCognis("systemone"), mode="jev")
    assert _evaluate(evaluator).decision == "approve"


# ── Hybrid paths: single-attempt chat and typed Jev questions ────────


def test_single_attempt_chat_mismatch_fails_closed_without_replay(managed):
    # The caller selected chat earlier; the route is now System One.
    cognis = FakeCognis("systemone")
    backend, evaluator = _stack(cognis)
    with pytest.raises(EvaluatorBackendError, match="failed closed"):
        evaluator._llm.generate(
            [{"role": "user", "content": "x"}],
            json_schema={"name": "s", "schema": {"type": "object"}},
            single_attempt=True,
        )
    assert cognis.count("/v1/chat/completions") == 1
    assert cognis.count("/v1/systemone") == 0
    assert isinstance(backend.select(), JevClient)
    assert cognis.count("/v1/models") == 1


def test_hybrid_jev_mismatch_is_not_a_validation_abstention(managed):
    """A stale Jev selection on the hybrid path raises (503), never abstains."""
    from intaris import policy_assessment

    cognis = FakeCognis("systemone")
    backend, _ = _stack(cognis)
    jev = backend.select()
    cognis.protocol = "chat"
    question = {
        "judge": False,
        "schema": {"properties": {"risk": {"enum": ["low", "high"]}}},
        "prompt": "evidence",
        "fields": ["risk"],
    }
    with pytest.raises(EvaluatorBackendError) as raised:
        policy_assessment.classify(MagicMock(), jev, question)
    # HybridRuntime maps ValueError to a Jev abstention; this must not be one.
    assert not isinstance(raised.value, ValueError)
    assert cognis.count("/v1/systemone") == 1
    assert backend.select() is None
    assert cognis.count("/v1/models") == 2


def test_failed_lookup_is_cached_briefly_then_recovers(managed):
    from intaris.evaluator_backend import FAILURE_CACHE_SECONDS

    clock = Clock()
    cognis = FakeCognis("systemone")
    backend, _ = _stack(cognis, clock=clock)
    original = cognis.handler
    outage = {"on": True}

    def handler(request):
        if outage["on"] and request.url.path == "/v1/models":
            cognis.calls.append(("/v1/models", ""))
            return httpx.Response(503)
        return original(request)

    backend._http = httpx.Client(transport=httpx.MockTransport(handler))
    for _ in range(3):
        with pytest.raises(EvaluatorBackendError, match="HTTP 503"):
            backend.select()
    assert cognis.count("/v1/models") == 1
    outage["on"] = False
    clock.now += FAILURE_CACHE_SECONDS - 1
    with pytest.raises(EvaluatorBackendError):
        backend.select()
    clock.now += 2
    assert isinstance(backend.select(), JevClient)
    assert cognis.count("/v1/models") == 2


# ── Alignment uses the per-call selection ────────────────────────────


def test_alignment_check_follows_selected_backend(managed):
    from intaris.alignment import check_intention_alignment

    cognis = FakeCognis("systemone")
    backend, _ = _stack(cognis)
    aligned, reasoning = check_intention_alignment(
        llm=None,
        evaluator_backend=backend,
        parent_intention="Maintain the docs",
        child_intention="Update docs/usage.md",
    )
    assert cognis.count("/v1/systemone") == 1
    assert aligned is True and "Jev" in reasoning

    cognis.metadata = {"operations": ["embeddings"]}
    failing = _stack(cognis)[0]
    from intaris.alignment import AlignmentUnavailable

    fallback = MagicMock()
    with pytest.raises(AlignmentUnavailable, match="EvaluatorBackendError"):
        check_intention_alignment(
            llm=fallback,
            evaluator_backend=failing,
            parent_intention="Maintain the docs",
            child_intention="Update docs/usage.md",
        )
    fallback.generate.assert_not_called()


# ── /api/v1/config reports the active backend ────────────────────────


def test_config_endpoint_reports_active_backend(managed, monkeypatch):
    import intaris.server as srv
    from intaris.api.deps import SessionContext
    from intaris.api.info import config as config_endpoint

    monkeypatch.setenv("LLM_API_KEY", "unused")
    cognis = FakeCognis("systemone")
    backend, _ = _stack(cognis)
    monkeypatch.setattr(srv, "_config", load_config())
    monkeypatch.setattr(srv, "_evaluator_backend", backend)
    ctx = SessionContext(user_id="user-cfg", agent_id=None, user_bound=False)
    data = asyncio.run(config_endpoint(MagicMock(), ctx))
    assert data["evaluator"]["backend"] == "jev"
    assert data["evaluator"]["mode"] == "auto"
    assert data["evaluator"]["model"] == EVALUATION_ROUTE
    assert data["evaluator"]["backend_error"] is None
    serialized = json.dumps(data)
    assert "token-1" not in serialized and "cognis.test" not in serialized


# ── Jev wire contract against the Cognis System One DTO ──────────────

# Mirrors the provider's public request fields (extra="forbid").
_REQUEST_FIELDS = {"model", "state", "questions"}
_QUESTION_FIELDS = {
    "choice": (
        {"type", "instructions", "criteria"},
        {"type", "instructions", "criteria"},
    ),
    "noul": ({"type", "instructions"}, {"type", "instructions", "criteria"}),
}


def test_jev_request_and_response_match_cognis_dto(managed):
    cognis = FakeCognis("systemone")
    backend, evaluator = _stack(cognis)
    decision = _evaluate(evaluator)
    request = next(r for r in cognis.requests if r.url.path == "/v1/systemone")
    body = json.loads(request.content)

    assert set(body) == _REQUEST_FIELDS
    assert body["model"] == EVALUATION_ROUTE
    assert 1 <= len(body["model"]) <= 512
    assert isinstance(body["state"], dict)
    assert set(body["state"]) == {"evaluation_rules", "tool_call"}
    assert 1 <= len(body["questions"]) <= 256
    assert set(body["questions"]) == {"aligned", "risk", "decision"}
    for name, question in body["questions"].items():
        required, allowed = _QUESTION_FIELDS[question["type"]]
        assert required <= set(question) <= allowed, name
        assert isinstance(question["instructions"], str)
        if question["type"] == "choice":
            assert isinstance(question["criteria"], dict)
            assert 1 <= len(question["criteria"]) <= 255
            assert all(isinstance(v, str) for v in question["criteria"].values())
        elif "criteria" in question:
            assert isinstance(question["criteria"], dict)
    assert body["questions"]["aligned"]["type"] == "noul"
    assert set(body["questions"]["risk"]["criteria"]) == {
        "low",
        "medium",
        "high",
        "critical",
    }
    assert set(body["questions"]["decision"]["criteria"]) == {
        "approve",
        "deny",
        "escalate",
    }
    assert request.headers["authorization"] == "Bearer token-1"

    # The Cognis response preserves the provider model, usage and extras.
    assert decision.decision == "approve"
    assert decision.risk == "low"
    assert "typesafe/jev-1.13" in decision.reasoning
    assert "275 input / 20 output tokens" in decision.reasoning
