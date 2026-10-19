"""Default policy and corrective review exercised through their owning boundaries."""

import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator
from intaris.config import JudgeConfig
from intaris.hybrid import Predicate, Rule, Truth, evaluate_policy, test_predicate
from intaris.judge import JudgeReviewer
from intaris.policy_evidence import CorrectedAssessment, Review, from_observations
from tests.test_intent_aligned import state
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime

RELAXED = ("balanced", "bounded_autonomy")
PROFILES = ("strict", *RELAXED)


def model_response(kwargs, **updates):
    answer = json.loads(response(kwargs))
    answer.update(updates)
    return json.dumps(
        {
            k: v
            for k, v in answer.items()
            if k in kwargs["json_schema"]["schema"]["properties"]
        }
    )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("risk", ["low", "medium"])
@pytest.mark.parametrize(
    "tool,args,effects,environment",
    [
        ("bash", {"command": "rm /tmp/task-owned-cache"}, ["delete"], "development"),
        (
            "artifact_publish",
            {"path": "/tmp/report.pdf"},
            ["write", "transmit"],
            "development",
        ),
        ("write", {"file_path": "/repo/parser.py"}, ["write"], "development"),
        ("config.update", {"setting": "sampling_interval"}, ["write"], "production"),
        (
            "bash",
            {"command": "git fetch origin && git status --short"},
            ["read", "execute", "write", "transmit"],
            "development",
        ),
    ],
)
def test_aligned_effect_matrix_routes_consequential_work_to_scope_review(
    runtime, profile, risk, tool, args, effects, environment
):
    sessions, audit, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})
    llm.generate.side_effect = lambda *a, **kw: model_response(
        kw,
        risk=risk,
        effects=effects,
        environment=environment,
        authorization="absent",
        authorization_source="none",
        authorization_scope="unclear",
    )
    # These commands are assessed only; no executor or live Judge is involved.
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool=tool,
        args=args,
    )
    consequential = profile == "strict" and "transmit" in effects
    assert result["decision"] == ("escalate" if consequential else "approve")
    assert llm.generate.call_count == 1
    metadata = audit.get_by_call_id(result["call_id"], user_id="owner")[
        "evaluation_metadata"
    ]
    assert metadata["judge_required"] is consequential
    if consequential:
        assert "intent.scope" in [
            item["id"] for item in metadata["policy_result"]["outstanding_obligations"]
        ]
    assert metadata["policy_evidence"]["review"] is None
    assert not metadata["policy_evidence"]["human_approved"]


@pytest.mark.parametrize("profile", RELAXED)
@pytest.mark.parametrize(
    "updates,obligation",
    [
        ({"target_scope": "conflicts"}, "model.denial"),
        ({"contextual_restrictions": "conflicts"}, "model.denial"),
        ({"risk": "high"}, "risk.high"),
        ({"risk": "critical"}, "model.risk.critical"),
    ],
)
def test_drift_and_severity_review_without_external_label(profile, updates, obligation):
    s = state(**updates)
    result = evaluate_policy([], s, profile, assessed=True)
    if profile == "bounded_autonomy" and updates == {"risk": "high"}:
        assert result.outcome == "approve"
        return
    assert result.outcome == "escalate"
    assert obligation in {o.id for o in result.outstanding_obligations}


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("original", ["conflict", "target", "critical"])
@pytest.mark.parametrize("corrected", ["low", "medium"])
@pytest.mark.asyncio
async def test_judge_corrects_model_interpretation_without_rewriting_l1(
    runtime, profile, original, corrected
):
    sessions, audit, llm, evaluator, sid = runtime
    sessions.update_session(sid, user_id="owner", policy={"profile": profile})
    updates = (
        {"contextual_restrictions": "conflicts"}
        if original == "conflict"
        else {"target_scope": "conflicts"}
        if original == "target"
        else {"risk": "critical"}
    )
    llm.generate.side_effect = lambda *a, **kw: model_response(
        kw, **updates, authorization="absent", authorization_source="none"
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="edit",
        args={"file_path": "/repo/parser.py"},
    )
    assert result["decision"] == "escalate"
    before = audit.get_by_call_id(result["call_id"], user_id="owner")[
        "evaluation_metadata"
    ]["policy_evidence"]
    llm.generate.side_effect = None
    llm.generate.return_value = json.dumps(
        {
            "user_would": "approve",
            "risk": corrected,
            "confidence": "high",
            "basis": "routine_within_task",
            "cites": [],
            "question": "",
            "action_summary": "Edit the task-owned parser implementation.",
            "explanation": "The edit is ordinary support for the assigned implementation.",
            "corrected_assessment": {
                "relationship": "supports_intent",
                "contextual_restrictions": "consistent",
                "restriction_reference": "session_intention",
                "target_scope": "consistent",
                "target_reference": "action",
            },
        }
    )
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(llm)
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="auto"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id=sid
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] == "approve"
    after = record["evaluation_metadata"]["policy_evidence"]
    assert after["assessment"] == before["assessment"]
    assert after["observations"] == before["observations"]
    assert after["review"]["risk"] == corrected
    assert not after["human_approved"]
    # Assessment and one contextual Judge; no authorization checker.
    assert llm.generate.call_count == 2


