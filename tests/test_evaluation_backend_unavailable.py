"""Routing metadata outages are temporary errors, never policy decisions."""

from unittest.mock import Mock

import httpx
import pytest

from intaris.evaluator_backend import (
    EvaluatorBackend,
    EvaluatorBackendError,
    EvaluatorBackendUnavailable,
)
from tests.test_hybrid_api import api as _api

api = _api


@pytest.mark.parametrize("failure", ["timeout", 429, 500, 502, 503, 504])
def test_transient_metadata_error_coalesces_then_recovers(failure):
    now = [0.0]
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(failure)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "routes/intaris_evaluation",
                        "cognis": {"operations": ["chat"]},
                    }
                ]
            },
        )

    backend = EvaluatorBackend(
        "auto",
        jev_factory=Mock,
        models_url="http://cognis.test/v1/models",
        token=lambda: "token",
        transport=httpx.MockTransport(respond),
        clock=lambda: now[0],
    )
    try:
        with pytest.raises(EvaluatorBackendUnavailable) as first:
            backend.select()
        assert first.value.retry_after_seconds == 5
        now[0] = 1.2
        with pytest.raises(EvaluatorBackendUnavailable) as cached:
            backend.select()
        assert cached.value.retry_after_seconds == 4
        assert cached.value is not first.value
        assert len(calls) == 1
        now[0] = 5.2
        assert backend.select() is None
        assert len(calls) == 2
    finally:
        backend.close()


def test_bad_credentials_are_not_labeled_transient():
    backend = EvaluatorBackend(
        "auto",
        jev_factory=Mock,
        models_url="http://cognis.test/v1/models",
        token=lambda: "token",
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    try:
        with pytest.raises(EvaluatorBackendError) as error:
            backend.select()
        assert not isinstance(error.value, EvaluatorBackendUnavailable)
    finally:
        backend.close()


def test_evaluate_returns_safe_503_and_retry_after(api, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    import intaris.server as srv

    client, _, _ = api
    client.headers.update(
        {"Authorization": "Bearer hybrid-test-key", "X-User-Id": "owner"}
    )
    evaluator = Mock()
    evaluator.evaluate_async = AsyncMock(
        side_effect=EvaluatorBackendUnavailable("private transport details", 4)
    )
    monkeypatch.setattr(srv, "_get_evaluator", lambda: evaluator)
    response = client.post(
        "/api/v1/evaluate",
        json={"session_id": "metadata-session", "tool": "read", "args": {}},
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "4"
    assert response.json()["detail"]["code"] == "evaluation_backend_unavailable"
    assert response.json()["detail"]["retryable"] is True
    assert "private transport details" not in response.text
    assert "decision" not in response.json()
