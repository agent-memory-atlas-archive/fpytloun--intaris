"""Offline recovery and user-facing explanations; no provider requests."""

import asyncio
import json
import threading
from unittest.mock import MagicMock

import httpx
import pytest
from openai import BadRequestError, InternalServerError

from intaris.intention import IntentionBarrier
from intaris.judge_simulator import Answer, classify, user_explanation
from tests.test_single_intention import capture, item, record
from tests.test_single_intention import stores as _stores

stores = _stores


def failure(status=502):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://provider.test")
    )
    cls = BadRequestError if status == 400 else InternalServerError
    return cls("Provider text must not be logged", response=response, body=None)


def completion(text="Read README only.", *, msg=1):
    return json.dumps(
        {
            "title": "Read-only task",
            "intention": text,
            "items": [item(text, msg=msg)],
            "current": [0],
        }
    )


def barrier(stores, llm):
    result = IntentionBarrier(db=stores[0], llm=llm, generation="single")
    result._recovery_delays = (0, 0)
    return result


async def finish(owner):
    await owner._pending[("owner", "session")][1]


def test_failure_recovers_profile_and_unavailable_cache_without_eval_calls(
    stores, caplog
):
    record(stores, "Read README only.")
    context, missing, extraction = capture(stores)
    assert missing["status"] == "unavailable"
    assert not context._cache
    revision = stores[1].get("session", user_id="owner")["state_revision"]
    record(stores, "Background assistant activity.", role="assistant")
    assert stores[1].get("session", user_id="owner")["state_revision"] == revision
    assert (
        context.capture(stores[1].get("session", user_id="owner"))["status"]
        == "unavailable"
    )
    llm = MagicMock()
    llm.generate.side_effect = [failure(), completion()]
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session")
        await finish(owner)

    asyncio.run(execute())
    assert llm.generate.call_count == 2
    assert owner.recovery_attempts == 1
    session = stores[1].get("session", user_id="owner")
    assert session["state_revision"] > revision
    for _ in range(5):
        assert context.capture(session)["status"] == "ready"
    extraction.generate.assert_not_called()
    assert "Provider text must not be logged" not in caplog.text


@pytest.mark.parametrize("status, calls", [(400, 1), (502, 3)])
def test_deterministic_failure_not_retried_and_unknown_502_is_bounded(
    stores, status, calls
):
    record(stores, "Read README only.")
    llm = MagicMock()
    llm.generate.side_effect = failure(status)
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session")
        await finish(owner)
        for _ in range(4):
            await owner.wait("owner", "session")

    asyncio.run(execute())
    assert llm.generate.call_count == calls
    assert owner.recovery_exhausted == (status == 502)
    assert owner.recovery_non_retryable == (status == 400)


def test_changed_source_aborts_retry_before_second_provider_call(stores):
    record(stores, "Read README only.")
    llm = MagicMock()

    def change(*args, **kwargs):
        record(stores, "Do not read README.")
        raise failure()

    llm.generate.side_effect = change
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session")
        await finish(owner)

    asyncio.run(execute())
    assert llm.generate.call_count == 1
    assert stores[1].get("session", user_id="owner")["intention_profile"] is None


def test_duplicate_source_trigger_coalesces(stores):
    record(stores, "Read README only.")
    llm = MagicMock()
    llm.generate.side_effect = [failure(), completion()]
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session", source_event_seq=1)
        task = owner._pending[("owner", "session")][1]
        await owner.trigger("owner", "session", source_event_seq=1)
        assert owner._pending[("owner", "session")][1] is task
        await task

    asyncio.run(execute())
    assert llm.generate.call_count == 2


def test_cancel_during_backoff_prevents_retry(stores):
    record(stores, "Read README only.")
    llm = MagicMock()
    llm.generate.side_effect = failure()
    owner = barrier(stores, llm)
    owner._recovery_delays = (10, 10)

    async def execute():
        await owner.trigger("owner", "session")
        task = owner._pending[("owner", "session")][1]
        while not owner.update_errors:
            await asyncio.sleep(0.001)
        owner.cancel("owner", "session")
        await task

    asyncio.run(execute())
    assert llm.generate.call_count == 1
    assert not owner._pending