@pytest.mark.parametrize("profile", RELAXED)
def test_critical_review_must_target_model_obligation_and_preserve_hard_facts(profile):
    s = state(risk="critical")
    prior = evaluate_policy([], s, profile, assessed=True)
    for ids, risk, expected in [
        ([], "low", "escalate"),
        (["model.risk.critical"], None, "escalate"),
        (["model.risk.critical"], "medium", "approve"),
        (["model.risk.critical"], "critical", "deny"),
    ]:
        reviewed = s.model_copy(
            update={
                "review": Review(
                    obligation_ids=ids,
                    evidence_refs=[],
                    user_would="approve",
                    risk=risk,
                    basis="routine_within_task",
                )
            }
        )
        assert evaluate_policy([], reviewed, profile, assessed=True).outcome == expected
    assert [o.id for o in prior.outstanding_obligations] == ["model.risk.critical"]
    observed = from_observations({"risk": "critical"}).model_copy(
        update={"assessment": s.assessment, "review": reviewed.review}
    )
    assert evaluate_policy([], observed, profile, assessed=True).outcome == "deny"
    assert evaluate_policy([], s, "strict", assessed=True).outcome == "escalate"


@pytest.mark.parametrize("quantifier", ["any", "all"])
@pytest.mark.parametrize(
    "facts,expected",
    [
        ({"paths": [], "paths_complete": True}, Truth.FALSE),
        ({"paths": [], "paths_complete": False}, Truth.UNKNOWN),
        ({"paths_complete": True}, Truth.UNKNOWN),
        ({}, Truth.UNKNOWN),
    ],
)
def test_empty_path_scope_truth_and_negation(quantifier, facts, expected):
    node = Predicate(op="path_matches", value="/production/*", quantifier=quantifier)
    assert test_predicate(node, facts) is expected
    negative = Predicate(op="not", children=[node])
    assert test_predicate(negative, facts) is (
        Truth.TRUE if expected is Truth.FALSE else Truth.UNKNOWN
    )
    for op in ("all", "any"):
        assert test_predicate(Predicate(op=op, children=[node]), facts) is expected


@pytest.mark.parametrize("effect", ["allow", "deny", "require_approval"])
@pytest.mark.parametrize("quantifier", ["any", "all"])
def test_no_path_read_does_not_match_scoped_rules(runtime, effect, quantifier):
    sessions, _, llm, evaluator, sid = runtime
    rule = Rule(
        id="production_scope",
        effect=effect,
        when=Predicate(
            op="path_matches",
            value="/production/*",
            quantifier=quantifier,
        ),
    )
    sessions.update_session(
        sid,
        user_id="owner",
        policy={
            "profile": "balanced",
            "rules": [rule.model_dump()],
        },
    )
    result = evaluator.evaluate(
        user_id="owner",
        session_id=sid,
        agent_id=None,
        tool="read",
        args={},
    )
    assert result["decision"] == "approve"
    assert not result["evaluation_metadata"]["mandatory_rule_ids"]
    assert llm.generate.call_count == 0


