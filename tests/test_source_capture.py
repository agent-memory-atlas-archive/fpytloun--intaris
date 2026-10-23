"""Long user assignments use one bounded view at generation and validation."""

import asyncio
import json

import pytest

from intaris.intention_profile import prompt_parts, snapshot, validate_items
from intaris.source_capture import MAX_CAPTURE_CHARS, MAX_SOURCE_CHARS, bound_sources
from tests.test_rich_context_recovery import barrier, completion, failure, finish
from tests.test_single_intention import capture, item, record, run
from tests.test_single_intention import stores as stores

HEAD = "Read README only."
TAIL = "Inspect the final assignment section."


def assignment(length):
    return HEAD + "\n" + "x" * (length - len(HEAD) - len(TAIL) - 3) + ".\n" + TAIL


@pytest.mark.parametrize("length", [2500, 15000])
def test_long_assignment_tail_generates_profile(stores, length):
    text = assignment(length)
    assert len(text) == length
    record(stores, text)
    _, llm = run(stores, [item(TAIL)])
    llm.generate.assert_called_once()
    assert TAIL in llm.generate.call_args.args[0][1]["content"]
    context, artifact, extraction = capture(stores)
    assert artifact["status"] == "ready"
    assert artifact["items"][0]["quote"] == TAIL
    assert context.capture(stores[1].get("session", user_id="owner"))["cache"] == "hit"
    extraction.generate.assert_not_called()


def test_over_cap_tail_rejected_but_complete_head_retained(stores):
    record(stores, assignment(20000))
    captured = snapshot(stores[0], stores[1].get("session", user_id="owner"))
    source = captured["profile_sources"][0]
    assert source["truncated"] and len(source["content"]) == MAX_SOURCE_CHARS
    dropped = []
    accepted, count = validate_items(
        [item(TAIL), item(HEAD)], captured["profile_sources"], drop_reasons=dropped
    )
    assert count == 1
    assert dropped == [{"item": 0, "code": "truncated_authority"}]
    assert [row["quote"] for row in accepted] == [HEAD]
    # Cutting a quote at the exact capture boundary is never accepted.
    assert (
        validate_items(
            [item("Read README only.")],
            [
                {
                    **source,
                    "content": HEAD,
                    "captured_length": len(HEAD),
                }
            ],
        )[1]
        == 1
    )
    run(stores, [item(HEAD)])
    assert capture(stores)[1]["items"][0]["quote"] == HEAD


def test_recovery_generates_long_profile_without_eval_inference(stores):
    from unittest.mock import MagicMock

    record(stores, assignment(15000))
    llm = MagicMock()
    llm.generate.side_effect = [failure(502), completion(TAIL)]
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session")
        await finish(owner)

    asyncio.run(execute())
    assert llm.generate.call_count == 2
    _, artifact, extraction = capture(stores)
    assert artifact["status"] == "ready"
    assert artifact["items"][0]["quote"] == TAIL
    extraction.generate.assert_not_called()


def test_full_long_source_is_citable_but_truncated_prefix_is_not(stores):
    from intaris.hybrid import evaluate_policy
    from intaris.judge_simulator import request
    from intaris.policy_evidence import from_observations

    ref = record(stores, assignment(2500))
    run(stores, [item(TAIL)])
    artifact = capture(stores)[1]
    state = from_observations({"effects": ["read"], "mutation": "read_only"})
    args = ({"tool": "read"}, {"intention": "Read"}, state, evaluate_policy([], state))
    question = request(*args, artifact)
    assert ref in question["citable"].values()
    assert any(
        assignment(2500) in row
        for row in json.loads(question["messages"][1]["content"])["originals"]
    )
    source = next(r for r in artifact["originals"] if r["ref"] == ref)
    source["truncated"] = True
    assert ref not in request(*args, artifact)["citable"].values()


