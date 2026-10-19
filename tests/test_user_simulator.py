import itertools
import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.classifier import name_effect
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import finalize_result
from intaris.policy_evidence import from_observations


def state(**changes):
    facts = dict(
        tool="inbox.delete",
        effects=["delete"],
        mutation="mutating",
        paths=[],
        operation="delete",
        risk="low",
        environment="development",
        reversibility="reversible",
    )
    facts.update(changes)
    return from_observations(
        facts,
        read_only=changes.get("read_only_required", False),
        sources={
            "message:one": {"origin": "client_user_claim", "independent_grant": True}
        },
    ).model_copy(
        update={
            "assessment": {
                "authorization": "explicit",
                "authorization_scope": "within_scope",
                "authorization_source": "message:one",
                "contextual_restrictions": "consistent",
                "restriction_reference": "message:one",
                "target_scope": "consistent",
                "target_reference": "action",
                "relationship": "directly_serves_intent",
                "consequence": "consequential",
            }
        }
    )


def answer(user_would="approve", risk="low", cites=None):
    return simulator.Answer(
        user_would=user_would,
        risk=risk,
        confidence="high",
        basis="explicit_request",
        cites=[1] if cites is None else cites,
        question="Confirm this action?" if user_would == "ask" else "",
        action_summary="Delete the selected inbox message.",
        explanation="The recorded user request names this exact message deletion.",
    )


def test_truncated_grant_cannot_authorize_simulator_approval():
    evidence = state()
    question = simulator.request(
        {"tool": "inbox.delete"},
        {},
        evidence,
        evaluate_policy([], evidence, "balanced"),
        {
            "originals": [
                {
                    "ref": "message:one",
                    "origin": "client_user_claim",
                    "content": "Delete the selected messages",
                    "truncated": True,
                }
            ],
            "items": [
                {
                    "source_ref": "message:one",
                    "kind": "request",
                    "quote": "Delete the selected messages",
                }
            ],
        },
    )
    assert "message:one" not in question["citable"].values()
    vote, diagnostics = simulator.classify(
        Mock(generate=Mock(return_value=answer().model_dump_json())),
        question,
    )
    assert vote.user_would == "approve"
    assert not diagnostics["cites_valid"]
    receipt = simulator.receipt(
        evidence, evaluate_policy([], evidence), vote, question["citable"]
    )
    assert receipt.corrected_assessment is None  # No invented permission.


@pytest.mark.parametrize(
    "profile,expected",
    # An explicit grant cited from the user's own message approves in every
    # mode, including strict, where it is the evidence (owner rule).
    [("strict", "approve"), ("balanced", "approve"), ("bounded_autonomy", "approve")],
)
def test_explicit_consequential_profiles(profile, expected):
    result = evaluate_policy([], state(), profile, assessed=True)
    assert result.outcome == expected
    if expected == "approve":
        assert result.outstanding_obligations == []


def test_bounded_known_read_does_not_require_applicability():
    evidence = state(effects=["read"], mutation="read_only").model_copy(
        update={"assessment": {"consequence": "unclear", "relationship": "unclear"}}
    )
    assert (
        evaluate_policy([], evidence, "bounded_autonomy", assessed=True).outcome
        == "approve"
    )


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
@pytest.mark.parametrize(
    "fact",
    [{"configured_deny": True}, {"risk": "critical"}, {"read_only_required": True}],
)
def test_simulator_cannot_override_controller_denies(profile, fact):
    evidence = state(**fact)
    prior = evaluate_policy([], evidence, profile, assessed=True)
    reviewed = evidence.model_copy(
        update={"review": simulator.receipt(evidence, prior, answer())}
    )
    assert evaluate_policy([], reviewed, profile, assessed=True).outcome == "deny"


@pytest.mark.parametrize("profile", ["strict", "balanced"])
def test_contextual_judge_can_correct_model_conflict(profile):
    evidence = state()
    evidence = evidence.model_copy(
        update={
            "assessment": {
                **evidence.assessment,
                "relationship": "conflicts_with_intent",
                "authorization": "absent",
                "authorization_source": "none",
            }
        }
    )
    prior = evaluate_policy([], evidence, profile, assessed=True)
    assert prior.outcome == "escalate"
    assert any(
        o.id == "model.denial" and o.protected for o in prior.outstanding_obligations
    )
    reviewed = evidence.model_copy(
        update={"review": simulator.receipt(evidence, prior, answer())}
    )
    assert evaluate_policy([], reviewed, profile, assessed=True).outcome == "approve"


