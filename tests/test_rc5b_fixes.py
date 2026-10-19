"""RC5b fixes from production evidence; model answers are fixtures."""

import json
from unittest.mock import Mock

import pytest

from intaris import judge_simulator as simulator
from intaris.classifier import verified_read_only_bash
from intaris.config import JudgeConfig
from intaris.effect_scope import external_action
from intaris.hybrid import evaluate_policy
from intaris.hybrid_runtime import _observe_action
from intaris.judge import JudgeReviewer
from intaris.policy_evidence import from_observations
from tests.test_policy_evidence_runtime import response, runtime  # noqa: F401

# Production balanced session: went to L1 and the Judge instead of static read.
SLEEP_GREP = 'sleep 1300; grep -E "decision|escalate" /tmp/intaris-rc5-3.log | tail -10'
# Production: command words inside a Python string literal flagged as delete.
PYTHON_LITERALS = (
    'uv run python -c "import shlex; '
    "for c in ['git branch -D x', '/usr/bin/rm x', 'git push origin main']: "
    'print(shlex.split(c))"'
)


@pytest.mark.parametrize(
    "command",
    [
        SLEEP_GREP,
        "true && date +%F; pwd",
        "printf '%s' x | sort -u | uniq -c -f 2 | wc -l",
        "date -d yesterday +%F",
        "false || echo done",
    ],
)
def test_side_effect_free_utilities_are_static_read_only(command):
    assert verified_read_only_bash({"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "echo x > out",
        "sleep 1; printf x >> out",
        "sort -o out in",
        "sort -uo out in",
        "sort --output=out in",
        "sort --compress-program=sh in",
        "uniq in out",
        "uniq 1 2",
        "uniq -c 1",
        "uniq -- -f 1",
        "date -- 0101",
        "date -s 2020-01-01",
        "date 010100002026.00",
        "date -u 0101",
        "sleep $(rm x)",
    ],
)
def test_writes_redirection_and_substitution_stay_unproven(command):
    assert not verified_read_only_bash({"command": command})


def test_sleep_grep_is_static_read_in_balanced_without_l1():
    facts = _observe_action("bash", {"command": SLEEP_GREP}, policy={})
    assert facts["static_read"]
    assert not facts["external_or_irreversible"]
    state = from_observations(facts)
    assert evaluate_policy([], state, "balanced").outcome == "approve"


@pytest.mark.parametrize(
    "command,external",
    [
        (PYTHON_LITERALS, False),
        ('bash -c "rm x"', False),  # interpreter code is left to L1
        ('echo "rm x"', False),
        ("grep rm notes.txt", False),
        ("rm x", True),
        ("/usr/bin/rm -f x", True),
        ("cd /tmp && rm -rf build", True),
        ("sudo rm x", True),
        ("sudo -u root rm x", True),
        ("xargs -I {} rm {}", True),
        ("timeout -s KILL 5 rm x", True),
        ("find . -name '*.o' | xargs rm", True),
        ("env FOO=1 rm x", True),
        ("echo $(rm x)", True),
        ("ls\nrm x", True),
        ("git push origin main", True),
        ("curl -X POST https://example.invalid", True),
        ("curl https://example.invalid", False),
    ],
)
def test_effect_detection_uses_executed_command_words(command, external):
    assert external_action("bash", {"command": command}) is external


def grep_state(**assessment):
    facts = _observe_action("bash", {"command": SLEEP_GREP}, policy={})
    facts["static_read"] = False
    state = from_observations(facts)
    return state.model_copy(
        update={
            "assessment": {
                "relationship": "supports_intent",
                "risk": "low",
                "contextual_restrictions": "consistent",
                **assessment,
            }
        }
    )


def judge_question(evidence, profile="balanced", **options):
    return simulator.request(
        {"tool": "bash", "args_redacted": {"command": SLEEP_GREP}},
        {"intention": "Watch the replay log", "policy": {"profile": profile}},
        evidence,
        evaluate_policy([], evidence, profile, assessed=True),
        {"originals": [], "items": []},
        **options,
    )


def ask(basis="missing_permission"):
    return json.dumps(
        {
            "user_would": "ask",
            "risk": "low",
            "confidence": "medium",
            "basis": basis,
            "cites": [],
            "question": "Do you want the grep search you proposed to run?",
            "action_summary": "Search the replay log",
            "explanation": "Assess this bounded diagnostic read against the current task.",
            "restriction_quote": "",
        }
    )


def test_missing_permission_basis_is_offered_only_in_strict():
    def bases(question):
        return question["schema"]["schema"]["properties"]["basis"]["enum"]

    assert "missing_permission" not in bases(judge_question(grep_state()))
    deleting = grep_state(effects=["delete"])
    assert "missing_permission" not in bases(judge_question(deleting))
    assert "missing_permission" not in bases(judge_question(grep_state(), "strict"))
    capped = judge_question(
        grep_state(), "bounded_autonomy", outcomes={"approve", "escalate"}
    )
    assert "missing_permission" not in bases(capped)


