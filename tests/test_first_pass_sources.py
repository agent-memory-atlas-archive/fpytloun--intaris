"""First-pass inputs must not hide original instructions behind stale extraction."""

import copy

import pytest

from intaris.hybrid import evaluate_policy
from intaris.l1_projection import classifier_view
from intaris.policy_assessment import request, system_prompt
from tools.benchmark.first_pass import cases, specimen


def case(identifier):
    return next(row for row in cases() if row["id"] == identifier)


@pytest.mark.parametrize("mode", ["raw", "rich", "single"])
@pytest.mark.parametrize("projection", ["json", "lines"])
def test_complete_originals_replace_stale_generated_phase_in_every_projection(
    mode, projection
):
    artifact, state, pending = specimen(case("phase-plan-to-build"))
    artifact.update(
        context_mode=mode,
        compact_intention="PLAN only. No repository edits.",
        items=[
            {
                "kind": "restriction",
                "status": "current",
                "text": "No repository edits.",
                "source_ref": "earlier:0",
            }
        ],
    )
    before = copy.deepcopy(artifact)
    question = request(state, pending, "", task_context=artifact, projection=projection)
    current = artifact["originals"][-1]["content"]
    assert current in question["prompt"]
    assert "Intention.items[1].text: No repository edits." not in question["prompt"]
    assert artifact == before


def test_earlier_independent_prohibition_is_not_discarded_with_stale_items():
    artifact, state, pending = specimen(case("independent-no-push-persists"))
    artifact["context_mode"] = "rich"
    question = request(state, pending, "", task_context=artifact, projection="lines")
    for original in artifact["originals"]:
        assert original["content"] in question["prompt"]
    assert "Do not push this repository at any stage." in question["prompt"]
    assert (
        "generated summaries" in system_prompt(question, include_schema=False).lower()
    )


@pytest.mark.parametrize("mode", ["raw", "single", "rich"])
def test_original_restriction_omitted_by_extraction_is_still_citable(mode):
    artifact, state, pending = specimen(case("independent-no-push-persists"))
    artifact.update(context_mode=mode, items=[])
    question = request(state, pending, "", task_context=artifact, projection="lines")
    references = question["reference_mappings"]["restriction_reference"]
    labels = {
        label for label, reference in references.items() if reference == "earlier:0"
    }
    assert labels
    assert labels <= set(
        question["schema"]["properties"]["restriction_reference"]["enum"]
    )
    assert question["raw_sources"]
    assert artifact["originals"][0]["content"] in question["prompt"]


def test_rich_partial_sources_do_not_erase_generated_constraints_or_hide_gaps():
    artifact, state, pending = specimen(case("independent-no-push-persists"))
    artifact.update(context_mode="rich", status="ready")
    artifact["coverage"] = {"missing": ["original-assignment"]}
    artifact["items"] = [
        {
            "kind": "restriction",
            "status": "current",
            "text": "Do not push.",
            "source_ref": "earlier:0",
        }
    ]
    view = classifier_view(artifact, state)
    assert view["intention"]["items"]
    question = request(state, pending, "", task_context=artifact, projection="lines")
    assert "Gaps: intention or action facts incomplete" in question["prompt"]
    assert artifact["originals"][-1]["content"] in question["prompt"]


def test_raw_fallback_with_complete_sources_is_not_a_missing_intention():
    artifact, state, pending = specimen(case("phase-plan-to-build"))
    artifact["status"] = "extraction_failed"
    question = request(state, pending, "", task_context=artifact, projection="lines")
    # Action facts may independently remain incomplete; inspect the source view
    # rather than pretending that extractor failure erased the original input.
    assert classifier_view(artifact, state)["intention"]["source_context"]
    assert artifact["originals"][-1]["content"] in question["prompt"]


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "assessment",
    [
        {"contextual_restrictions": "conflicts", "restriction_reference": "none"},
        {
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
        },
        {"target_scope": "conflicts", "target_reference": "none"},
        {
            "contextual_restrictions": "conflicts",
            "restriction_reference": "missing-source",
            "target_scope": "conflicts",
            "target_reference": "none",
        },
    ],
)
def test_ungrounded_restrictions_bind_but_target_opinions_follow_profile(
    profile, assessment
):
    _, state, _ = specimen(case("ongoing-read-only-not-withdrawn"))
    state = state.model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "medium",
                "effects": ["write"],
                "mutation": "mutating",
                "contextual_restrictions": "consistent",
                "restriction_reference": "none",
                "target_scope": "consistent",
                "target_reference": "none",
                **assessment,
            }
        }
    )
    result = evaluate_policy([], state, profile, assessed=True)
    if profile == "bounded_autonomy" and assessment == {
        "target_scope": "conflicts",
        "target_reference": "none",
    }:
        assert result.outcome == "approve"
        assert "target_conflict" in result.ignored_opinions
        assert not result.outstanding_obligations
    else:
        assert result.outcome == "escalate"
        assert sum(o.id == "model.denial" for o in result.outstanding_obligations) == 1


def test_frozen_corpus_has_disjoint_development_holdout_and_negative_controls():
    rows = cases()
    assert len(rows) == len({row["id"] for row in rows}) == 24
    assert {row["split"] for row in rows} == {"development", "holdout"}
    assert sum(row["expected"] == "approve" for row in rows) == 12
    assert sum(row["expected"] == "review" for row in rows) == 12
