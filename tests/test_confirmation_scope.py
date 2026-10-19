"""Controller proof checks use structure/provenance, not English action keywords."""

import copy
import json

import pytest

from intaris import policy_assessment
from intaris.hybrid import digest
from intaris.intention_profile import wire_schema
from tests.test_single_intention import (
    capture,
    item,
    record,
    run,
)
from tests.test_single_intention import (
    stores as _stores,
)
from tests.test_single_intention_optimization import response
from tools.l1_projection_sizes import specimen

stores = _stores


def proof(form, *actions):
    return {"form": form, "actions": list(actions)}


@pytest.mark.parametrize(
    "action",
    [
        "compress the archive; remove the originals",
        "Compress the archive. Remove the originals",
        "compress the archive\nremove the originals",
    ],
)
def test_one_list_entry_cannot_hide_punctuated_multiple_actions(stores, action):
    record(stores, "May I " + action + "?", role="assistant")
    record(stores, "yes")
    run(
        stores,
        [item("yes", "approval", confirmation=proof("single_permission", action))],
    )
    assert capture(stores)[1]["items"][0]["kind"] == "uncertainty"


@pytest.mark.parametrize(
    "question",
    [
        "May I compress the archive; remove the originals?",
        "May I compress the archive? Remove the originals?",
        "May I compress the archive\nremove the originals?",
    ],
)
def test_final_action_selector_cannot_hide_an_earlier_action(stores, question):
    record(stores, question, role="assistant")
    record(stores, "yes")
    run(
        stores,
        [
            item(
                "yes",
                "approval",
                confirmation=proof("single_permission", "remove the originals"),
            )
        ],
    )
    artifact = capture(stores)[1]
    assert artifact["items"][0]["kind"] == "uncertainty"
    assert "approves" not in artifact["items"][0]
    assert artifact["current_request"]["item_indices"] == []
    _, state, result = specimen("inbox-inspection-only")
    rendered = policy_assessment.request(
        state, result, "", task_context=artifact, projection="lines"
    )["prompt"]
    assert "Approvals:" not in rendered
    assert "kind: uncertainty" in rendered


def test_verbatim_single_action_proof_is_not_english_specific(stores):
    action = "odeslat balíček do kanceláře"
    record(stores, "Mohu " + action + "?", role="assistant")
    record(stores, "ano")
    run(
        stores,
        [item("ano", "approval", confirmation=proof("single_permission", action))],
    )
    assert capture(stores)[1]["items"][0]["approves"] == action


def test_old_unproved_approval_is_not_rendered_or_retained_as_approval(stores):
    action = "upload the archive"
    record(stores, "May I " + action + "?", role="assistant")
    record(stores, "yes")
    run(
        stores,
        [item("yes", "approval", confirmation=proof("single_permission", action))],
    )
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    profile["version"] = "task-context-single-v2"
    profile["items"][0].pop("confirmation")
    profile["items"][0].pop("approves")
    profile.pop("integrity")
    profile["integrity"] = digest(profile)
    with stores[0].cursor() as cur:
        cur.execute("UPDATE sessions SET intention_profile = ?", (json.dumps(profile),))
    assert capture(stores)[1]["status"] == "unavailable"
    _, llm = run(
        stores,
        [item("yes", "approval", confirmation=proof("single_permission", action))],
    )
    payload = json.loads(llm.generate.call_args.args[0][1]["content"].splitlines()[1])
    assert payload["previous_items"][0]["kind"] == "uncertain"
    assert capture(stores)[1]["items"][0]["approves"] == action


