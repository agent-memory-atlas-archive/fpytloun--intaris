"""Test-owned loopback provider relay: counts wire attempts, never stores prompts.

Only two pinned upstream endpoints are reachable. No upstream URL or credential
is accepted from a request. The relay does not retry or change model output.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

_ROUTES = {
    "/l1/chat/completions": ("l1", "https://api.groq.com/openai/v1/chat/completions"),
    "/judge/chat/completions": (
        "judge",
        "https://api.groq.com/openai/v1/chat/completions",
    ),
    "/intention/chat/completions": (
        "intention",
        "https://api.groq.com/openai/v1/chat/completions",
    ),
    "/compiler/chat/completions": (
        "compiler",
        "https://api.groq.com/openai/v1/chat/completions",
    ),
    # Jev chooses the TypeSafe route for a loopback hostname. Only translate
    # the path, forwarding the unchanged payload to OpenRouter's decisions API.
    "/jev/v1/systemone": ("l1", "https://openrouter.ai/api/alpha/decisions"),
    "/jev/alpha/decisions": ("l1", "https://openrouter.ai/api/alpha/decisions"),
}
_USAGE_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "cost",
    "cached_tokens",
    "reasoning_tokens",
}


def safe_usage(value: Any) -> dict | None:
    """Whitelist numeric accounting only, including supported token details."""
    if not isinstance(value, dict):
        return None
    result = {}
    for key, item in value.items():
        if (
            key in _USAGE_KEYS
            and isinstance(item, (int, float))
            and not isinstance(item, bool)
        ):
            result[key] = item
        elif key in {"prompt_tokens_details", "completion_tokens_details"}:
            result[key] = safe_usage(item)
    return result or None


class ProviderRecorder:
    def __init__(
        self,
        groq_key: str,
        jev_key: str | None = None,
        *,
        max_requests: int = 400,
        max_tokens: int = 2_000_000,
        forbid_compiler: bool = False,
        budget=None,
        extraction_schema_name: str | None = None,
    ) -> None:
        if extraction_schema_name == "policy_compilation":
            raise ValueError("Extraction schema cannot mask the compiler guard")
        self._keys = {"groq": groq_key, "jev": jev_key}
        self.token = secrets.token_urlsafe(32)
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.forbid_compiler = forbid_compiler
        self.budget = budget
        self.extraction_schema_name = extraction_schema_name
        self.records: list[dict] = []
        self.validation_errors: list[dict] = []
        self.context: dict = {}
        self.stopped = False
        self._tokens = 0
        self._failures = 0
        self._active = 0
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=40, trust_env=False, follow_redirects=False)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                if not hmac.compare_digest(
                    self.headers.get("Authorization", ""), f"Bearer {owner.token}"
                ):
                    self.send_error(403)
                    return
                route = _ROUTES.get(self.path)
                if route is None:
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_000_000:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body)
                except ValueError:
                    self.send_error(400)
                    return
                stage, upstream = route
                provider = "jev" if "openrouter.ai" in upstream else "groq"
                schema_name = (
                    (payload.get("response_format") or {}).get("json_schema") or {}
                ).get("name")
                if schema_name == "policy_compilation":
                    stage = "compiler"
                if (
                    stage != "compiler"
                    and owner.extraction_schema_name
                    and schema_name == owner.extraction_schema_name
                ):
                    stage = "source_extraction"
                if stage == "compiler" and owner.forbid_compiler:
                    owner.validation_errors.append(
                        {
                            **owner.context,
                            "stage": "compiler",
                            "code": "compiler_forbidden_in_comparison_lane",
                        }
                    )
                    self.send_error(409, "Compiler forbidden in comparison lane")
                    return
                if provider == "jev" and set(payload.get("questions") or {}) == {
                    "intention"
                }:
                    stage = "intention_gate"
                expected_model = (
                    "typesafe/jev-1.13-20260917"
                    if provider == "jev"
                    else "openai/gpt-oss-20b"
                    if stage == "l1"
                    else "openai/gpt-oss-120b"
                )
                if payload.get("model") != expected_model or payload.get("stream"):
                    owner.validation_errors.append(
                        {
                            **owner.context,
                            "stage": stage,
                            "code": "frozen_model_contract",
                            "expected_model": expected_model,
                        }
                    )
                    self.send_error(400, "Frozen model/nonstreaming contract violated")
                    return
                key = owner._keys[provider]
                with owner._lock:
                    if (
                        owner.stopped
                        or len(owner.records) >= owner.max_requests
                        or owner._tokens >= owner.max_tokens
                        or not key
                    ):
                        owner.stopped = True
                        self.send_error(503, "Benchmark budget/access stop")
                        return
                    budget_attempt = owner.budget.reserve() if owner.budget else None
                    if owner.budget and budget_attempt is None:
                        owner.stopped = True
                        self.send_error(503, "Cumulative benchmark budget stop")
                        return
                    record = {
                        **owner.context,
                        "stage": stage,
                        "provider": provider,
                        "model": expected_model,
                        "attempt": len(owner.records) + 1,
                        "questions": len(payload.get("questions") or {}),
                        "request_bytes": length,
                        "usage": None,
                    }
                    if budget_attempt is not None:
                        record["cumulative_attempt"] = budget_attempt
                    owner.records.append(record)
                    owner._active += 1
                started = time.monotonic()
                try:
                    response = owner._client.post(
                        upstream,
                        content=body,
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    )
                    content = response.content
                    record["status"] = response.status_code
                    try:
                        data = response.json()
                        record["usage"] = safe_usage(data.get("usage"))
                        if stage == "intention_gate":
                            answer = (data.get("answers") or {}).get("intention") or {}
                            choices = {"keep", "refresh", "uncertain"}
                            record["gate_answer"] = {
                                "choice": answer.get("choice")
                                if answer.get("choice") in choices
                                else None,
                                "confidence": answer.get("confidence")
                                if isinstance(answer.get("confidence"), (int, float))
                                else None,
                                "probabilities": {
                                    key: value
                                    for key, value in (
                                        answer.get("probabilities") or {}
                                    ).items()
                                    if key in choices
                                    and isinstance(value, (int, float))
                                },
                            }
                    except (ValueError, AttributeError):
                        pass
                    with owner._lock:
                        usage = record["usage"] or {}
                        owner._tokens += usage.get(
                            "total_tokens",
                            (
                                usage.get("input_tokens", 0)
                                + usage.get("output_tokens", 0)
                            ),
                        )
                        owner._failures = (
                            owner._failures + 1 if response.status_code >= 400 else 0
                        )
                        if owner._failures >= 3:
                            owner.stopped = True
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    if response.headers.get("retry-after"):
                        self.send_header("Retry-After", response.headers["retry-after"])
                    self.end_headers()
                    try:
                        self.wfile.write(content)
                    except OSError:
                        # Runtime timeout must not discard late usage/accounting.
                        pass
                except httpx.HTTPError as exc:
                    record["status"] = 502
                    record["error"] = type(exc).__name__
                    with owner._lock:
                        owner._failures += 1
                        if owner._failures >= 3:
                            owner.stopped = True
                    self.send_error(502, "Provider transport failure")
                finally:
                    record["latency_ms"] = (time.monotonic() - started) * 1000
                    with owner._lock:
                        if owner.budget:
                            owner.budget.finish(budget_attempt, record)
                            if owner.budget.snapshot()["stop_reason"]:
                                owner.stopped = True
                        owner._active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> ProviderRecorder:
        self.thread.start()
        return self

    def drain(self, timeout_s: float = 45) -> None:
        """Wait for late provider accounting before snapshotting an attempt."""
        deadline = time.monotonic() + timeout_s
        while True:
            with self._lock:
                if not self._active:
                    return
            if time.monotonic() >= deadline:
                raise RuntimeError("Provider accounting drain timed out")
            time.sleep(0.01)

    def __exit__(self, *_args: Any) -> None:
        self.drain()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self._client.close()