def test_total_budget_and_prompt_validation_views_are_identical(stores):
    for revision in range(5):
        record(stores, f"Revision {revision}: " + assignment(15000))
    session = stores[1].get("session", user_id="owner")
    captured = snapshot(stores[0], session)
    assert sum(len(r["content"]) for r in captured["originals"]) <= MAX_CAPTURE_CHARS
    assert not captured["profile_sources"][-1]["truncated"]
    assert captured["profile_sources"][-1]["content"] == "Revision 4: " + assignment(
        15000
    )
    assert any(r["truncated"] for r in captured["profile_sources"][:-1])
    prompt = prompt_parts(captured, session, [])
    # Boundary markers are generated; locate the one canonical JSON payload.
    wire = json.JSONDecoder().raw_decode(prompt[prompt.index("{") :])[0]
    assert [r["text"] for r in wire["sources"]] == [
        r["content"] for r in captured["profile_sources"]
    ]
    assert [bool(r.get("truncated")) for r in wire["sources"]] == [
        bool(r.get("truncated")) for r in captured["profile_sources"]
    ]


def test_newest_user_reserved_before_newer_nonuser_rows():
    rows = [
        {
            "ref": f"source-{n}",
            "content": "x" * MAX_SOURCE_CHARS,
            "origin": "client_user_claim" if n == 0 else "human_resolution",
            "chronology": str(n),
        }
        for n in range(8)
    ]
    bound_sources(rows)
    assert not rows[0]["truncated"]
    assert len(rows[0]["content"]) == MAX_SOURCE_CHARS
    assert sum(len(r["content"]) for r in rows) == MAX_CAPTURE_CHARS


def test_judge_preserves_complete_captured_multilingual_authority(stores):
    from intaris.hybrid import evaluate_policy
    from intaris.judge_simulator import request
    from intaris.policy_evidence import from_observations

    text = HEAD + "\n" + "界" * 14900 + "\n" + TAIL
    ref = record(stores, text)
    run(stores, [item(TAIL)])
    artifact = capture(stores)[1]
    assert not next(r for r in artifact["originals"] if r["ref"] == ref)["truncated"]
    state = from_observations({"effects": ["read"], "mutation": "read_only"})
    question = request(
        {"tool": "read"},
        {"intention": "Read"},
        state,
        evaluate_policy([], state),
        artifact,
    )
    assert question["model_view"]["source_coverage"] == "captured"
    raw_sources = json.loads(question["messages"][1]["content"])["raw_task_context"][
        "sources"
    ]
    assert (
        next(row for row in raw_sources if row["ref"] == ref)["render_truncated"]
        is False
    )
    assert ref in question["citable"].values()
    assert ref in [row["ref"] for row in question["citation_sources"]]
    payload = json.loads(question["messages"][1]["content"])
    number = next(
        n
        for n, source_ref in payload["reference_ids"]["sources"].items()
        if source_ref == ref
    )
    # Presented sources carry a chronology label between index and text.
    assert any(
        row.startswith(f"[{number}]") and row.endswith(f") {text}")
        for row in payload["originals"]
    )
    assert "intention_items" not in payload  # Do not duplicate extracted evidence.


def test_user_message_longer_than_judge_tail_clip_is_citable(stores):
    from unittest.mock import Mock

    from intaris import judge_simulator
    from intaris.hybrid import evaluate_policy
    from intaris.policy_evidence import from_observations

    text = assignment(judge_simulator.MESSAGE_CHAR_LIMIT + 1000)
    ref = record(stores, text)
    captured = snapshot(stores[0], stores[1].get("session", user_id="owner"))
    state = from_observations(
        {"tool": "edit", "effects": ["write"], "mutation": "mutating", "risk": "low"}
    )
    question = judge_simulator.request(
        {"tool": "edit", "args_redacted": {"path": "/repo/README.md"}},
        {"policy": {"profile": "strict"}},
        state,
        evaluate_policy([], state, "strict"),
        captured,
    )
    number = next(k for k, v in question["citable"].items() if v == ref)
    source = next(s for s in question["citation_sources"] if s["ref"] == ref)
    assert source["text"] == text
    answer, diagnostics = judge_simulator.classify(
        Mock(
            generate=Mock(
                return_value=json.dumps(
                    {
                        "user_would": "approve",
                        "risk": "low",
                        "confidence": "high",
                        "basis": "explicit_request",
                        "cites": [number],
                        "question": "",
                        "action_summary": "Edit the requested README",
                        "explanation": "The captured original assignment authorizes this edit.",
                        "restriction_quote": "",
                    }
                )
            )
        ),
        question,
    )
    assert diagnostics["cites_valid"]
    assert answer.user_would == "approve"
    assert answer.cites == [number]
