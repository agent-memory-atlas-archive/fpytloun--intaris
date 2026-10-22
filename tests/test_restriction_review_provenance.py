"""A guard report cannot substitute for the instruction a refusal cites."""

import json
from unittest.mock import Mock

import pytest

from intaris.hybrid import evaluate_policy
from intaris.judge_simulator import (
    REVIEW_UNAVAILABLE,
    Answer,
    classify,
    request,
    user_explanation,
    verified_restriction_quote,
)
from tests.test_user_simulator import state


def packet():
    s = state()
    c = {
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": "Run the focused tests. Previous diagnostic holds are retired.",
                "truncated": False,
            }
        ],
        "items": [],
    }
    return request(
        {"tool": "bash", "args_redacted": {"command": "vitest run"}},
        {},
        s,
        evaluate_policy([], s, "balanced", assessed=True),
        c,
        latest_assistant="The guard cited an old diagnostic-only prohibition.",
    )


def bare_packet():
    """No citable instruction text at all: nothing a restriction could cite."""
    s = state()
    return request(
        {"tool": "bash", "args_redacted": {"command": "vitest run"}},
        {},
        s,
        evaluate_policy([], s, "balanced", assessed=True),
        {"originals": [], "items": []},
        latest_assistant="The guard cited an old diagnostic-only prohibition.",
    )


def response(question, *, quote, cites):
    return Answer.model_validate(
        {
            "user_would": "refuse",
            "risk": "low",
            "confidence": "high",
            "basis": "restriction_conflict",
            "cites": cites,
            "restriction_quote": quote,
            "action_summary": "Run the focused tests.",
            "explanation": "A diagnostic-only restriction was reported.",
            "question": "",
        }
    )


@pytest.mark.parametrize("vote", ["refuse", "ask"])
def test_reported_denial_cannot_be_quoted_from_an_approval_source(vote):
    q = packet()
    index = next(iter(q["restriction_citable"]))
    answer = response(
        q, quote="The guard cited an old diagnostic-only prohibition.", cites=[index]
    )
    answer = answer.model_copy(
        update={
            "user_would": vote,
            "question": "Approve running the focused tests?" if vote == "ask" else "",
        }
    )
    assert not verified_restriction_quote(q, answer)
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = answer.model_dump_json()
    vote, diagnostics = classify(llm, q)
    assert vote.user_would == "ask"
    assert vote.basis == REVIEW_UNAVAILABLE
    assert vote.question == ""
    assert vote.corrected_assessment is None
    assert diagnostics["response_origin"] == "unverified_restriction"
    assert (
        diagnostics["validation_details"][0][0]["type"]
        == "unverified_restriction_quote"
    )
    assert llm.generate.call_count <= 2
    text = user_explanation(
        vote,
        {"status": "complete"},
        "escalate",
        {"tool": "bash", "risk": "unknown", "reasoning": "An old guard prohibited it."},
        review_failure=diagnostics["response_origin"],
    )
    assert "review-validation failure" in text
    assert "not evidence that you withheld permission" in text
    assert "risk assessment remains unknown" in text
    assert "Permission for this action has not been verified" not in text
    assert "An old guard prohibited it" not in text