@pytest.mark.parametrize("profile", RELAXED)
@pytest.mark.parametrize("effect", ["deny", "require_approval"])
def test_judge_does_not_override_configured_constraints(profile, effect):
    s = state().model_copy(
        update={
            "review": Review(
                obligation_ids=["human.checkpoint", "rule:scope"],
                evidence_refs=[],
                user_would="approve",
                risk="low",
                basis="routine_within_task",
            )
        }
    )
    rule = Rule(
        id="scope", effect=effect, when=Predicate(op="tool_matches", value="edit")
    )
    result = evaluate_policy([rule], s, profile, assessed=True)
    assert result.outcome == ("deny" if effect == "deny" else "escalate")
    unresolved = Rule(
        id="scope",
        effect=effect,
        when=Predicate(op="path_matches", value="/production/*"),
    )
    assert evaluate_policy([unresolved], s, profile, assessed=True).outcome != "approve"


@pytest.mark.parametrize("profile", RELAXED)
def test_validated_medium_judge_does_not_downgrade_prior_denial(profile):
    s = state(contextual_restrictions="conflicts")
    prior = evaluate_policy([], s, profile, assessed=True)
    q = judge_simulator.request(
        {"tool": "edit"},
        {"policy": {"profile": profile}, "intention": "Implement parser"},
        s,
        prior,
        {"originals": [], "items": []},
    )
    llm = Mock(
        generate=Mock(
            return_value=json.dumps(
                {
                    "user_would": "approve",
                    "risk": "medium",
                    "confidence": "high",
                    "basis": "routine_within_task",
                    "cites": [],
                    "question": "",
                    "action_summary": "Edit the local parser.",
                    "explanation": "The edit is routine support for the implementation task.",
                }
            )
        )
    )
    answer, diagnostics = judge_simulator.classify(llm, q)
    assert answer.user_would == "approve"
    assert diagnostics["downgrade_reason"] is None


@pytest.mark.parametrize(
    "relationship,risk,expected",
    [
        ("supports_intent", "low", ("approve", "approve", "approve")),
        ("supports_intent", "medium", ("approve", "approve", "approve")),
        ("unrelated", "low", ("escalate", "approve", "approve")),
        ("unrelated", "medium", ("escalate", "escalate", "approve")),
        ("conflicts_with_intent", "low", ("escalate", "escalate", "escalate")),
        ("conflicts_with_intent", "medium", ("escalate", "escalate", "escalate")),
        ("unclear", "low", ("escalate", "approve", "approve")),
        ("supports_intent", "unknown", ("escalate", "escalate", "approve")),
        ("supports_intent", "high", ("escalate", "escalate", "approve")),
    ],
)
def test_progressive_same_assessment_pipeline(runtime, relationship, risk, expected):
    sessions, _, llm, evaluator, sid = runtime
    for profile, outcome in zip(PROFILES, expected):
        sessions.update_session(sid, user_id="owner", policy={"profile": profile})
        llm.generate.side_effect = lambda *a, **kw: model_response(
            kw,
            relationship=relationship,
            risk=risk,
            contextual_restrictions="consistent",
            target_scope="consistent",
            authorization="absent",
            authorization_source="none",
        )
        result = evaluator.evaluate(
            user_id="owner",
            session_id=sid,
            agent_id=None,
            tool="config.update",
            args={"setting": "interval"},
        )
        assert result["decision"] == outcome


