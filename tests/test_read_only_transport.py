"""Existing REST/native request metadata carries the same per-call ceiling."""

import anyio
import pytest
from mcp import ClientSession

import intaris.server as srv
from intaris.audit import AuditStore
from intaris.session import SessionStore
from tests.test_hybrid_acceptance_api import proxy_for
from tests.test_hybrid_api import api as _api
from tests.test_hybrid_runtime import stores as _stores
from tests.test_read_only_overlay import answer

api, stores = _api, _stores


def test_rest_call_plan_then_build(api):
    client, _, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    assert (
        client.post(
            "/api/v1/intention",
            json={
                "session_id": "work",
                "intention": "Update local notes",
                "policy": {"allow_tools": ["up:write"], "maximum_outcome": "approve"},
            },
        ).status_code
        == 200
    )
    evaluator = srv._get_evaluator()
    evaluator._llm.generate.return_value = answer("mutating")
    proxy, upstream = proxy_for(
        SessionStore(srv._get_db()), AuditStore(srv._get_db()), evaluator
    )
    client.app.state._api_app.state.mcp_proxy = proxy
    body = {
        "session_id": "work",
        "server": "up",
        "tool": "write",
        "arguments": {"content": "new"},
    }
    blocked = client.post(
        "/api/v1/mcp/call",
        json={**body, "context": {"chat_mode": "plan", "read_only_required": True}},
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["decision"] == "deny"
    assert blocked.json()["evaluation_metadata"]["overlay"]["read_only_required"]
    assert upstream.call_tool.await_count == 0
    allowed = client.post(
        "/api/v1/mcp/call", json={**body, "context": {"chat_mode": "build"}}
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["decision"] == "approve"
    assert upstream.call_tool.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["legacy", "hybrid_v1"])
async def test_native_sdk_overlay_preserved(stores, version):
    from unittest.mock import AsyncMock

    from intaris.server import _session_agent_id, _session_user_id

    _, sessions, audit, llm, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Update notes",
        policy={
            "evaluation_policy_version": version,
            "allow_tools": ["up:write"],
            "maximum_outcome": "approve",
        },
    )
    llm.generate.return_value = answer("mutating")
    proxy, upstream = proxy_for(sessions, audit, evaluator)
    proxy._ensure_session = AsyncMock(return_value="work")
    user_token, agent_token = _session_user_id.set("owner"), _session_agent_id.set(None)
    to_server, server_input = anyio.create_memory_object_stream(10)
    from_server, client_input = anyio.create_memory_object_stream(10)
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(
                proxy._server.run,
                server_input,
                from_server,
                proxy._server.create_initialization_options(),
            )
            async with ClientSession(client_input, to_server) as client:
                await client.initialize()
                response = await client.call_tool(
                    "up:write",
                    {"content": "new"},
                    meta={
                        "intaris": {
                            "context": {
                                "chat_mode": "plan",
                                "read_only_required": True,
                                "chat_mode_source": "request",
                            }
                        }
                    },
                )
                assert response.isError
                assert response.meta["intaris"]["evaluation_metadata"]["overlay"][
                    "read_only_required"
                ]
                assert upstream.call_tool.await_count == 0
                allowed = await client.call_tool(
                    "up:write",
                    {"content": "new"},
                    meta={"intaris": {"context": {"chat_mode": "build"}}},
                )
                assert not allowed.isError
                assert upstream.call_tool.await_count == 1
            group.cancel_scope.cancel()
    finally:
        _session_user_id.reset(user_token)
        _session_agent_id.reset(agent_token)


def test_mcp_post_admission_error_logs_current_audit_id(api, monkeypatch, caplog):
    import intaris.server as srv
    from intaris.policy_activation import HybridError

    client, version, _ = api
    if version != "hybrid_v1":
        return
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    assert (
        client.post(
            "/api/v1/intention",
            json={
                "session_id": "proxy-error",
                "intention": "Read test data",
                "policy": {"maximum_outcome": "approve"},
            },
        ).status_code
        == 200
    )
    proxy, pool = proxy_for(
        SessionStore(srv._get_db()), AuditStore(srv._get_db()), srv._get_evaluator()
    )
    for route in client.app.routes:
        if getattr(route, "path", None) == "/api/v1":
            route.app.state.mcp_proxy = proxy
            break
    else:
        raise AssertionError("API mount not found")
    admitted = []

    def expired(call_id, *, user_id, state):
        admitted.append(call_id)
        raise HybridError("expired_admission", "Fresh admission required", 409)

    monkeypatch.setattr(proxy._audit, "mark_execution", expired)
    response = client.post(
        "/api/v1/mcp/call",
        json={
            "session_id": "proxy-error",
            "server": "up",
            "tool": "read",
            "arguments": {"file_path": "/repo/test.py"},
            "logical_call_id": "proxy-error-logical",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "expired_admission"
    assert len(admitted) == 1
    audit = AuditStore(srv._get_db()).get_by_call_id(admitted[0], user_id="owner")
    errors = [r.hybrid_error for r in caplog.records if hasattr(r, "hybrid_error")]
    assert errors == [
        {
            "code": "expired_admission",
            "status": 409,
            "session_id": "proxy-error",
            "tool": "up:read",
            "call_id": audit["call_id"],
            "logical_call_id": "proxy-error-logical",
        }
    ]
    assert audit["decision"] == "approve"
    pool.call_tool.assert_not_called()
