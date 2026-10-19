"""Chat labels do not decide outcomes; policy and independent review do."""

import json

import pytest

from intaris.config import JudgeConfig
from intaris.judge import JudgeReviewer
from intaris.judge_simulator import Answer
from intaris.read_only import enforce, observe, resolve
from tests.evidence_mock import EvidenceMock as Mock
from tests.test_hybrid_runtime import stores as _stores
from tests.test_read_only_overlay import answer

stores = _stores

INSPECT = (
    "cd ~/src/sample && git status -sb | head -1 && git log --oneline -1 "
    "&& ls sample/store/migrations/versions | sort | tail -3 "
    '&& grep -n "class StoredDocumentRow" -A50 sample/store/models.py | head -60'
)


@pytest.mark.parametrize("mode", ["default", "plan", "build"])
@pytest.mark.parametrize("mutation", ["read_only", "unclear", "mutating"])
def test_chat_mode_does_not_impose_an_execution_ceiling(mode, mutation):
    overlay = resolve({"chat_mode": mode})
    assert not overlay["read_only_required"]
    assert enforce("approve", overlay, mutation) == ("approve", None)
    assert enforce("escalate", overlay, mutation) == ("escalate", None)
    assert enforce("deny", overlay, mutation) == ("deny", None)


@pytest.mark.parametrize("mode", ["default", "plan", "build"])
def test_explicit_ceiling_is_preserved_in_every_mode(mode):
    overlay = resolve(
        {"chat_mode": mode, "read_only_required": False},
        {"read_only_required": True},
    )
    assert enforce("approve", overlay, "mutating")[0] == "deny"
    assert enforce("approve", overlay, "unclear")[0] == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["default", "plan", "build"])
@pytest.mark.parametrize(
    "command,observed",
    [(INSPECT, "unclear"), ("printf outline > /tmp/artifact-plan.md", "mutating")],
)
async def test_supporting_action_reaches_judge_and_can_be_approved(
    stores, mode, command, observed
):
    _, sessions, audit, llm, evaluator = stores
    instruction = (
        "Inspect the sample document model and migration history to prepare a plan "
        "in /tmp/artifact-plan.md. Do not implement the plan."
    )
    sessions.create(user_id="owner", session_id="work", intention=instruction)
    sessions.allocate_event_sequences(
        "work",
        user_id="owner",
        user_message_flags=[True],
        latest_user_evidence={"content": instruction},
    )
    # Preserve the conservative classifier result: no special command allowlist.
    assert observe("bash", {"command": command}) == observed
    uncertain = json.loads(answer("unclear"))
    uncertain["risk"] = "high"
    llm.generate.return_value = json.dumps(uncertain)
    pending = evaluator.evaluate(
        user_id="owner",
        session_id="work",
        agent_id=None,
        tool="bash",
        args={"command": command},
        context={"chat_mode": mode},
    )
    assert pending["decision"] == "escalate"
    assert not pending["evaluation_metadata"]["overlay"]["read_only_required"]
    reviewer = Mock()
    reviewer.generate.return_value = Answer(
        user_would="approve",
        risk="low",
        confidence="high",
        basis="routine_within_task",
        cites=[1],
        question="",
        action_summary="Inspect repository status, history and artifact model",
        explanation="These bounded inspection commands support the requested plan.",
        restriction_quote="",
    ).model_dump_json()
    judge = JudgeReviewer(
        llm=reviewer,
        config=JudgeConfig(mode="advisory"),
        audit_store=audit,
        session_store=sessions,
        evaluator=evaluator,
    )
    await judge.review_and_resolve(
        call_id=pending["call_id"], user_id="owner", session_id="work"
    )
    record = audit.get_by_call_id(pending["call_id"], user_id="owner")
    assert record["user_decision"] == "approve"
    assert record["resolved_by"] == "judge"
    reviewer.generate.assert_called_once()