def test_common_judge_contract_and_no_vote_derived_permission():
    s = state(relationship="unrelated", risk="medium")
    prior = evaluate_policy([], s, "strict", assessed=True)
    questions = [
        judge_simulator.request(
            {"tool": "edit"},
            {"policy": {"profile": p}, "intention": "Implement parser"},
            s,
            prior,
            {"originals": [], "items": []},
        )
        for p in PROFILES
    ]
    for q in questions[1:]:
        assert q["messages"] != questions[0]["messages"]
    for vote in ("approve", "ask", "refuse"):
        old = Review(
            obligation_ids=["intent.scope", "intent.unrelated"],
            evidence_refs=[],
            user_would=vote,
            risk="medium",
        )
        reviewed = s.model_copy(update={"review": old})
        assert [
            evaluate_policy([], reviewed, p, assessed=True).outcome for p in PROFILES
        ] == (
            ["deny"] * 3
            if vote == "refuse"
            else ["escalate"] * 3
            if vote == "ask"
            else ["escalate", "escalate", "approve"]
        )


@pytest.mark.parametrize("profile", PROFILES)
def test_confirmed_real_constraints_and_harm_block_all_profiles(profile):
    for updates in (
        {
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
        },
        {"target_scope": "conflicts", "target_reference": "action"},
        {"relationship": "conflicts_with_intent", "risk": "high"},
    ):
        s = state(**updates)
        prior = evaluate_policy([], s, profile, assessed=True)
        correction = CorrectedAssessment.model_validate(
            {
                k: v
                for k, v in s.assessment.items()
                if k in CorrectedAssessment.model_fields
            }
        )
        receipt = Review(
            obligation_ids=[o.id for o in prior.outstanding_obligations],
            evidence_refs=[],
            user_would="approve",
            risk=s.value("risk"),
            corrected_assessment=correction,
        )
        assert (
            evaluate_policy(
                [], s.model_copy(update={"review": receipt}), profile, assessed=True
            ).outcome
            == "deny"
        )


@pytest.mark.parametrize("profile", PROFILES)
def test_grant_exception_requires_typed_cited_user_evidence(profile):
    s = state(relationship="unrelated").model_copy(
        update={
            "sources": {
                "message:1": {"independent_grant": True, "origin": "client_user_claim"},
            }
        }
    )
    answer = judge_simulator.Answer(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="explicit_request",
        cites=[1],
        question="",
        corrected_assessment=CorrectedAssessment(
            relationship="unrelated",
            authorization="explicit",
            authorization_scope="within_scope",
            authorization_source="message:1",
        ),
    )
    prior = evaluate_policy([], s, "strict", assessed=True)
    valid = judge_simulator.receipt(
        s,
        prior,
        answer,
        {1: "message:1"},
    )
    assert (
        evaluate_policy(
            [], s.model_copy(update={"review": valid}), profile, assessed=True
        ).outcome
        == "approve"
    )
    for refs in ({}, {1: "session_intention"}):
        invalid = judge_simulator.receipt(s, prior, answer, refs)
        assert invalid.corrected_assessment.authorization is None
        assert (
            evaluate_policy(
                [], s.model_copy(update={"review": invalid}), "strict", assessed=True
            ).outcome
            == "approve"
        )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("conflict", ["restriction", "target"])
