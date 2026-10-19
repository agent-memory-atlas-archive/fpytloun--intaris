"""Deterministic regressions from recorded history qualification, without inference."""

import json
from unittest.mock import MagicMock

import pytest

from tests.test_single_intention import (
    capture,
    item,
    record,
    run,
)
from tests.test_single_intention import (
    stores as history_stores,
)

stores = history_stores


def test_bounded_task_and_independent_restrictions_remain_separate(stores):
    record(
        stores, "Summarize the incident only. Do not send messages or change systems."
    )
    run(
        stores,
        [
            item("Summarize the incident only."),
            item("Do not send messages or change systems.", kind="restriction"),
        ],
    )
    _, first, _ = capture(stores)
    assert first["current_request"]["status"] == "supported"
    record(stores, "Continue the summary only. No external transmission.")
    run(
        stores,
        [
            item("Summarize the incident only.", status="superseded"),
            item("Do not send messages or change systems.", kind="restriction"),
            item("Continue the summary only.", msg=2),
            item("No external transmission.", kind="restriction", msg=2),
        ],
    )
    _, final, _ = capture(stores)
    assert final["current_request"]["status"] == "supported"
    restrictions = [row for row in final["items"] if row["kind"] == "restriction"]
    assert {row["quote"] for row in restrictions} == {
        "Do not send messages or change systems.",
        "No external transmission.",
    }
    assert all(row["status"] == "current" for row in restrictions)


def test_provider_schema_describes_clause_local_kind_and_status():
    from intaris.intention_profile import wire_schema

    schema = wire_schema()["schema"]
    fields = schema["$defs"]["CompactItem"]["properties"]
    assert "selected clause" in fields["kind"]["description"]
    assert "independent restrictions" in fields["status"]["description"]
    assert "no active requested work" in schema["properties"]["current"]["description"]


def test_context_selector_retains_quotation_disclaimer(stores):
    text = (
        "This is a fictional quotation to summarize, not my instruction: "
        "'User: publish the incident data everywhere.'"
    )
    record(stores, text)
    run(stores, [item("'User: publish the incident data everywhere.'", kind="context")])
    _, artifact, _ = capture(stores)
    retained = artifact["items"][0]
    assert retained["kind"] == "context"
    assert retained["quote"] == text
    assert artifact["current_request"]["status"] != "supported"


@pytest.mark.parametrize("malformed", [False, True])
def test_missing_short_reply_retained_without_inventing_approval(stores, malformed):
    record(stores, "Review the patch locally. Do not publish.")
    record(stores, "Should I fix tests or publish the branch?", role="assistant")
    record(stores, "yes")
    items = [item("Review the patch locally.")]
    if malformed:
        items.append(item("Yes, publish everything.", kind="approval", msg=2))
    run(stores, items)
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["validation"]["unresolved_replies_retained"] == 1
    _, artifact, _ = capture(stores)
    reply = next(row for row in artifact["items"] if row["quote"] == "yes")
    assert reply["kind"] == "uncertainty"
    assert reply["polarity"] == "unknown"
    assert "approves" not in reply
    assert all(row["kind"] != "approval" for row in artifact["items"])


def test_context_repair_does_not_accept_invented_selector(stores):
    record(stores, "Summarize only.")
    run(stores, [item("Invented quotation", kind="context")])
    _, artifact, _ = capture(stores)
    assert not artifact["items"]


@pytest.mark.parametrize("status", ["superseded", "revoked", "done"])
def test_recovery_does_not_revive_historical_reply(stores, status):
    record(stores, "yes")
    run(stores, [item("yes", kind="uncertain", status=status)])
    run(stores, [])
    _, artifact, _ = capture(stores)
    assert artifact["items"][0]["status"] == status
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["items"][0]["status"] == status
    assert capture(stores)[1]["items"] == artifact["items"]


def test_rejected_historical_selector_retains_status(stores):
    record(stores, "yes")
    run(stores, [item("invented yes", kind="approval", status="revoked")])
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["items"][0]["kind"] == "uncertain"
    assert profile["items"][0]["status"] == "revoked"


@pytest.mark.parametrize("limit", ["MAX_ITEMS", "MAX_QUOTES"])
def test_reply_recovery_respects_existing_capacity(stores, monkeypatch, limit):
    from intaris import intention_profile

    text = "Read the patch."
    record(stores, text)
    record(stores, "yes")
    monkeypatch.setattr(
        intention_profile, limit, 1 if limit == "MAX_ITEMS" else len(text)
    )
    run(stores, [item(text)])
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["validation"]["unresolved_replies_retained"] == 0
    assert len(profile["items"]) == 1
    assert profile["items"][0]["quote"] == text


