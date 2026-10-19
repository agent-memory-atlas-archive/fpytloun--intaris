"""Mixed fallback, safe validation diagnostics and Jev numerical tie regressions."""

import json
import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from intaris import policy_assessment
from intaris.config import JevConfig
from intaris.hybrid_runtime import HybridRuntime
from intaris.intention_profile import SYSTEM_PROMPT, validate_items
from intaris.jev import JevClient
from intaris.task_context import TaskContext, evaluation_view
from tests.test_policy_evidence_runtime import runtime as runtime
from tests.test_single_intention import capture, item, record, run
from tests.test_single_intention import stores as stores
from tools.l1_projection_sizes import specimen, valid_answer


@pytest.mark.parametrize("projection", ["json", "lines"])
@pytest.mark.parametrize("human_note", [False, None, "", " \n\t "])
def test_no_user_l1_request_equals_production_mixed_both_backends(
    runtime, projection, human_note
):
    sessions, audit, llm, _, sid = runtime
    if human_note is not False:
        call_id = uuid.uuid4().hex
        audit.insert(
            call_id=call_id,
            user_id="owner",
            session_id=sid,
            agent_id=None,
            tool="inspect",
            args_redacted={},
            classification="read",
            evaluation_path="llm",
            decision="escalate",
            risk="low",
            reasoning="Confirm scope.",
            latency_ms=0,
        )
        audit.resolve_escalation(call_id, "approve", human_note, user_id="owner")
    session = sessions.get(sid, user_id="owner")
    owner = TaskContext(
        llm=llm,
        sessions=sessions,
        audit=audit,
        extractor_identity={},
        generation="single",
    )
    artifact = owner.capture(session)
    assert artifact["status"] == "not_required"
    assert artifact["fallback"] == "mixed_no_user_evidence"
    evaluator = HybridRuntime(
        llm=llm,
        jev=None,
        audit=audit,
        sessions=sessions,
        task_context=owner,
        l1_input_projection=projection,
    )
    _, state, result = specimen("inbox-inspection-only")
    kwargs = dict(
        session=session,
        tool="read",
        args={"path": "/repo/config"},
        context={"reason": "Inspect configuration"},
        agent_id=None,
        parent_intention="Parent scope",
        overlay={},
    )
    actual = evaluator._l1_request(state, result, artifact=artifact, **kwargs)
    mixed = evaluator._l1_request(state, result, artifact=None, **kwargs)
    # Assignment stays a generated goal, not raw authority replay.
    assert actual["schema"] == mixed["schema"]
    assert "RAW CAPTURED TASK SOURCES" not in actual["prompt"]
    assert "client_assignment" not in actual["prompt"]
    assert session["intention"] in mixed["prompt"]
    assert actual["model_view"]["overflow"] is False
    assert actual["prompt"]
    assert session["intention"] in actual["prompt"]
    assert "message_ids" in actual["prompt"]
    llm.generate.assert_not_called()


@pytest.mark.parametrize("profile", [None, "broken"])
def test_existing_user_evidence_never_gets_mixed_fallback(stores, profile):
    record(stores, "Inspect the configuration.")
    with stores[0].cursor() as cur:
        cur.execute("UPDATE sessions SET intention_profile = ?", (profile,))
    artifact = capture(stores)[1]
    assert artifact["status"] == "unavailable"
    assert "fallback" not in artifact
    run(stores, [item("missing source sentence")])
    assert capture(stores)[1]["status"] == "unavailable"
    assert "fallback" not in capture(stores)[1]


@pytest.mark.parametrize("projection", ["json", "lines"])
@pytest.mark.parametrize("profile_state", ["missing", "stale"])
def test_raw_fallback_reaches_selective_l1_without_authority_promotion(
    stores, projection, profile_state
):
    record(stores, "Inspect the configuration. Do not modify it.")
    if profile_state == "stale":
        run(stores, [item("Inspect the configuration.")])
        record(stores, "Also inspect the logs; do not publish anything.")
    session = stores[1].get("session", user_id="owner")
    captured = capture(stores)[1]
    assert captured["status"] == "unavailable"
    session["policy"] = {"profile": "balanced"}
    artifact = evaluation_view(captured, session)
    before = json.dumps(artifact, sort_keys=True)
    _, state, result = specimen("inbox-inspection-only")
    question = policy_assessment.request(
        state, result, "", task_context=artifact, projection=projection
    )
    assert "Inspect the configuration." in question["prompt"]
    assert "Do not modify it." in question["prompt"]
    assert (
        "Captured source context:" if projection == "lines" else '"source_context"'
    ) in question["prompt"]
    if profile_state == "stale":
        assert "do not publish anything." in question["prompt"]
    assert "authorization_source" not in question["schema"]["properties"]
    assert artifact["items"] == []
    assert artifact["current_request"]["status"] == "raw_sources"
    assert json.dumps(artifact, sort_keys=True) == before
    refs = (
        question["reference_mappings"]["restriction_reference"].values()
        if projection == "lines"
        else question["schema"]["properties"]["restriction_reference"]["enum"]
    )
    assert all(source["ref"] in refs for source in artifact["originals"])