@pytest.mark.parametrize(
    "partial",
    [
        {},
        {"relationship": "supports_intent"},
        {
            "contextual_restrictions": "consistent",
            "restriction_reference": "not_present",
        },
        {"target_scope": "consistent", "target_reference": "not_present"},
        {"target_reference": "session_intention"},
        {"restriction_reference": "action"},
        {"authorization_source": "session_intention"},
        {"authorization_scope": "within_scope"},
    ],
)
def test_omitted_or_rejected_correction_does_not_confirm_other_conflicts(
    profile, conflict, partial
):
    updates = (
        {
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
        }
        if conflict == "restriction"
        else {"target_scope": "conflicts", "target_reference": "action"}
    )
    s = state(**updates)
    prior = evaluate_policy([], s, profile, assessed=True)
    answer = judge_simulator.Answer(
        user_would="ask",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[],
        question="Uncertain scope",
        corrected_assessment=CorrectedAssessment.model_validate(partial),
    )
    receipt = judge_simulator.receipt(s, prior, answer)
    assert (
        evaluate_policy(
            [], s.model_copy(update={"review": receipt}), profile, assessed=True
        ).outcome
        == "escalate"
    )
    direct = Review(
        obligation_ids=[o.id for o in prior.outstanding_obligations],
        evidence_refs=[],
        user_would="ask",
        risk="low",
        corrected_assessment=answer.corrected_assessment,
    )
    persisted = Review.model_validate_json(direct.model_dump_json())
    assert (
        evaluate_policy(
            [], s.model_copy(update={"review": persisted}), profile, assessed=True
        ).outcome
        == "escalate"
    )


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("boundary", ["target", "restriction", "authorization"])
@pytest.mark.parametrize("matching", [False, True])
def test_observed_group_member_is_atomic_and_matching_witness_allows_correction(
    profile, boundary, matching
):
    from intaris.policy_evidence import Observation

    if boundary == "target":
        fields = {"target_scope": "consistent", "target_reference": "action"}
        initial = {"target_scope": "conflicts", "target_reference": "action"}
        protected, blocked = "target_scope", "conflicts"
        witness, value = "target_reference", "action"
    elif boundary == "restriction":
        fields = {
            "contextual_restrictions": "consistent",
            "restriction_reference": "session_intention",
        }
        initial = {
            "contextual_restrictions": "conflicts",
            "restriction_reference": "session_intention",
        }
        protected, blocked = "contextual_restrictions", "conflicts"
        witness, value = "restriction_reference", "session_intention"
    else:
        fields = {
            "authorization": "explicit",
            "authorization_source": "message:1",
            "authorization_scope": "within_scope",
        }
        initial = {
            "authorization": "explicit",
            "authorization_source": "none",
            "authorization_scope": "unclear",
            "risk": "high",
        }
        protected, blocked = "authorization_scope", "outside_scope"
        witness, value = "authorization_source", "message:1"
    s = state(**initial)
    s = s.model_copy(
        update={
            "sources": {
                **s.sources,
                "message:1": {
                    "independent_grant": True,
                    "origin": "client_user_claim",
                },
            },
            "observations": {
                **s.observations,
                (witness if matching else protected): Observation(
                    value=value if matching else blocked,
                    source="controller",
                    coverage="complete",
                    references=("action",),
                ),
            },
        }
    )
    original = s.model_dump()
    prior = evaluate_policy([], s, profile, assessed=True)
    answer = judge_simulator.Answer(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[1],
        question="",
        corrected_assessment=CorrectedAssessment(**fields),
    )
    live = judge_simulator.receipt(s, prior, answer, {1: "message:1"})
    # Isolate source-group handling from the independent risk receipt.
    live = live.model_copy(update={"risk": None})
    direct = Review(
        obligation_ids=[o.id for o in prior.outstanding_obligations],
        evidence_refs=["message:1"],
        user_would="approve",
        corrected_assessment=answer.corrected_assessment,
    )
    for receipt in (live, Review.model_validate_json(direct.model_dump_json())):
        result = evaluate_policy(
            [], s.model_copy(update={"review": receipt}), profile, assessed=True
        )
        expected_approval = matching or (
            profile == "bounded_autonomy" and boundary == "authorization"
        )
        assert result.outcome == ("approve" if expected_approval else "escalate")
    assert s.model_dump() == original


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(
    "partial",
    [
        {"authorization_source": "message:1"},
        {"authorization_scope": "within_scope"},
        {"authorization_source": "message:1", "authorization_scope": "within_scope"},
        {"authorization": "explicit", "authorization_source": "message:1"},
    ],
)
def test_orphan_authorization_cannot_upgrade_old_unproven_explicit(profile, partial):
    s = state(
        risk="high",
        authorization="explicit",
        authorization_source="none",
        authorization_scope="unclear",
    ).model_copy(
        update={
            "sources": {
                "message:1": {"origin": "client_user_claim", "independent_grant": True},
            }
        }
    )
    prior = evaluate_policy([], s, profile, assessed=True)
    answer = judge_simulator.Answer(
        user_would="ask",
        risk="high",
        confidence="high",
        basis="routine_within_task",
        cites=[1],
        question="",
        corrected_assessment=CorrectedAssessment(**partial),
    )
    live = judge_simulator.receipt(s, prior, answer, {1: "message:1"})
    direct = Review(
        obligation_ids=["model.denial"],
        evidence_refs=["message:1"],
        user_would="ask",
        corrected_assessment=answer.corrected_assessment,
    )
    for receipt in (live.model_copy(update={"risk": None}), direct):
        assert (
            evaluate_policy(
                [], s.model_copy(update={"review": receipt}), profile, assessed=True
            ).outcome
            == "escalate"
        )