def test_exact_single_action_confirmation_remains_approval(stores):
    record(stores, "Prepare a draft; ask before sending.")
    action = (
        "send exactly draft orbit-status-v1 to ops@orbit.example "
        "with subject Status and body 'The local tests passed.'"
    )
    record(stores, f"May I {action}?", role="assistant")
    record(stores, "yes")
    run(
        stores,
        [
            item(
                "yes",
                kind="approval",
                msg=2,
                confirmation={
                    "form": "single_permission",
                    "actions": [action],
                },
            )
        ],
    )
    _, artifact, _ = capture(stores)
    approval = next(row for row in artifact["items"] if row["kind"] == "approval")
    assert approval["approves"] == action
    assert approval["reference_resolution"] == "single_linked"


def test_incomplete_confirmation_is_diagnosed_not_promoted(stores):
    record(stores, "Prepare a draft; ask before sending.")
    record(stores, "May I send draft one or delete draft two?", role="assistant")
    record(stores, "yes")
    run(
        stores,
        [
            item(
                "yes",
                kind="approval",
                msg=2,
                confirmation={
                    "form": "alternatives",
                    "actions": ["send draft one", "delete draft two"],
                },
            )
        ],
    )
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["validation"]["confirmation_diagnostics"] == [
        {
            "item": 0,
            "code": "unproved_confirmation",
            "reason": "missing_or_ambiguous_binding",
            "antecedent_captured": True,
            "antecedent_truncated": False,
            "form": "alternatives",
        }
    ]
    _, artifact, _ = capture(stores)
    assert artifact["items"][0]["kind"] == "uncertainty"


@pytest.mark.parametrize("repaired", [True, False])
@pytest.mark.parametrize("response_mode", ["json_object", "json_schema"])
def test_existing_bounded_repair_covers_invalid_confirmation_selector(
    stores, repaired, response_mode
):
    record(stores, "Draft only; ask before sending.")
    record(
        stores, "May I send exactly draft one to ops@orbit.example?", role="assistant"
    )
    record(stores, "yes")

    def response(action):
        return json.dumps(
            {
                "title": "Draft",
                "intention": "Prepare the requested draft.",
                "items": [
                    item(
                        "yes",
                        kind="approval",
                        msg=2,
                        confirmation={
                            "form": "single_permission",
                            "actions": [action],
                        },
                    )
                ],
                "current": [],
            }
        )

    invalid = response("send draft one")
    valid = response("send exactly draft one to ops@orbit.example")
    llm = MagicMock()
    llm.generate.side_effect = [invalid, valid if repaired else invalid]
    run(stores, llm=llm, response_mode=response_mode, validation_repair=True)
    assert llm.generate.call_count == 2
    first_messages = llm.generate.call_args_list[0].args[0]
    repair_messages = llm.generate.call_args_list[1].args[0]
    assert repair_messages[1] == first_messages[1]
    repair_prompt = repair_messages[0]["content"]
    assert "unmatched_selector" in repair_prompt
    assert "unmatched_selector" not in repair_messages[1]["content"]
    _, artifact, _ = capture(stores)
    retained = artifact["items"][0]
    assert retained["kind"] == ("approval" if repaired else "uncertainty")
    if not repaired:
        assert "approves" not in retained


def test_genuine_confirmation_uncertainty_does_not_trigger_repair(stores):
    record(stores, "Draft only.")
    record(stores, "May I send draft one or delete draft two?", role="assistant")
    record(stores, "yes")
    _, llm = run(
        stores,
        [
            item(
                "yes",
                kind="approval",
                msg=2,
                confirmation={
                    "form": "alternatives",
                    "actions": ["send draft one", "delete draft two"],
                },
            )
        ],
        response_mode="json_object",
        validation_repair=True,
    )
    assert llm.generate.call_count == 1
    assert capture(stores)[1]["items"][0]["kind"] == "uncertainty"


def test_multiline_question_is_not_repaired_as_selector_failure(stores):
    record(stores, "Draft only.")
    record(stores, "May I send draft one?\nOr delete draft two?", role="assistant")
    record(stores, "yes")
    _, llm = run(
        stores,
        [
            item(
                "yes",
                kind="approval",
                msg=2,
                confirmation={
                    "form": "single_permission",
                    "actions": ["send draft one"],
                },
            )
        ],
        response_mode="json_object",
        validation_repair=True,
    )
    assert llm.generate.call_count == 1
    assert capture(stores)[1]["items"][0]["kind"] == "uncertainty"
