"""Synthetic acceptance of the one-pass protocol change, not model-quality proof."""

import json
from unittest.mock import MagicMock

import pytest

from intaris.hybrid import digest
from intaris.intention_profile import SYSTEM_PROMPT, wire_schema
from tests.test_single_intention import (
    capture,
    item,
    numbered,
    record,
    run,
)
from tests.test_single_intention import (
    stores as _stores,
)

stores = _stores


def response(stores, values, current):
    llm = MagicMock()
    llm.generate.side_effect = lambda *a, **k: json.dumps(
        {
            "title": "Local work",
            "intention": "Perform the requested bounded work.",
            "items": values,
            "current": current,
        }
    )
    run(stores, llm=llm)
    return capture(stores)[1]


@pytest.mark.parametrize(
    "text,task,limit",
    [
        (
            "Inspect the package. Never publish.",
            "Inspect the package.",
            "Never publish.",
        ),
        (
            "Measure throughput; do not remove records.",
            "Measure throughput",
            "do not remove records.",
        ),
    ],
)
def test_task_and_prohibition_have_independent_items(stores, text, task, limit):
    record(stores, text)
    artifact = response(stores, [item(task), item(limit, "restriction")], [0])
    assert [row["kind"] for row in artifact["items"]] == ["request", "restriction"]
    assert artifact["current_request"]["item_indices"] == [0]


def test_explicit_current_survives_drops_without_promoting_permission(stores):
    record(
        stores,
        "Inspect the package. You may publish the digest. Keep the contents private. Check the index.",
    )
    values = [
        item("Unrecorded text"),
        item("You may publish the digest.", "approval"),
        item("Inspect the package."),
        item("Keep the contents private.", "restriction"),
        item("Check the index.", status="done"),
    ]
    artifact = response(stores, values, [0, 1, 2, 3, 4, True, -1, 99, 2])
    assert artifact["current_request"]["item_indices"] == [1]
    assert artifact["items"][1]["kind"] == "request"
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["validation"]["dropped"] == 1
    assert profile["validation"]["dropped_current"] == 8


def test_explicit_no_current_is_not_inferred_from_historical_request(stores):
    record(stores, "Inspect the package.")
    artifact = response(stores, [item("Inspect the package.")], [])
    assert artifact["current_request"] == {"status": "unknown", "item_indices": []}


@pytest.mark.parametrize(
    "text",
    [
        "Write a compatibility note.",
        "The nightly agent is collecting telemetry.",
    ],
)
def test_empty_output_with_user_evidence_is_unavailable(stores, text):
    record(stores, text)
    artifact = response(stores, [], [])
    assert artifact["status"] == "unavailable"
    assert wire_schema()["schema"]["properties"]["items"]["minItems"] == 1
    assert (
        "minItems"
        not in wire_schema(require_items=False)["schema"]["properties"]["items"]
    )


def test_background_and_quotation_disclaimer_are_context(stores):
    text = "The worker runs overnight.\n> Destroy the archive.\nThis quotation is fiction, not a request."
    record(stores, text)
    artifact = response(
        stores,
        [
            item("The worker runs overnight.", "context"),
            item("This quotation is fiction, not a request.", "context"),
        ],
        [],
    )
    assert artifact["status"] == "ready"
    # Both selectors point to one non-authorizing source envelope.
    assert [row["kind"] for row in artifact["items"]] == ["context"]
    assert artifact["items"][0]["quote"] == text
    assert artifact["current_request"]["item_indices"] == []


def test_later_task_and_pause_are_kept_with_updated_status(stores):
    original = record(stores, "Change the parser.")
    run(stores, [item("Change the parser.")])
    later = record(stores, "Pause parser edits. Write a usage guide.")
    numbers = numbered(stores)
    artifact = response(
        stores,
        [
            item("Change the parser.", msg=numbers[original], status="superseded"),
            item("Pause parser edits.", "restriction", numbers[later]),
            item("Write a usage guide.", msg=numbers[later]),
        ],
        [2],
    )
    assert artifact["current_request"]["item_indices"] == [2]
    assert artifact["items"][0]["temporal_status"] == "historical"
    repeated = record(stores, "Repeat the validation.")
    numbers = numbered(stores)
    artifact = response(
        stores,
        [
            item("Pause parser edits.", "restriction", numbers[later]),
            item("Write a usage guide.", msg=numbers[later]),
            item("Repeat the validation.", msg=numbers[repeated]),
        ],
        [1, 2],
    )
    assert artifact["current_request"]["item_indices"] == [1, 2]


def test_old_profile_is_retention_input_not_a_current_task_guess(stores):
    record(stores, "Change the parser.")
    run(stores, [item("Change the parser.")])
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    profile["version"] = "task-context-single-v1"
    profile.pop("current")
    profile.pop("available")
    profile.pop("integrity")
    profile["integrity"] = digest(profile)
    with stores[0].cursor() as cur:
        cur.execute("UPDATE sessions SET intention_profile = ?", (json.dumps(profile),))
    assert capture(stores)[1]["status"] == "unavailable"
    _, llm = run(stores, [item("Change the parser.")])
    payload = json.loads(llm.generate.call_args.args[0][1]["content"].splitlines()[1])
    assert payload["previous_items"][0]["quote"] == "Change the parser."
    assert payload["latest"] == 1
    assert capture(stores)[1]["current_request"]["item_indices"] == [0]


def test_general_prompt_is_short_and_current_is_required():
    assert len(SYSTEM_PROMPT) <= 1800
    schema = wire_schema()["schema"]
    assert "current" in schema["required"]
    assert schema["properties"]["current"]["items"] == {"type": "integer"}