@pytest.mark.parametrize(
    "maximum,minimum,verdict",
    itertools.product(
        [None, "escalate", "approve"],
        [None, "escalate"],
        ["approve", "ask", "refuse", "critical_refuse"],
    ),
)
def test_simulator_finalization_table(maximum, minimum, verdict):
    evidence = state()
    prior = evaluate_policy([], evidence, "balanced", assessed=True)
    vote = answer(
        "refuse" if verdict == "critical_refuse" else verdict,
        "critical" if verdict == "critical_refuse" else "low",
    )
    reviewed = evidence.model_copy(
        update={"review": simulator.receipt(evidence, prior, vote)}
    )
    result = evaluate_policy([], reviewed, "balanced", assessed=True)
    raw = {
        "approve": "approve",
        "ask": "escalate",
        "refuse": "deny",
        "critical_refuse": "deny",
    }[verdict]
    assert result.outcome == raw
    final = finalize_result(
        result,
        maximum=maximum,
        minimum=minimum,
        judge_allowed={"approve", "defer", "deny"},
        judge_uncertain="defer",
    )
    order = {"approve": 0, "escalate": 1, "deny": 2}
    capped = min(order[raw], order[maximum]) if maximum else order[raw]
    expected = max(capped, order[minimum]) if minimum else capped
    assert order[final.decision] == expected
    if minimum and order[minimum] > capped:
        assert final.override == "request.minimum_outcome"
    elif maximum and order[raw] > capped:
        assert final.override == (
            "waived_by_operator_bound"
            if maximum == "approve"
            else "session_policy.maximum_outcome"
        )


@pytest.mark.parametrize(
    "allowed,expected,override",
    [
        ({"approve", "deny", "defer"}, "escalate", "session_policy.maximum_outcome"),
        # The session maximum bounds the Judge-policy fallback too.
        ({"approve", "deny"}, "escalate", "session_policy.judge"),
    ],
)
def test_operator_judge_restrictions_remain_after_caps(allowed, expected, override):
    evidence = state()
    prior = evaluate_policy([], evidence, "balanced", assessed=True)
    reviewed = evidence.model_copy(
        update={
            "review": simulator.receipt(evidence, prior, answer("refuse", "critical")),
        }
    )
    final = finalize_result(
        evaluate_policy([], reviewed, "balanced", assessed=True),
        maximum="escalate",
        minimum=None,
        judge_allowed=allowed,
        judge_uncertain="defer" if "defer" in allowed else "deny",
    )
    assert final.decision == expected
    assert final.override == override


def question(**options):
    evidence = state()
    return simulator.request(
        {"tool": "inbox.delete", "args_redacted": {"ids": ["one"]}},
        {"intention": "Maintain inbox"},
        evidence,
        evaluate_policy([], evidence, "balanced", assessed=True),
        {
            "items": [
                {
                    "kind": "request",
                    "status": "current",
                    "quote": "Delete this message.",
                    "source_ref": "message:one",
                }
            ],
            "originals": [
                {
                    "ref": "message:one",
                    "origin": "client_user_claim",
                    "content": "Delete this message.",
                }
            ],
        },
        **options,
    )


@pytest.mark.parametrize("cites", [[], [0], [-1], [3]])
def test_invalid_cites_do_not_fabricate_permission(cites):
    llm = Mock()
    llm.generate.return_value = answer(cites=cites).model_dump_json()
    value, diagnostics = simulator.classify(
        llm, question(latest_assistant="Please delete it.")
    )
    assert value.user_would == "approve"
    assert diagnostics["cites_valid"] is False
    assert value.corrected_assessment is None


def test_small_explicit_preflight_never_drops_approval_antecedent():
    # Budget the entire finalized request, including new runtime/source metadata.
    baseline = (question()["model_view"]["bytes"] + 3) // 4 + 128
    q = question(reason="x" * 30000, latest_assistant="y" * 30000, max_tokens=baseline)
    assert q["overflow"] is True
    assert q["trimmed_sections"] == []
    assert "y" * 30000 in q["messages"][1]["content"]
    payload = json.loads(q["messages"][1]["content"])
    assert payload["action"]["args"] == {"ids": ["one"]}
    assert payload["originals"] == ["[1] Delete this message."]
    assert "intention_items" not in payload
    assert set(q["citable"]) == set(range(1, len(q["citable"]) + 1))
    assert q["citable"][1] == "message:one"


