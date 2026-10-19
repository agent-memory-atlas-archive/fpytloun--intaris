"""Event rejection taxonomy and non-replay of ambiguous appends."""

# Fixtures imported from the API suite are intentionally named by pytest.
# ruff: noqa: F811

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from intaris.api import events as api
from intaris.api.deps import SessionContext
from intaris.api.schemas import SessionEvent
from intaris.events.errors import ERROR_HEADER, EVENT_ROUTE
from intaris.events.idempotency import EventAppendIdempotencyStore
from intaris.metrics import RuntimeMetrics, render_prometheus_metrics
from tests.test_api import (  # noqa: F401
    _create_session,
    _reset_server_globals,
    client_no_auth,
    client_with_auth,
    env_no_auth,
    env_with_auth,
    tmp_db,
)

URL = "/api/v1/session/ingestion-session/events"
HEADERS = {"X-User-Id": "ingestion-user"}
KEY = "ingestion-session:batch:1"


@pytest.mark.parametrize(
    "payload,status,code",
    [
        ([], 400, "empty_batch"),
        ([{"type": "message", "data": {}}] * 1001, 400, "batch_too_large"),
        ({"type": "unrecognized", "data": {}}, 400, "invalid_event_type"),
        ({"data": {}}, 422, "validation_failed"),
    ],
)
def test_rejection_codes_keep_detail(client_no_auth, payload, status, code):
    response = client_no_auth.post(URL, headers=HEADERS, json=payload)
    assert response.status_code == status
    assert response.headers[ERROR_HEADER] == code
    assert set(response.json()) == {"detail"}


def test_disabled_and_missing_are_distinct(client_no_auth, monkeypatch):
    response = client_no_auth.post(
        URL, headers=HEADERS, json={"type": "message", "data": {}}
    )
    assert response.status_code == 404
    assert response.headers[ERROR_HEADER] == "session_not_found"
    monkeypatch.setattr(client_no_auth.app.state._api_app.state, "event_store", None)
    response = client_no_auth.post(
        URL, headers=HEADERS, json={"type": "message", "data": {}}
    )
    assert response.status_code == 404
    assert response.headers[ERROR_HEADER] == "recording_disabled"


def test_auth_failure_observed(client_with_auth):
    response = client_with_auth.post(URL, json={"type": "message", "data": {}})
    assert response.status_code == 401
    assert response.headers[ERROR_HEADER] == "unauthenticated"
    assert client_with_auth.app.state.runtime_metrics.snapshot()["event_http"] == [
        {"method": "POST", "status": "401", "category": "unauthenticated", "count": 1}
    ]