def test_invalid_restriction_question_can_be_repaired_into_valid_task_approval():
    q = packet()
    index = next(iter(q["restriction_citable"]))
    invalid = response(
        q, quote="A prior guard prohibited this.", cites=[index]
    ).model_copy(update={"user_would": "ask", "question": "Approve this action?"})
    repaired = invalid.model_copy(
        update={
            "user_would": "approve",
            "basis": "routine_within_task",
            "restriction_quote": "",
            "cites": [],
            "question": "",
            "explanation": "Running focused tests supports the requested implementation.",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.side_effect = [invalid.model_dump_json(), repaired.model_dump_json()]
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "approve"
    assert diagnostics["response_origin"] == "model"
    assert llm.generate.call_count == 2


def test_restriction_retry_removes_the_unsupported_option_from_the_contract():
    q = bare_packet()
    assert not q["restriction_evidence_available"]
    invalid = response(q, quote="A prior guard prohibited this.", cites=[])
    repaired = invalid.model_copy(
        update={
            "user_would": "approve",
            "basis": "routine_within_task",
            "restriction_quote": "",
            "cites": [],
            "explanation": "The command runs the requested tests.",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.side_effect = [invalid.model_dump_json(), repaired.model_dump_json()]
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "approve"
    assert diagnostics["response_origin"] == "model"
    assert llm.generate.call_count == 2
    first_schema = llm.generate.call_args_list[0].kwargs["json_schema"]
    retry_schema = llm.generate.call_args_list[1].kwargs["json_schema"]
    assert (
        "restriction_conflict" in first_schema["schema"]["properties"]["basis"]["enum"]
    )
    assert (
        "restriction_conflict"
        not in retry_schema["schema"]["properties"]["basis"]["enum"]
    )
    assert (
        "restriction_conflict" in q["schema"]["schema"]["properties"]["basis"]["enum"]
    )
    retry_messages = llm.generate.call_args_list[1].args[0]
    assert "no longer an allowed basis" in retry_messages[-1]["content"]


@pytest.mark.parametrize("build", [packet, bare_packet])
def test_repeated_unsupported_restriction_after_retry_is_a_safe_failure(build):
    q = build()
    cites = [next(iter(q["restriction_citable"]))] if q["restriction_citable"] else []
    invalid = response(q, quote="A prior guard prohibited this.", cites=cites)
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.return_value = invalid.model_dump_json()
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "ask"
    assert answer.basis == REVIEW_UNAVAILABLE
    assert diagnostics["response_origin"] == "unverified_restriction"
    assert llm.generate.call_count == 2
    assert [d[0]["type"] for d in diagnostics["validation_details"]] == [
        "unverified_restriction_quote",
        "unverified_restriction_quote",
    ]


def test_citable_text_keeps_option_and_retry_names_the_quote_failure():
    """With citable text the option stays; the retry says exactly what failed."""
    q = packet()
    assert q["restriction_evidence_available"]
    index = next(iter(q["restriction_citable"]))
    invalid = response(q, quote="A prior guard prohibited this.", cites=[index])
    repaired = invalid.model_copy(
        update={
            "user_would": "approve",
            "basis": "routine_within_task",
            "restriction_quote": "",
            "cites": [],
            "explanation": "The command runs the requested tests.",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.side_effect = [invalid.model_dump_json(), repaired.model_dump_json()]
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "approve"
    assert diagnostics["response_origin"] == "model"
    retry_schema = llm.generate.call_args_list[1].kwargs["json_schema"]
    assert (
        "restriction_conflict" in retry_schema["schema"]["properties"]["basis"]["enum"]
    )
    retry_messages = llm.generate.call_args_list[1].args[0]
    assert "not found verbatim" in retry_messages[-1]["content"]
    assert "do not answer restriction_conflict" in retry_messages[-1]["content"]


def test_narrowed_retry_rewrites_embedded_schema_in_json_object_mode():
    q = bare_packet()
    invalid = response(q, quote="A prior guard prohibited this.", cites=[])
    repaired = invalid.model_copy(
        update={
            "user_would": "approve",
            "basis": "routine_within_task",
            "restriction_quote": "",
            "cites": [],
            "explanation": "The command runs the requested tests.",
        }
    )
    marker = "Return JSON conforming to:\n"
    seen_bases = []
    replies = iter([invalid.model_dump_json(), repaired.model_dump_json()])

    def generate(messages, **kwargs):
        # The system message is rewritten in place for the retry, so capture
        # the embedded schema at call time rather than from call_args later.
        system = messages[0]["content"]
        schema = json.loads(system.split(marker, 1)[1])
        seen_bases.append(list(schema["properties"]["basis"]["enum"]))
        return next(replies)

    llm = Mock(judge_response_mode="json_object", input_view=None)
    llm.generate.side_effect = generate
    answer, _ = classify(llm, q)
    assert answer.user_would == "approve"
    assert len(seen_bases) == 2
    assert "restriction_conflict" in seen_bases[0]
    assert "restriction_conflict" not in seen_bases[1]
    assert "routine_within_task" in seen_bases[1]


def test_restriction_retry_may_still_ask_for_a_genuine_scope_gap():
    q = bare_packet()
    invalid = response(q, quote="A prior guard prohibited this.", cites=[])
    asks = invalid.model_copy(
        update={
            "user_would": "ask",
            "basis": "outside_task",
            "restriction_quote": "",
            "cites": [],
            "question": "Which test file should be run?",
            "explanation": "The instruction names no test target.",
        }
    )
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.side_effect = [invalid.model_dump_json(), asks.model_dump_json()]
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "ask"
    assert answer.basis == "outside_task"
    assert answer.question == "Which test file should be run?"
    assert diagnostics["response_origin"] == "model"


@pytest.mark.parametrize(
    "instruction",
    [
        "Run the focused tests. Do not push this repository.",
        "Run the focused tests. Keep the production configuration unchanged.",
        "Run the focused tests. Don\u2019t push this repository.",
    ],
)
def test_unextracted_prohibition_keeps_the_restriction_option_available(instruction):
    """A missed extraction must not strip Judge's ability to cite a real limit,
    whatever form the restriction takes."""
    s = state()
    c = {
        "originals": [
            {
                "ref": "message:one",
                "origin": "client_user_claim",
                "content": instruction,
                "truncated": False,
            }
        ],
        "items": [],
    }
    q = request(
        {"tool": "bash", "args_redacted": {"command": "git push"}},
        {},
        s,
        evaluate_policy([], s, "balanced", assessed=True),
        c,
    )
    assert q["restriction_evidence_available"]
    index = next(iter(q["restriction_citable"]))
    paraphrased = response(q, quote="User prohibited pushing.", cites=[index])
    real_words = instruction.split(". ", 1)[1]
    corrected = paraphrased.model_copy(update={"restriction_quote": real_words})
    llm = Mock(judge_response_mode="json_schema", input_view=None)
    llm.generate.side_effect = [
        paraphrased.model_dump_json(),
        corrected.model_dump_json(),
    ]
    answer, diagnostics = classify(llm, q)
    assert answer.user_would == "refuse"
    assert answer.basis == "restriction_conflict"
    assert diagnostics["response_origin"] == "model"
    retry_schema = llm.generate.call_args_list[1].kwargs["json_schema"]
    assert (
        "restriction_conflict" in retry_schema["schema"]["properties"]["basis"]["enum"]
    )


def test_verbatim_words_of_cited_original_are_verified():
    q = packet()
    index = next(iter(q["restriction_citable"]))
    text = next(r["text"] for r in q["citation_sources"] if r["index"] == index)
    assert verified_restriction_quote(q, response(q, quote=text, cites=[index]))
    assert not verified_restriction_quote(q, response(q, quote=text, cites=[]))


def test_non_authorizing_assistant_report_is_not_an_instruction():
    q = packet()
    q["citation_sources"].append(
        {
            "index": 90,
            "ref": "assistant_report",
            "text": "The guard cited an old diagnostic-only prohibition.",
        }
    )
    assert not verified_restriction_quote(
        q,
        response(
            q, quote="The guard cited an old diagnostic-only prohibition.", cites=[90]
        ),
    )


def test_configured_clause_can_ground_a_refusal_without_a_user_source_index():
    q = packet()
    q["restriction_clauses"] = {"operator-clause": "Do not deploy."}
    answer = response(q, quote="Do not deploy.", cites=[])
    answer = Answer.model_validate(
        {
            **answer.model_dump(),
            "corrected_assessment": {"restriction_reference": "operator-clause"},
        }
    )
    assert verified_restriction_quote(q, answer)
    answer.restriction_quote = "Do not run unit tests."
    assert not verified_restriction_quote(q, answer)


def test_partial_view_cannot_verify_words_outside_the_visible_source():
    q = packet()
    index = next(iter(q["restriction_citable"]))
    for row in q["citation_sources"]:
        if row["index"] == index:
            row["text"] = "Visible source excerpt."
    assert not verified_restriction_quote(
        q, response(q, quote="A hidden prohibition.", cites=[index])
    )