def test_prompt_has_numbered_sources_before_l1_and_no_policy_verdict():
    q = question()
    content = q["messages"][1]["content"]
    payload = json.loads(content)
    assert payload["originals"] == ["[1] Delete this message."]
    assert "intention_items" not in payload
    assert payload["reference_ids"]["sources"]["1"] == "message:one"
    assert "l1" not in payload
    assert payload["controller_facts"]["effects"] == ["delete"]
    # Provenance refs and context-only guardrail instructions are now explicit.
    # They do not constitute a policy verdict or model authority.
    assert payload["raw_task_context"]["sources"][0]["ref"] == "message:one"
    public_packet = {
        key: value
        for key, value in payload.items()
        if key not in {"runtime_context", "raw_task_context"}
    }
    # Review receives sources and non-authoritative action provenance, never
    # the first evaluator's conclusion or the policy's computed outcome.
    for forbidden in ("l1", "policy_result", "outstanding_obligations"):
        assert forbidden not in public_packet
    assert payload["action"]["authority"] == "none"
    assert q["schema"]["schema"]["properties"]["cites"]["items"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1,
    }
    assert payload["reference_ids"]["sources"]["1"] == "message:one"
    vote, diagnostics = simulator.classify(
        Mock(generate=Mock(return_value=answer().model_dump_json())),
        q,
    )
    assert vote.user_would == "approve"
    assert diagnostics["cites_valid"]
    evidence = state()
    receipt = simulator.receipt(
        evidence, evaluate_policy([], evidence, "balanced"), vote, q["citable"]
    )
    assert receipt.evidence_refs == ["message:one"]


def test_empty_sources_only_allow_empty_citations():
    evidence = state()
    q = simulator.request(
        {"tool": "inbox.delete"},
        {},
        evidence,
        evaluate_policy([], evidence, "balanced"),
        {},
    )
    assert q["citable"] == {}
    assert q["schema"]["schema"]["properties"]["cites"]["maxItems"] == 0


@pytest.mark.parametrize("profile", ["strict", "balanced", "bounded_autonomy"])
def test_invalid_assessment_remains_human_only_after_simulator_approval(profile):
    evidence = state().model_copy(update={"assessment_failed": True})
    prior = evaluate_policy([], evidence, profile)
    evidence = evidence.model_copy(
        update={"review": simulator.receipt(evidence, prior, answer())}
    )
    result = evaluate_policy([], evidence, profile)
    assert result.outcome == "escalate"
    assert any(
        o.id == "assessment.invalid_response" and o.requirement == "human"
        for o in result.outstanding_obligations
    )


@pytest.mark.parametrize(
    "tool,effect",
    [
        ("inbox.delete", "delete"),
        ("mail.send", "transmit"),
        ("db.inspect", "read"),
        ("files.update", "write"),
    ],
)
def test_name_observer(tool, effect):
    assert name_effect(tool) == effect


def test_positive_model_witness_cannot_be_erased_by_read_observation():
    evidence = state(effects=["read"], mutation="read_only").model_copy(
        update={"assessment": {"effects": ["delete"]}}
    )
    assert evidence.value("effects") == ["delete", "read"]
    assert evidence.value("mutation") == "mutating"


def _decision(index, decision="approve", **changes):
    return {
        "call_id": f"call-{index}",
        "tool": "inbox.delete",
        "args_redacted": {"message_id": f"m{index}"},
        "user_decision": decision,
        "user_note": None,
        **changes,
    }


def test_recent_human_decisions_are_citable_precedent():
    evidence = state()
    decisions = [_decision(index) for index in range(12)]
    decisions[1] = _decision(1, "deny", user_note="Never the archive folder")
    question = simulator.request(
        {"tool": "inbox.delete", "args_redacted": {"message_id": "m99"}},
        {},
        evidence,
        evaluate_policy([], evidence, "strict"),
        {"originals": [], "items": []},
        human_decisions=decisions,
    )
    cited = {source["ref"]: source["text"] for source in question["citation_sources"]}
    assert list(cited) == [f"decision:call-{index}" for index in range(12)]
    assert cited["decision:call-0"] == (
        'Earlier you approved inbox.delete with {"message_id": "m0"}'
    )
    assert cited["decision:call-1"].startswith("Earlier you denied inbox.delete")
    assert cited["decision:call-1"].endswith("Your note: Never the archive folder")
    assert (
        "Prior final human decisions are evidence" in question["messages"][0]["content"]
    )
    number = next(k for k, v in question["citable"].items() if v == "decision:call-0")
    result, diagnostics = simulator.classify(
        Mock(
            generate=Mock(
                return_value=json.dumps(
                    {
                        "user_would": "approve",
                        "risk": "low",
                        "confidence": "high",
                        "basis": "scoped_approval",
                        "cites": [number],
                        "question": "",
                        "action_summary": "Delete message m0 from the inbox.",
                        "explanation": "The earlier human decision covers this exact action.",
                    }
                )
            )
        ),
        question,
    )
    assert diagnostics["cites_valid"]
    assert result.user_would == "approve"
    receipt = simulator.receipt(
        evidence, evaluate_policy([], evidence, "strict"), result, question["citable"]
    )
    assert receipt.evidence_refs == ["decision:call-0"]


def test_unresolved_or_missing_decisions_add_no_sources():
    evidence = state()
    question = simulator.request(
        {"tool": "inbox.delete"},
        {},
        evidence,
        evaluate_policy([], evidence, "strict"),
        {"originals": [], "items": []},
        human_decisions=[_decision(0, None)],
    )
    assert question["citable"] == {}
