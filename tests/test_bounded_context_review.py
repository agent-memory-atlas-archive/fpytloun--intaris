import copy
import json
from unittest.mock import Mock

import pytest

from intaris.alignment import AlignmentUnavailable, check_intention_alignment
from intaris.input_budget import Capacity, ContextInsufficientError, fit, measure


@pytest.mark.parametrize("jev", [False, True])
@pytest.mark.parametrize("size", [100_000, 1_000_000])
def test_every_dynamic_surface_is_bounded_without_mutating_originals(jev, size):
    text = "KEEP START\n" + ("middle data\n" * (size // 12)) + "\nKEEP END"
    data = {
        "original_assignment": text,
        "raw_task_context": {
            "sources": [{"ref": "source:1", "origin": "user", "content": text}]
        },
        "tool_call": {"tool": "apply_patch", "args": {"patch": text}},
        "history": [{"text": "history " * 512}] * 400,
        "metadata": {f"field:{i}": "x" * 200 for i in range(1000)},
    }
    payload = (
        {
            "state": {**data, "evaluation_rules": "Never invent authorization."},
            "questions": {"risk": {"type": "choice", "choices": ["low", "high"]}},
        }
        if jev
        else {
            "messages": [
                {"role": "system", "content": "Never invent authorization."},
                {"role": "user", "content": json.dumps(data)},
            ],
            "max_tokens": 1000,
        }
    )
    before = copy.deepcopy(payload)
    capacity = Capacity(max_input_tokens=32000, status="known")
    bounded, view = fit(payload, capacity, jev=jev)
    assert not measure(bounded, capacity, jev=jev)["overflow"]
    assert payload == before
    assert view["trimmed"] and view["source_coverage"] == "partial"
    assert view["middle_truncations"]
    if jev:
        assert bounded["questions"] == payload["questions"]
        assert (
            bounded["state"]["evaluation_rules"] == payload["state"]["evaluation_rules"]
        )
        projected = bounded["state"]
    else:
        assert bounded["messages"][0] == payload["messages"][0]
        projected = json.loads(bounded["messages"][1]["content"])
    for value in (
        projected["original_assignment"],
        projected["tool_call"]["args"]["patch"],
    ):
        assert value.startswith("KEEP START") and value.endswith("KEEP END")
        assert "OMITTED MIDDLE" in value and "sha256=" in value
    assert projected["tool_call"]["tool"] == "apply_patch"
    assert projected["raw_task_context"]["sources"][0]["ref"] == "source:1"


def test_small_input_is_unchanged_and_unbounded_system_instructions_fail_closed():
    payload = {"messages": [{"role": "user", "content": "Inspect a file."}]}
    assert fit(payload)[0] == payload
    huge = {"messages": [{"role": "system", "content": "instruction " * 100000}]}
    with pytest.raises(ContextInsufficientError):
        fit(huge, Capacity(max_input_tokens=32000))


@pytest.mark.parametrize("aligned", [True, False])
def test_analysis_classifies_alignment_without_judge_wiring(aligned):
    jev = Mock()
    jev.check_alignment.return_value = (False, "Review is not implementation.")
    analysis = Mock()
    analysis.generate.return_value = json.dumps(
        {"aligned": aligned, "reasoning": "Reviewed original scope."}
    )
    result = check_intention_alignment(
        llm=analysis,
        jev=jev,
        parent_intention="Fix and review the parser.",
        child_intention="Review the parser implementation.",
        parent_assignment="Fix the parser. Do not publish or deploy.",
        child_assignment="Review only. Do not change files.",
    )
    assert result == (aligned, "Reviewed original scope.")
    analysis.generate.assert_called_once()
    prompt = analysis.generate.call_args.args[0][1]["content"]
    assert "Do not publish or deploy." in prompt and "Do not change files." in prompt


def test_unavailable_analysis_alignment_does_not_turn_into_permission():
    analysis = Mock()
    analysis.generate.side_effect = TimeoutError("offline")
    jev = Mock()
    jev.check_alignment.return_value = (False, "Requires contextual classification")
    with pytest.raises(AlignmentUnavailable):
        check_intention_alignment(
            llm=analysis,
            jev=jev,
            parent_intention="Read only",
            child_intention="Deploy changes",
        )


def test_positive_first_level_does_not_need_extra_analysis_call():
    jev, analysis = Mock(), Mock()
    jev.check_alignment.return_value = (True, "Supporting work")
    assert check_intention_alignment(
        llm=analysis,
        jev=jev,
        parent_intention="Fix parser",
        child_intention="Review parser",
    ) == (True, "Supporting work")
    analysis.generate.assert_not_called()


@pytest.mark.parametrize(
    "capacity", [Capacity(), Capacity(max_input_tokens=32000, status="known")]
)
@pytest.mark.parametrize("jev", [False, True])
def test_truncated_linked_scope_retains_identities_but_cannot_look_complete(
    capacity, jev
):
    proposal = "start " * 15000 + "Only staging; never production." + " end" * 15000
    sources = [
        {
            "ref": "proposal",
            "origin": "linked_non_authorizing_context",
            "content": proposal,
            "body_availability": "full_captured",
            "render_truncated": False,
        },
        {
            "ref": "human",
            "origin": "client_user_claim",
            "content": "Yes.",
            "linked_to": "proposal",
            "body_availability": "full_captured",
        },
    ]
    sources += [
        {"ref": f"log:{i}", "origin": "tool_context", "content": "log " * 500}
        for i in range(80)
    ]
    data = {
        "sources": sources,
        "reference_ids": {
            "sources": {str(i): row["ref"] for i, row in enumerate(sources, 1)}
        },
    }
    payload = (
        {"state": data, "questions": {}}
        if jev
        else {"messages": [{"role": "user", "content": json.dumps(data)}]}
    )
    output, view = fit(payload, capacity, jev=jev)
    projected = output["state"] if jev else json.loads(output["messages"][0]["content"])
    assert projected["reference_ids"] == data["reference_ids"]
    assert [r["ref"] for r in projected["sources"]] == [r["ref"] for r in sources]
    assert view["affirmative_grants_eligible"] is False
    assert projected["model_view_coverage"]["affirmative_grants_eligible"] is False
    assert all(
        r["body_availability"] == "partial_model_view" for r in projected["sources"]
    )
    assert all(r["grant_eligible"] is False for r in projected["sources"])
    assert sources[0]["body_availability"] == "full_captured"
    assert "Only staging; never production." in sources[0]["content"]


@pytest.mark.parametrize("at_preflight", [False, True])
@pytest.mark.parametrize("basis", ["explicit_request", "scoped_approval"])
def test_judge_cannot_approve_explicit_grant_from_partial_model_view(
    at_preflight, basis
):
    from intaris import judge_simulator
    from tests.test_user_simulator import answer, question

    q = question(latest_assistant="Please delete it.")
    original_ids = dict(q["citable"])
    view = {"affirmative_grants_eligible": False, "source_coverage": "partial"}
    vote = answer().model_copy(update={"basis": basis})
    llm = Mock(generate=Mock(return_value=vote.model_dump_json()), input_view={})
    if at_preflight:
        q["model_view"].update(view)
    else:
        llm.input_view = view
    vote, diagnostics = judge_simulator.classify(llm, q)
    assert vote.user_would != "approve"
    assert q["grant_citable"] == []
    assert q["citable"] == original_ids


def test_single_oversized_jev_prompt_is_cut_only_as_much_as_budget_requires():
    """One long Jev prompt keeps most of its middle, not a fixed 8k excerpt."""
    head = "## Session Intention\nFix the cache bug.\n"
    tail = "\n## Current Tool Call\nbash: git status\nLATEST USER: merge and push"
    middle = "".join(f"history row {i}: read file module_{i}.py\n" for i in range(2400))
    payload = {
        "state": {"prompt": head + middle + tail, "evaluation_rules": "Rules."},
        "model": "jev",
        "questions": {"risk": {"type": "choice", "choices": ["low", "high"]}},
    }
    capacity = Capacity(max_input_tokens=32000, status="known")
    original = len(payload["state"]["prompt"])
    assert measure(payload, capacity, jev=True)["overflow"]
    bounded, view = fit(payload, capacity, jev=True)
    prompt = bounded["state"]["prompt"]
    assert not measure(bounded, capacity, jev=True)["overflow"]
    assert prompt.startswith(head) and prompt.endswith(tail)
    assert view["middle_truncations"]
    # Budget-sized cut: far more than a fixed 8192-character excerpt survives.
    assert len(prompt) > 0.8 * original
