import json

import pytest

from intaris.config import JudgeConfig
from intaris.evaluation_context import normalize, runtime_view
from intaris.judge import JudgeReviewer
from intaris.judge_simulator import Answer
from tests.evidence_mock import EvidenceMock as Mock
from tests.test_hybrid_runtime import stores as _stores
from tests.test_read_only_overlay import answer

stores = _stores


def context():
    return {
        "tool": {
            "name": "bash_kill",
            "source": {"type": "executor"},
            "read_only": False,
            "capabilities": ["write", "privileged"],
            "descriptor": {
                "authority": "native",
                "operation": {"mutation_kind": "execute"},
            },
        },
        "shell_target": {
            "user_id": "owner",
            "session_id": "work",
            "shell_id": "shell_monitor",
            "executor_id": "executor_monitor",
            "coverage": "complete",
            "authority": "context_only",
            "target": {
                "command": "kubectl port-forward service/alertmanager 18938:9093",
                "description": "Temporary release verification tunnel",
                "status": "running",
                "output_tail": "private output",
            },
        },
    }


def normalized(value):
    return normalize(
        value,
        user_id="owner",
        session_id="work",
        evaluated_tool="bash_kill",
        args={"shell_id": "shell_monitor", "target_executor": "executor_monitor"},
    )


@pytest.mark.parametrize("field", ["user_id", "session_id", "shell_id", "executor_id"])
def test_target_binding_mismatch_is_unknown(field):
    value = context()
    value["shell_target"][field] = "other"
    result = normalized(value)["shell_target"]
    assert result == {"coverage": "unknown", "reason": "identity_mismatch"}


def test_runtime_projection_excludes_output_and_never_promotes_authority():
    value = context()
    value["shell_target"]["authority"] = "permission"
    result = runtime_view(normalized(value))["shell_target"]
    assert result["authority"] == "context_only"
    assert "output_tail" not in result["target"]


def test_tool_metadata_cannot_impersonate_the_evaluated_tool():
    result = normalize(
        context(),
        user_id="owner",
        session_id="work",
        evaluated_tool="bash",
        args={"shell_id": "shell_monitor"},
    )
    assert result["shell_target"]["reason"] == "identity_mismatch"


@pytest.mark.asyncio
async def test_l1_and_judge_see_target_even_without_recent_start_event(stores):
    _, sessions, audit, llm, evaluator = stores
    instruction = "Deploy and verify the release, then clean up temporary monitoring."
    sessions.create(user_id="owner", session_id="work", intention=instruction)
    sessions.allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": instruction},
    )
    assessed = json.loads(answer("mutating"))
    assessed["risk"] = "high"
    llm.generate.return_value = json.dumps(assessed)
    pending = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="bash_kill",
        args={"shell_id": "shell_monitor", "target_executor": "executor_monitor"},
        context=context(),
    )
    assert pending["decision"] == "escalate"  # provenance is not permission
    assert "Temporary release verification tunnel" in str(llm.generate.call_args)
    reviewer = Mock()
    reviewer.generate.return_value = Answer(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[1],
        question="",
        action_summary="Stop temporary monitoring tunnel",
        explanation="Cleanup of the specific monitoring job supports the release task.",
        restriction_quote="",
    ).model_dump_json()
    from tests.authorization_fixtures import add_supported_authorization_response

    add_supported_authorization_response(reviewer)
    judge = JudgeReviewer(
        llm=reviewer,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=pending["call_id"],
        user_id="owner",
        session_id="work",
    )
    assert "Temporary release verification tunnel" in str(reviewer.generate.call_args)
    record = audit.get_by_call_id(pending["call_id"], user_id="owner")
    assert record["user_decision"] == "approve"
