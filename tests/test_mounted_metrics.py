"""Exercise the real application route graph across FastAPI versions."""

import pytest
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.testclient import TestClient

from intaris.config import Config
from intaris.metrics import RuntimeMetrics
from intaris.server import _route_template, create_app


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("authenticated", [False, True])
def test_real_app_metrics_distinguish_endpoint_templates(
    monkeypatch, wrapped, authenticated
):
    from intaris import server

    cfg = Config()
    cfg.server.api_key = "metrics-test-key"
    monkeypatch.setattr(server, "_get_config", lambda: cfg)
    app = create_app()
    if wrapped:
        mount = next(
            r for r in app.routes if isinstance(r, Mount) and r.path == "/api/v1"
        )
        mount.app = CORSMiddleware(mount.app)
    app.state.runtime_metrics = RuntimeMetrics()
    # No lifespan: no databases, workers, models or external services are started.
    client = TestClient(app)
    headers = (
        {"X-API-Key": "metrics-test-key", "X-User-Id": "metrics-user"}
        if authenticated
        else {}
    )
    cases = [
        ("POST", "/api/v1/evaluate", "/api/v1/evaluate"),
        ("POST", "/api/v1/intention", "/api/v1/intention"),
        (
            "POST",
            "/api/v1/session/private-a/events",
            "/api/v1/session/{session_id}/events",
        ),
        (
            "POST",
            "/api/v1/session/private-b/events",
            "/api/v1/session/{session_id}/events",
        ),
        ("PATCH", "/api/v1/session/private-a", "/api/v1/session/{session_id}"),
        ("PATCH", "/api/v1/session/private-b", "/api/v1/session/{session_id}"),
        ("POST", "/api/v1/not-a-route/private-a", "<unmatched>"),
        ("POST", "/not-a-route/private-b", "<unmatched>"),
    ]
    expected = {}
    try:
        for method, path, template in cases:
            # Missing required bodies exercise real routing/validation, without
            # executing mutation handlers. Auth failures stop before routing.
            response = client.request(method, path, headers=headers)
            assert response.status_code == (
                (404 if template == "<unmatched>" else 422) if authenticated else 401
            )
            key = f"{method} {template} 4xx"
            expected[key] = expected.get(key, 0) + 1
        response = client.get("/api/v1/whoami", headers=headers)
        assert response.status_code == (200 if authenticated else 401)
        expected[f"GET /api/v1/whoami {'2xx' if authenticated else '4xx'}"] = 1
    finally:
        client.close()
    observed = app.state.runtime_metrics.snapshot()["http"]
    assert {key: value["requests_total"] for key, value in observed.items()} == expected


def test_included_router_prefixes_are_effective_templates():
    from fastapi import APIRouter, FastAPI
    from starlette.applications import Starlette

    router = APIRouter()

    @router.get("/items/{item_id}")
    async def item(item_id: str):
        return item_id

    api = FastAPI()
    api.include_router(router, prefix="/nested")
    app = Starlette(routes=[Mount("/api", app=CORSMiddleware(api))])
    assert (
        _route_template(
            app,
            {
                "type": "http",
                "method": "GET",
                "root_path": "",
                "path": "/api/nested/items/private-id",
            },
        )
        == "/api/nested/items/{item_id}"
    )