def test_duplicate_and_ordering(client_no_auth):
    _create_session(client_no_auth, "ingestion-session", HEADERS)
    payload = [{"type": "message", "data": {"index": i}} for i in range(3)]
    first = client_no_auth.post(
        URL, params={"idempotency_key": KEY}, headers=HEADERS, json=payload
    )
    replay = client_no_auth.post(
        URL, params={"idempotency_key": KEY}, headers=HEADERS, json=payload
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    result = client_no_auth.get(
        URL, params={"type": "message"}, headers=HEADERS
    ).json()["events"]
    assert [event["data"]["index"] for event in result] == [0, 1, 2]
    assert [event["seq"] for event in result] == list(
        range(first.json()["first_seq"], first.json()["last_seq"] + 1)
    )


def _ledger():
    from intaris.server import _get_db

    return EventAppendIdempotencyStore(_get_db())


def _age_claim():
    from intaris.server import _get_db

    with _get_db().cursor() as cur:
        cur.execute(
            "UPDATE event_append_idempotency SET created_at = ? WHERE idempotency_key = ?",
            ("2000-01-01T00:00:00+00:00", KEY),
        )


def test_pending_survives_restart_and_retention(client_no_auth, monkeypatch):
    _create_session(client_no_auth, "ingestion-session", HEADERS)
    assert _ledger().claim("ingestion-user", "ingestion-session", KEY)[0]
    append = Mock()
    monkeypatch.setattr(client_no_auth.app.state.event_store, "append", append)
    # A freshly constructed ledger models a new process reading the durable claim.
    response = client_no_auth.post(
        URL,
        params={"idempotency_key": KEY},
        headers=HEADERS,
        json={"type": "message", "data": {}},
    )
    assert response.status_code == 409
    assert response.headers[ERROR_HEADER] == "append_in_progress"
    _age_claim()
    assert _ledger().delete_expired(max_age=timedelta(hours=24)) == 0
    for _ in range(3):
        response = client_no_auth.post(
            URL,
            params={"idempotency_key": KEY},
            headers=HEADERS,
            json={"type": "message", "data": {}},
        )
        assert response.status_code == 409
        assert response.headers[ERROR_HEADER] == "append_uncertain"
    append.assert_not_called()
    assert (
        _ledger().get("ingestion-user", "ingestion-session", KEY)["status"] == "pending"
    )


@pytest.mark.asyncio
async def test_cancelled_async_request_keeps_sync_append_claim(
    client_no_auth, monkeypatch
):
    _create_session(client_no_auth, "ingestion-session", HEADERS)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = client_no_auth.app.state.event_store.append

    def append(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(client_no_auth.app.state.event_store, "append", append)
    request = SimpleNamespace(app=client_no_auth.app, headers={})
    task = asyncio.create_task(
        api.append_events(
            "ingestion-session",
            [SessionEvent(type="message", data={"index": 0})],
            request,
            SessionContext("ingestion-user"),
            KEY,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    assert (
        _ledger().get("ingestion-user", "ingestion-session", KEY)["status"] == "pending"
    )
    _age_claim()
    with pytest.raises(HTTPException) as error:
        await api.append_events(
            "ingestion-session",
            [SessionEvent(type="message", data={"index": 0})],
            request,
            SessionContext("ingestion-user"),
            KEY,
        )
    assert error.value.headers[ERROR_HEADER] == "append_uncertain"
    result = client_no_auth.get(
        URL, params={"type": "message"}, headers=HEADERS
    ).json()["events"]
    assert len(result) == 1


def test_partial_failure_retains_claim(client_no_auth, monkeypatch):
    _create_session(client_no_auth, "ingestion-session", HEADERS)
    original = client_no_auth.app.state.event_store.append

    def append(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic post-append failure")

    monkeypatch.setattr(client_no_auth.app.state.event_store, "append", append)
    response = client_no_auth.post(
        URL,
        params={"idempotency_key": KEY},
        headers=HEADERS,
        json={"type": "message", "data": {}},
    )
    assert response.status_code == 500
    assert response.headers[ERROR_HEADER] == "append_uncertain"
    assert (
        _ledger().get("ingestion-user", "ingestion-session", KEY)["status"] == "pending"
    )


def test_metric_labels_are_bounded():
    metrics = RuntimeMetrics()
    for i in range(100):
        metrics.observe_request("POST", EVENT_ROUTE, 404, 1, f"private-{i}")
    snapshot = metrics.snapshot()
    assert snapshot["event_http"] == [
        {
            "method": "POST",
            "status": "404",
            "category": "unknown_rejection",
            "count": 100,
        }
    ]
    text = render_prometheus_metrics(snapshot, {}, None)
    assert "private-" not in text
    assert 'status_group="4xx"' in text
    assert 'status="404"' in text


def test_unsupported_method_is_observed(client_no_auth):
    response = client_no_auth.put(URL, headers=HEADERS, json={})
    assert response.status_code == 405
    assert response.headers[ERROR_HEADER] == "unknown_rejection"
    snapshot = client_no_auth.app.state.runtime_metrics.snapshot()
    assert snapshot["event_http"] == [
        {
            "method": "OTHER",
            "status": "405",
            "category": "unknown_rejection",
            "count": 1,
        }
    ]
    assert "PUT <unmatched> 4xx" in snapshot["http"]