@pytest.mark.parametrize("status", ["absent", "unclear"])
def test_negative_authorization_resets_source_and_scope(status):
    s = state(
        authorization="explicit",
        authorization_source="message:1",
        authorization_scope="within_scope",
    )
    correction = CorrectedAssessment(authorization=status)
    assert correction.validated_groups(s, []) == [
        {
            "authorization": status,
            "authorization_source": "none",
            "authorization_scope": "unclear",
        }
    ]


@pytest.mark.parametrize("profile", PROFILES)
def test_advisory_false_conflict_correction_has_reference_identity(profile):
    clause = {
        "id": "deny_policies.0",
        "effect": "deny",
        "text": "Do not change production files",
    }
    s = state(
        contextual_restrictions="conflicts", restriction_reference=clause["id"]
    ).model_copy(update={"advisory_clauses": [clause]})
    prior = evaluate_policy([], s, profile, assessed=True)
    q = judge_simulator.request(
        {"tool": "edit"},
        {"policy": {"profile": profile}, "intention": "Update local parser"},
        s,
        prior,
        {"originals": [], "items": []},
    )
    payload = json.loads(q["messages"][1]["content"])
    assert payload["operator_restrictions"]["clauses"] == [clause]
    assert clause["id"] in payload["reference_ids"]["restrictions"]
    # Independent review receives originals/restrictions, not L1's conclusion.
    assert "l1" not in payload
    assert s.assessment["restriction_reference"] == clause["id"]
    assert clause["id"] not in q["citable"].values()
    llm = Mock(
        generate=Mock(
            return_value=json.dumps(
                {
                    "user_would": "approve",
                    "risk": "low",
                    "confidence": "high",
                    "basis": "routine_within_task",
                    "cites": [],
                    "question": "Proceed?",
                    "action_summary": "Edit the local parser.",
                    "explanation": "The production restriction does not apply to this local edit.",
                    "corrected_assessment": {
                        "contextual_restrictions": "consistent",
                        "restriction_reference": clause["id"],
                    },
                }
            )
        )
    )
    answer, _ = judge_simulator.classify(llm, q)
    receipt = judge_simulator.receipt(s, prior, answer, q["citable"])
    assert (
        evaluate_policy(
            [], s.model_copy(update={"review": receipt}), profile, assessed=True
        ).outcome
        == "approve"
    )
    assert s.assessment["contextual_restrictions"] == "conflicts"


@pytest.mark.parametrize("profile", PROFILES)
def test_scope_review_can_confirm_harm_without_lowering_untargeted_risk(profile):
    s = state(relationship="unrelated", risk="medium")
    prior = evaluate_policy([], s, "strict", assessed=True)
    receipt = Review(
        obligation_ids=[o.id for o in prior.outstanding_obligations],
        evidence_refs=[],
        user_would="approve",
        risk="high",
        corrected_assessment=CorrectedAssessment(relationship="unrelated"),
    )
    assert (
        evaluate_policy(
            [], s.model_copy(update={"review": receipt}), profile, assessed=True
        ).outcome
        == "deny"
    )
