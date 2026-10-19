"""Persisted profiles remain readable, but changed extraction must regenerate."""

import json

from intaris.hybrid import digest
from intaris.intention_profile import VERSION, adapt, read_profile, validate_items


def test_previous_profile_is_readable_but_not_reused_after_extraction_change():
    old = {
        "version": "task-context-single-v4",
        "intention": "Review the patch.",
        "items": [],
        "sources": [],
        "available": True,
    }
    old["integrity"] = digest(old)
    session = {
        "intention": old["intention"],
        "intention_profile": json.dumps(old),
    }
    assert read_profile(session) == old
    assert VERSION != old["version"]
    assert adapt({"profile": old}) == ([], "unavailable")


def test_current_profile_with_matching_sources_keeps_active_task():
    text = "Review the patch."
    source = {
        "ref": "message:review",
        "content": text,
        "origin": "client_user_claim",
        "truncated": False,
    }
    items, dropped = validate_items(
        [
            {
                "kind": "request",
                "msg": 1,
                "quote": text,
                "status": "current",
                "confirmation": None,
            }
        ],
        [source],
    )
    assert not dropped
    watermarks = {"decisions": "unchanged-decisions", "latest_user": "current-user"}
    profile = {
        "version": VERSION,
        "available": True,
        "items": items,
        "current": [0],
        "sources": [source],
        "watermarks": watermarks,
    }
    result, status = adapt(
        {
            "profile": profile,
            "profile_sources": [source],
            "originals": [source],
            "watermarks": watermarks,
        }
    )
    assert status == "ready"
    assert result[0]["kind"] == "request"
    assert result[0]["quote"] == text
