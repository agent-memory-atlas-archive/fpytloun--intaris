"""Read-only public projection of controller-persisted rich intention."""

import json

import pytest

from intaris.api.schemas import SessionResponse
from intaris.session import SessionStore
from tests.test_hybrid_api import api  # noqa: F401


def profile():
    return {
        "version": "task-context-single-v3",
        "available": True,
        "items": [
            {
                "kind": "request",
                "quote": "Inspect the repository.",
                "status": "current",
                "source_ref": "user:1",
            },
            {
                "kind": "approval",
                "quote": "yes",
                "status": "current",
                "source_ref": "user:2",
                "approves": "upload the snapshot after review",
            },
            {
                "kind": "restriction",
                "quote": "Never push to main.",
                "status": "historical",
                "source_ref": "user:1",
            },
        ],
        "current": [0],
        "sources": [
            {"ref": "user:1", "content": "Full private source, not part of this view."},
            {"ref": "user:2", "content": "yes"},
        ],
        "watermarks": {"latest_user": "internal"},
        "integrity": "internal binding",
        "validation": {
            "dropped": 2,
            "dropped_current": 1,
            "drop_reasons": [
                {"item": 3, "code": "partial_statement"},
                {"item": 4, "code": "non_user_source"},
            ],
        },
    }


def response(value):
    return SessionResponse(
        session_id="rich-session",
        user_id="owner",
        intention="Inspect the repository.",
        created_at="2026-10-08T05:00:00Z",
        updated_at="2026-10-08T05:00:00Z",
        intention_profile=value,
    ).model_dump()


def test_profile_public_view_does_not_expose_raw_sources_or_mutate_storage():
    stored = json.dumps(profile(), ensure_ascii=False)
    view = response(stored)["intention_profile"]
    assert view["current"] == [0]
    assert [item["msg"] for item in view["items"]] == [1, 2, 1]
    assert view["items"][1]["approves"] == "upload the snapshot after review"
    assert view["validation"] == profile()["validation"]
    assert (
        "sources" not in view and "integrity" not in view and "watermarks" not in view
    )
    assert all("source_ref" not in item for item in view["items"])
    assert stored == json.dumps(profile(), ensure_ascii=False)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "malformed JSON",
        "[]",
        {"items": [None]},
        {"items": [], "sources": "bad"},
        {"items": [], "current": None},
    ],
)
def test_absent_or_malformed_profile_is_null(stored):
    assert response(stored)["intention_profile"] is None


def test_get_list_and_update_include_rich_profile_additively(api):  # noqa: F811
    from intaris.server import _get_db

    client, _, _ = api
    client.headers.update(
        {
            "Authorization": "Bearer hybrid-test-key",
            "X-User-Id": "owner",
            "X-Agent-Id": "hook",
        }
    )
    created = client.post(
        "/api/v1/intention",
        json={
            "session_id": "rich-session",
            "intention": "Inspect the repository.",
        },
    )
    assert created.status_code == 200, created.text
    store = SessionStore(_get_db())
    assert store.update_generated_intention(
        "rich-session",
        user_id="owner",
        intention="Inspect the repository.",
        intention_source="user",
        title="Repository inspection",
        intention_profile=profile(),
    )
    stored = store.get("rich-session", user_id="owner")["intention_profile"]
    assert isinstance(stored, str)
    expected = response(stored)["intention_profile"]
    read = client.get("/api/v1/session/rich-session").json()
    assert read["intention_profile"] == expected
    assert read["title"] == "Repository inspection"
    listed = client.get("/api/v1/sessions").json()
    assert listed["items"][0]["intention_profile"] == expected
    updated = client.patch(
        "/api/v1/session/rich-session",
        json={
            "details": {"working_directory": "/repo"},
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["intention_profile"] == expected
    assert store.get("rich-session", user_id="owner")["intention_profile"] == stored
    denied = client.get(
        "/api/v1/session/rich-session", headers={"X-User-Id": "other-owner"}
    )
    assert denied.status_code == 404
