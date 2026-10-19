"""Authenticated API and real native transport negative controls."""

import json
import time
from unittest.mock import AsyncMock, Mock

import anyio
import jwt
import pytest
from mcp import ClientSession
from mcp.types import CallToolResult, TextContent

from intaris.audit import AuditStore
from intaris.mcp.proxy import MCPProxy, _compact_evaluator_args
from intaris.session import SessionStore
from tests.test_hybrid_api import api as _api
from tests.test_hybrid_runtime import stores as _stores

api = _api
stores = _stores


@pytest.mark.parametrize("fault", ["session", "capability", "expired"])
def test_controller_scope_rejected_by_http_owner(api, fault):
    client, _, key = api
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
                "intention": "Read tests",
                "policy": {"evaluation_policy_version": "hybrid_v1"},
            },
        ).status_code
        == 200
    )
    now = int(time.time())
    claims = {
        "iss": "cognis",
        "aud": "intaris",
        "sub": "owner",
        "agent_id": "hook",
        "typ": "controller",
        "session_id": "other" if fault == "session" else "work",
        "iat": now - 120,
        "nbf": now - 120,
        "exp": now - 1 if fault == "expired" else now + 60,
        "jti": "scope-negative-control",
        "capabilities": [] if fault == "capability" else ["intaris:controller_context"],
        "governing_execution_id": "run",
        "governing_snapshot_id": "snapshot",
    }
    client.headers["Authorization"] = "Bearer " + jwt.encode(
        claims, key, algorithm="ES256"
    )
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "work",
            "tool": "read",
            "args": {},
            "controller_context": {
                "governing_execution_id": "run",
                "governing_snapshot_id": "snapshot",
                "executor_id": "local",
            },
        },
    )
    assert response.status_code == (403 if fault == "session" else 401), response.text


@pytest.mark.parametrize("cap", [None, "approve"])
def test_model_cannot_inject_authenticated_executor_on_http_boundary(api, cap):
    import intaris.server as srv
    from intaris.hybrid import Assessment

    client, _, key = api
    now = int(time.time())
    claims = {
        "iss": "cognis",
        "aud": "intaris",
        "sub": "owner",
        "agent_id": "hook",
        "typ": "controller",
        "session_id": "work",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": "executor-negative-control",
        "capabilities": ["intaris:controller_context"],
        "governing_execution_id": "run",
        "governing_snapshot_id": "snapshot",
    }
    client.headers.update(
        {
            "Authorization": "Bearer " + jwt.encode(claims, key, algorithm="ES256"),
            "X-Agent-Id": "hook",
        }
    )
    assert (
        client.post(
            "/api/v1/intention",
            json={
                "session_id": "work",
                "intention": "Write tests",
                "policy": {
                    "evaluation_policy_version": "hybrid_v1",
                    **({"maximum_outcome": cap} if cap else {}),
                },
            },
        ).status_code
        == 200
    )
    llm = srv._get_evaluator()._llm
    llm.generate.return_value = json.dumps(
        {
            **Assessment(relationship="supports_intent", risk="low").model_dump(),
            "executor_id": "forged",
        }
    )
    response = client.post(
        "/api/v1/evaluate",
        json={
            "session_id": "work",
            "tool": "write",
            "args": {},
            "controller_context": {
                "governing_execution_id": "run",
                "governing_snapshot_id": "snapshot",
                "executor_id": "local",
            },
        },
    )
    if cap == "approve":
        assert response.status_code == 200, response.text
        assert (
            response.json()["evaluation_metadata"]["controller_context"]["executor_id"]
            == "local"
        )
        assert response.json()["evaluation_metadata"]["operator_bound"]
        llm.generate.assert_not_called()
    else:
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["execution_state"] == "not_dispatched"
        assert not AuditStore(srv._get_db()).get_recent(
            "work", user_id="owner", limit=10
        )


def arguments():
    return {
        "file_path": "/workspace/"
        + "segment/" * 400
        + "forbidden/"
        + "segment/" * 400
        + "file.py"
    }


def proxy_for(sessions, audit, evaluator):
    upstream = Mock()
    upstream.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="written")])
    )
    connection = Mock()
    connection.get_or_connect = AsyncMock(return_value=upstream)
    connection.evict = AsyncMock()
    servers = Mock()
    servers.get_server.return_value = {
        "name": "up",
        "enabled": True,
        "agent_pattern": "*",
        "tools_cache": [
            {
                "name": "write",
                "inputSchema": {"type": "object"},
                "description": "Write file",
            }
        ],
    }
    servers.list_servers.return_value = [servers.get_server.return_value]
    servers.get_all_tool_preferences.return_value = {}
    proxy = MCPProxy(
        connection_manager=connection,
        evaluator=evaluator,
        session_store=sessions,
        audit_store=audit,
        server_store=servers,
    )
    return proxy, upstream


def test_rest_http_compacted_target_cannot_hide_denial(api):
    import intaris.server as srv

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
                "intention": "Write tests",
                "policy": {
                    "evaluation_policy_version": "hybrid_v1",
                    "deny_paths": ["*/forbidden/*"],
                    "allow_tools": ["up:write"],
                },
            },
        ).status_code
        == 200
    )
    proxy, upstream = proxy_for(
        SessionStore(srv._get_db()), AuditStore(srv._get_db()), srv._get_evaluator()
    )
    client.app.state._api_app.state.mcp_proxy = proxy
    original = arguments()
    assert "forbidden/" not in _compact_evaluator_args(original)["file_path"]
    response = client.post(
        "/api/v1/mcp/call",
        json={
            "session_id": "work",
            "server": "up",
            "tool": "write",
            "arguments": original,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["decision"] == "deny"
    assert response.json()["evaluation_metadata"]["admission"] is None
    assert upstream.call_tool.await_count == 0


@pytest.mark.asyncio
async def test_native_compacted_target_cannot_hide_denial(stores):
    from intaris.server import _session_agent_id, _session_user_id

    _, sessions, audit, _, evaluator = stores
    sessions.create(
        user_id="owner",
        session_id="work",
        intention="Write tests",
        policy={"deny_paths": ["*/forbidden/*"], "allow_tools": ["up:write"]},
    )
    proxy, upstream = proxy_for(sessions, audit, evaluator)
    proxy._ensure_session = AsyncMock(return_value="work")
    original = arguments()
    assert "forbidden/" not in _compact_evaluator_args(original)["file_path"]
    client_send, server_receive = anyio.create_memory_object_stream(10)
    server_send, client_receive = anyio.create_memory_object_stream(10)
    user_token, agent_token = _session_user_id.set("owner"), _session_agent_id.set(None)
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(
                proxy._server.run,
                server_receive,
                server_send,
                proxy._server.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as client:
                await client.initialize()
                response = await client.call_tool("up:write", original)
                assert response.isError
                assert response.meta is not None, response.model_dump()
                metadata = response.model_dump(by_alias=True)["_meta"]["intaris"]
                assert metadata["decision"] == "deny"
                assert metadata["evaluation_metadata"]["admission"] is None
                assert upstream.call_tool.await_count == 0
            group.cancel_scope.cancel()
    finally:
        _session_user_id.reset(user_token)
        _session_agent_id.reset(agent_token)