def test_unavailable_is_not_presented_as_user_withholding_permission():
    answer = Answer(
        user_would="ask",
        risk="medium",
        confidence="low",
        basis="missing_permission",
        cites=[],
        question="Please confirm this action.",
    )
    text = user_explanation(answer, {"status": "unavailable"}, "escalate")
    assert "Verified task context is currently unavailable" in text
    assert "does not mean you withheld permission" in text
    assert "specific action" in text
    assert not any(
        value in text for value in ("User simulator", "basis=", "cites=", "pending")
    )
    text = user_explanation(answer, {"status": "ready"}, "escalate")
    assert "Permission for this action has not been verified" in text


@pytest.mark.parametrize("would", ["approve", "ask"])
def test_model_ask_distinct_from_invalid_approval_downgrade(would):
    llm = MagicMock()
    llm.generate.return_value = json.dumps(
        {
            "user_would": would,
            "risk": "low",
            "confidence": "high",
            "basis": "explicit_request" if would == "approve" else "missing_permission",
            "cites": [],
            "question": "" if would == "approve" else "May I read this file?",
        }
    )
    answer, diagnostics = classify(
        llm,
        {
            "overflow": False,
            "messages": [],
            "schema": {},
            "citable": [],
            "trimmed_sections": [],
            "model_view": {"version": "evaluation-context-v1", "overflow": False},
        },
    )
    assert answer.user_would == would
    assert diagnostics["response_origin"] == "model"
    assert diagnostics["model_answer"]["user_would"] == would
    assert diagnostics["downgrade_reason"] is None
    if would == "ask":
        assert user_explanation(answer, {"status": "ready"}, "escalate") == (
            "Permission for this action has not been verified. "
            "Approve the request to read this file?"
        )


@pytest.mark.parametrize("supersede", [False, True])
def test_inflight_retry_cannot_publish_after_cancel_or_new_source(stores, supersede):
    record(stores, "Read README only.")
    entered, release = threading.Event(), threading.Event()
    calls = 0
    llm = MagicMock()

    def respond(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure()
        if calls == 2:
            entered.set()
            assert release.wait(5)
            return completion()
        return completion("Read LICENSE only.", msg=2)

    llm.generate.side_effect = respond
    owner = barrier(stores, llm)

    async def execute():
        await owner.trigger("owner", "session")
        old = owner._pending[("owner", "session")][1]
        assert await asyncio.to_thread(entered.wait, 5)
        if supersede:
            record(stores, "Read LICENSE only.")
            await owner.trigger("owner", "session")
            await finish(owner)
        else:
            owner.cancel("owner", "session")
        release.set()
        await old
        # Cancellation stops awaiting, not the executor thread. Join all executor
        # work before checking that the old completion could not publish.
        await asyncio.get_running_loop().shutdown_default_executor()

    asyncio.run(execute())
    current = stores[1].get("session", user_id="owner")
    if supersede:
        assert current["intention"] == "Read LICENSE only."
        assert current["intention_profile"] is not None
    else:
        assert current["intention"] == "Initial task"
        assert current["intention_profile"] is None


def test_duplicate_during_backoff_does_not_reset_budget(stores):
    record(stores, "Read README only.")
    llm = MagicMock()
    llm.generate.side_effect = failure()
    owner = barrier(stores, llm)
    owner._recovery_delays = (0.02, 0.02)

    async def execute():
        await owner.trigger("owner", "session", source_event_seq=1)
        task = owner._pending[("owner", "session")][1]
        while not owner.update_errors:
            await asyncio.sleep(0.001)
        for _ in range(10):
            await owner.trigger("owner", "session", source_event_seq=1)
            assert owner._pending[("owner", "session")][1] is task
        await task

    asyncio.run(execute())
    assert llm.generate.call_count == 3
    assert owner.recovery_exhausted == 1