def test_balanced_missing_permission_ask_is_repaired():
    llm = Mock()
    approve = json.dumps(
        {
            "user_would": "approve",
            "risk": "low",
            "confidence": "high",
            "basis": "routine_within_task",
            "cites": [],
            "question": "",
            "action_summary": "Search the replay log",
            "explanation": "This bounded diagnostic read supports the requested investigation.",
            "restriction_quote": "",
        }
    )
    llm.generate.side_effect = [ask(), approve]
    answer, diagnostics = simulator.classify(llm, judge_question(grep_state()))
    assert llm.generate.call_count == 2
    repair = llm.generate.call_args.args[0][-1]["content"]
    assert repair == simulator.BASIS_REPAIR
    assert answer.user_would == "approve"
    assert diagnostics["downgrade_reason"] is None


@pytest.mark.parametrize(
    "assessment,expected",
    [
        ({}, "ask"),
        ({"risk": "medium"}, "ask"),
        ({"contextual_restrictions": "conflicts"}, "ask"),
    ],
)
def test_balanced_missing_permission_still_invalid_after_repair(assessment, expected):
    llm = Mock()
    llm.generate.side_effect = [ask(), ask()]
    answer, diagnostics = simulator.classify(
        llm, judge_question(grep_state(**assessment))
    )
    assert llm.generate.call_count == 2
    assert answer.user_would == expected
    assert diagnostics["downgrade_reason"] == "invalid_basis"
    assert diagnostics["response_origin"] == "model"


def test_balanced_missing_permission_is_repaired_for_explicit_effects_too():
    llm = Mock()
    llm.generate.side_effect = [ask(), ask("outside_task")]
    answer, diagnostics = simulator.classify(
        llm, judge_question(grep_state(effects=["delete"]))
    )
    assert llm.generate.call_count == 2
    assert (answer.user_would, answer.basis) == ("ask", "outside_task")


def test_explanation_includes_reviewer_question_without_internal_labels():
    answer = simulator.Answer(
        user_would="ask",
        risk="medium",
        confidence="medium",
        basis="outside_task",
        cites=[],
        question="User simulator: is your proposed grep search needed? basis=outside_task",
    )
    text = simulator.user_explanation(
        answer, {}, "escalate", {"tool": "bash", "args_redacted": {}}
    )
    assert text.startswith("This action may be outside the verified task scope.")
    assert "Intaris reviewer: is the agent's proposed grep search needed?" in text
    for label in ("basis", "cites", "User simulator", "your proposed"):
        assert label not in text


def test_explanation_without_reviewer_question_keeps_generic_prompt():
    answer = simulator.Answer(
        user_would="ask",
        risk="low",
        confidence="low",
        basis="missing_permission",
        cites=[],
        question="Please confirm this action.",
    )
    text = simulator.user_explanation(answer, {}, "escalate", {"tool": "bash"})
    assert text.endswith("Approve bash for this specific action?")


def call_bash(evaluator, session_id, command):
    return evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="bash",
        args={"command": command},
    )


def test_runtime_sleep_grep_is_static_in_balanced(runtime):  # noqa: F811
    sessions, _, llm, evaluator, session_id = runtime
    sessions.update_session(session_id, user_id="owner", policy={"profile": "balanced"})
    result = call_bash(evaluator, session_id, SLEEP_GREP)
    assert result["decision"] == "approve"
    assert llm.generate.call_count == 0


def test_runtime_escalation_names_the_l1_reason(runtime):  # noqa: F811
    _, audit, llm, evaluator, session_id = runtime
    llm.generate.side_effect = lambda *args, **kwargs: response(kwargs, high=True)
    result = evaluator.evaluate(
        user_id="owner",
        session_id=session_id,
        agent_id=None,
        tool="write",
        args={"file_path": "/repo/parser.py"},
    )
    assert result["decision"] == "escalate"
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["reasoning"] == (
        "The evaluator rated this action high risk, which needs independent assessment. "
        "The evaluator rated it high risk."
    )


@pytest.mark.asyncio
async def test_runtime_balanced_judge_ask_shows_reviewer_reason(runtime):  # noqa: F811
    sessions, audit, llm, evaluator, session_id = runtime
    sessions.update_session(
        session_id,
        user_id="owner",
        policy={"profile": "balanced", "maximum_outcome": "escalate"},
    )

    def answer(*args, **kwargs):
        if "user_would" in kwargs["json_schema"]["schema"]["properties"]:
            return ask()
        return response(kwargs, high=True)

    llm.generate.side_effect = answer
    result = call_bash(evaluator, session_id, 'grep -E "a(b)" /tmp/x.log')
    assert result["decision"] == "escalate"
    judge = JudgeReviewer(
        llm=llm,
        config=JudgeConfig(mode="advisory"),
        session_store=sessions,
        audit_store=audit,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=result["call_id"], user_id="owner", session_id=session_id
    )
    record = audit.get_by_call_id(result["call_id"], user_id="owner")
    assert record["user_decision"] is None
    stage = record["evaluation_metadata"]["stages"][-1]
    assert stage["downgrade_reason"] == "invalid_basis"
    explanation = record["judge_reasoning"]
    assert "couldn't complete its review" in explanation
    assert "basis" not in explanation