@pytest.mark.parametrize(
    "form,question,actions",
    [
        (
            "alternatives",
            "Should I rebuild the index or upload the archive?",
            ["rebuild the index", "upload the archive"],
        ),
        (
            "alternatives",
            "Should I rebuild the index or upload the archive?",
            ["upload the archive"],
        ),
        (
            "multiple_actions",
            "May I compress the bundle and remove the originals?",
            ["compress the bundle", "remove the originals"],
        ),
        ("open_question", "What should happen to the archive?", ["the archive"]),
        ("unknown", "What next?", []),
        ("alternatives", "Mám obnovit index, nebo odeslat archiv?", ["odeslat archiv"]),
        (
            "single_permission",
            "May I compress the bundle and remove the originals?",
            ["compress the bundle", "remove the originals"],
        ),
        ("single_permission", "May I upload the archive?", []),
    ],
)
def test_non_single_questions_cannot_authorize(stores, form, question, actions):
    record(stores, question, role="assistant")
    record(stores, "yes")
    run(stores, [item("yes", "approval", confirmation=proof(form, *actions))])
    artifact = capture(stores)[1]
    value = artifact["items"][0]
    assert value["kind"] == "uncertainty"
    assert value["explicitness"] == "uncertain"
    assert value["reference_resolution"] == "ambiguous"
    assert "approves" not in value
    assert artifact["current_request"]["item_indices"] == []


@pytest.mark.parametrize(
    "confirmation",
    [
        None,
        {"form": "single_permission"},
        {"form": "single_permission", "actions": [""]},
        {"form": "single_permission", "actions": {"action": "send the summary"}},
        proof("single_permission", "send a different message"),
        proof("single_permission", " send the summary "),
        proof("single_permission", "send the"),
        proof("single_permission", "send the summary"),
    ],
)
def test_missing_or_partial_or_invented_action_downgrades(stores, confirmation):
    record(
        stores, "May I send the summary after the reviewer signs off?", role="assistant"
    )
    record(stores, "yes")
    run(stores, [item("yes", "approval", confirmation=confirmation)])
    assert capture(stores)[1]["items"][0]["kind"] == "uncertainty"


def test_one_verbatim_action_preserves_conditions_and_projects_action_scope(stores):
    action = "send the summary after the reviewer signs off"
    record(stores, "May I " + action + "?", role="assistant")
    record(stores, "yes")
    _, llm = run(
        stores,
        [item("yes", "approval", confirmation=proof("single_permission", action))],
    )
    artifact = capture(stores)[1]
    value = artifact["items"][0]
    assert value["kind"] == "approval"
    assert value["approves"] == action
    assert value["reference_resolution"] == "single_linked"
    llm.generate.assert_called_once()
    _, state, result = specimen("inbox-inspection-only")
    before = copy.deepcopy(artifact)
    q = policy_assessment.request(
        state, result, "", task_context=artifact, projection="lines"
    )
    assert f"; Approved action: {action}" not in q["prompt"]
    assert '): "yes"' not in q["prompt"]
    assert "; Scope: May I " not in q["prompt"]
    assert artifact == before
    assert capture(stores)[1]["items"][0]["approves"] == action


def test_bare_confirmation_mislabeled_request_never_becomes_current(stores):
    record(stores, "May I upload the archive?", role="assistant")
    record(stores, "yes")
    artifact = response(
        stores,
        [
            item(
                "yes",
                "request",
                confirmation=proof("single_permission", "upload the archive"),
            )
        ],
        [0],
    )
    assert artifact["items"][0]["kind"] == "uncertainty"
    assert artifact["current_request"]["item_indices"] == []


def test_allowance_is_approval_not_current_task(stores):
    record(stores, "You may disclose the build identifier; keep account names private.")
    artifact = response(
        stores,
        [
            item("You may disclose the build identifier", "approval"),
            item("keep account names private.", "restriction"),
        ],
        [0, 1],
    )
    assert artifact["current_request"]["item_indices"] == []
    assert [r["kind"] for r in artifact["items"]] == ["approval", "restriction"]
    profile = json.loads(stores[1].get("session", user_id="owner")["intention_profile"])
    assert profile["validation"]["dropped_current"] == 2
    schema = wire_schema()["schema"]
    compact = schema["$defs"]["CompactItem"]
    assert "confirmation" in compact["required"]
    assert (
        "Permission without asking for execution"
        in compact["properties"]["kind"]["description"]
    )
    confirmation = schema["$defs"]["Confirmation"]
    assert confirmation["additionalProperties"] is False
    assert set(confirmation["required"]) == {"form", "actions"}
    # Quotes are verbatim source spans, bounded only by the source capture.
    assert "maxLength" not in confirmation["properties"]["actions"]["items"]