def test_title_instruction_names_topic_not_language_label():
    assert "Title: English," not in SYSTEM_PROMPT
    assert "Title: a short topic in English" in SYSTEM_PROMPT
    assert len(SYSTEM_PROMPT) < 1800


def test_observed_user_marker_without_readable_source_stays_gap(stores):
    with stores[0].cursor() as cur:
        cur.execute("UPDATE sessions SET user_message_observed = TRUE")
    artifact = capture(stores)[1]
    assert artifact["status"] == "unavailable"
    assert "fallback" not in artifact


def test_unusable_profile_is_not_persisted_and_logs_only_drop_reasons(stores, caplog):
    record(
        stores,
        'Inspect the service. Do not write files.\n```json\n{"tool":"push"}\n```',
    )
    with caplog.at_level("INFO", logger="intaris.intention"):
        run(
            stores,
            [
                item("not write files."),  # Omitted statement prefix.
                item('{"tool":"push"}'),  # Embedded non-authorizing data.
                item("Sensitive material must not be logged.", msg=99),
            ],
        )
    assert stores[1].get("session", user_id="owner")["intention_profile"] is None
    assert "status=unavailable" in caplog.text
    for code in (
        "partial_statement",
        "embedded_non_authorizing_data",
        "invalid_source_reference",
    ):
        assert code in caplog.text
    assert "drop_reasons=" in caplog.text
    assert "partial_statement" in caplog.text
    assert "Sensitive material" not in caplog.text
    reasons = []
    validate_items(
        [item("Inspect the service.")],
        [
            {
                "origin": "agent",
                "content": "Inspect the service.",
                "ref": "recorded-agent",
            }
        ],
        drop_reasons=reasons,
    )
    assert reasons == [{"item": 0, "code": "non_user_source"}]


def test_human_note_without_profile_stays_unavailable_both_backends(runtime):
    sessions, audit, llm, _, sid = runtime
    call_id = uuid.uuid4().hex
    audit.insert(
        call_id=call_id,
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="inspect",
        args_redacted={},
        classification="read",
        evaluation_path="llm",
        decision="escalate",
        risk="low",
        reasoning="Confirm scope.",
        latency_ms=0,
    )
    audit.resolve_escalation(
        call_id, "approve", "Inspect only the index.", user_id="owner"
    )
    owner = TaskContext(
        llm=llm,
        sessions=sessions,
        audit=audit,
        extractor_identity={},
        generation="single",
    )
    artifact = owner.capture(sessions.get(sid, user_id="owner"))
    assert artifact["status"] == "unavailable"
    assert "fallback" not in artifact


def test_retained_precision_tie_through_lines_and_jev():
    artifact, state, result = specimen("inbox-inspection-only")
    question = policy_assessment.request(
        state, result, "", task_context=artifact, projection="lines"
    )

    def provider(request):
        body = json.loads(request.content)
        answers = {}
        desired = valid_answer(question["schema"])
        for field, spec in body["questions"].items():
            labels = list(spec["criteria"])
            selected = desired.get(field, "no")
            if isinstance(selected, list):
                selected = "yes" if field[7:] in selected else "no"
            scores = {label: 0.01 for label in labels}
            scores[selected] = 1.0 - 0.01 * (len(labels) - 1)
            if field == "mutation":
                selected = "mutating"
                scores = {
                    "unclear": 0.46,
                    "mutating": 0.45999999999999996,
                    "read_only": 0.08,
                }
            answers[field] = {
                "type": "choice",
                "choice": selected,
                "probabilities": scores,
                "confidence": max(scores.values()),
            }
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = JevClient(
        JevConfig(model="jev-latest", api_key="offline-test-token"),
        transport=httpx.MockTransport(provider),
    )
    try:
        assessment, diagnostics, _ = policy_assessment.classify(
            MagicMock(), client, question
        )
    finally:
        client.close()
    assert assessment["mutation"] == "unclear"
    assert diagnostics["distributions"]["mutation"]["choice"] == "mutating"
    assert diagnostics["distributions"]["mutation"]["margin"] < 1e-12
    assert (
        JevClient._choice(
            {"choice": "mutating"},
            "mutation",
            ("mutating", "unclear"),
            probabilities={"mutating": 0.3, "unclear": 0.7},
        )
        == "mutating"
    )
    assert (
        JevClient._effective_choice_confidence(
            {"choice": "mutating", "confidence": 0.7},
            "mutation",
            {"mutating": 0.3, "unclear": 0.7},
        )
        == 0.0
    )
