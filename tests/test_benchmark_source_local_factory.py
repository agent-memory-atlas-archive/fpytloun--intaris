"""Actual qualified server/SDK/proxy smoke with mock provider HTTP only."""

import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from tools.benchmark.provider_recorder import ProviderRecorder
from tools.benchmark.source_budget import CumulativeBudget
from tools.benchmark.source_local_factory import LocalFactory, api_artifact


def test_api_artifact_matches_call_tenant_and_session_without_observer():
    artifact = {
        "tenant": "bench",
        "session": "s",
        "artifact_digest": "a" * 64,
        "source_digest": "b" * 64,
    }
    response = {
        "call_id": "recorded-call",
        "evaluation_metadata": {"task_context": artifact},
    }
    audit = {**copy.deepcopy(response), "session_id": "s"}
    result = api_artifact(response, audit, session_id="s", user_id="bench")
    artifact["tenant"] = "another"
    assert result["artifact"]["tenant"] == "bench"
    with pytest.raises(ValueError):
        api_artifact(response, audit, session_id="s", user_id="bench")


def test_setup_counts_are_not_primary_quality_rows_or_budget_reset():
    from tools.benchmark.source_setup_plan import focused_setup_plan

    value = focused_setup_plan()
    assert value["fixture_control_sources"] == 2
    assert value["historical_setup_evaluations"] == 48
    assert value["normal_extra_attempt_envelope"] == 120
    assert value["primary_quality_evaluations"] == 1800
    assert value["scope_expansion_or_budget_reset"] is False


def mocked_provider(request):
    body = json.loads(request.content)
    if "questions" in body:
        answers = {}
        for field, question in body["questions"].items():
            choices = question["criteria"]
            values = {
                "risk": "low",
                "relationship": "directly_serves_intent",
                "authorization": "explicit",
                "authorization_source": "latest_user_message",
                "authorization_scope": "within_scope",
                "mutation": "mutating",
                "effects": ["write"],
                "environment": "development",
                "side_effect_scope": "bounded",
                "reversibility": "easy",
                "data_sensitivity": "ordinary",
                "exposure": "none",
                "consequence": "consequential",
                "contextual_restrictions": "consistent",
                "restriction_reference": "latest_user_message",
                "target_scope": "consistent",
                "target_reference": "action",
            }
            value = (
                ("yes" if field == "effect_write" else "no")
                if field.startswith("effect_")
                else values.get(field)
            )
            if value not in choices:
                value = choices[0]
            answers[field] = {
                "type": "choice",
                "choice": value,
                "confidence": 1,
                "probabilities": {v: float(v == value) for v in choices},
            }
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "answers": answers,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
    schema = body.get("response_format", {}).get("json_schema", {})
    name = schema.get("name")
    if name == "task_context_extraction":
        answer = {"items": []}
    elif not name:
        answer = {
            "title": "Local work",
            "intention": "Perform the requested local repository work.",
        }
    else:
        values = {
            "risk": "low",
            "relationship": "directly_serves_intent",
            "authorization": "explicit",
            "authorization_source": "latest_user_message",
            "authorization_scope": "within_scope",
            "mutation": "mutating",
            "effects": ["write"],
            "environment": "development",
            "side_effect_scope": "bounded",
            "reversibility": "easy",
            "data_sensitivity": "ordinary",
            "exposure": "none",
            "consequence": "consequential",
            "contextual_restrictions": "consistent",
            "restriction_reference": "latest_user_message",
            "target_scope": "consistent",
            "target_reference": "action",
        }
        props = schema["schema"]["properties"]
        judge = "classification" in props
        fields = props["classification"]["properties"] if judge else props
        answer = {field: values[field] for field in fields}
        if judge:
            answer = {
                "classification": answer,
                "obligations": {
                    key: {"status": "consistent", "reference": "latest_user_message"}
                    for key in props["obligations"]["properties"]
                },
            }
    return httpx.Response(
        200,
        json={
            "id": "local-transport",
            "model": body["model"],
            "choices": [
                {
                    "message": {"role": "assistant", "content": json.dumps(answer)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


@pytest.mark.parametrize("backend", ["llm", "jev"])
def test_actual_factory_qualified_api_component_and_proxy(tmp_path, backend):
    path = os.getenv("INTARIS_SOURCE_RUNTIME_PATH")
    if not path:
        pytest.skip("Explicit qualified source runtime path required")
    revision = os.getenv("INTARIS_SOURCE_RUNTIME_REVISION")
    assert revision, (
        "INTARIS_SOURCE_RUNTIME_REVISION must explicitly identify the qualified "
        "checkout supplied by INTARIS_SOURCE_RUNTIME_PATH"
    )
    budget = CumulativeBudget(reserve_usd=0.0589824, max_attempts=6000)
    with ProviderRecorder(
        "local-test",
        "local-test",
        max_requests=6000,
        budget=budget,
        forbid_compiler=True,
        extraction_schema_name="task_context_extraction",
    ) as relay:
        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(mocked_provider))
        factory = LocalFactory(
            runtime=Path(path),
            revision=revision,
            directory=tmp_path / "servers",
            relay=relay,
            proof={},
        )
        try:
            lifecycle = factory.open_history(
                arm="source_context",
                trial=1,
                backend=backend,
                judge="advisory",
                relay=relay,
                initial_assignment="Inspect local code.",
            )
            try:
                record = lifecycle.record(
                    {
                        "id": "recorded-user",
                        "kind": "message",
                        "text": "Update the local documentation only.",
                    },
                    origin="client_attributed_user",
                )
                assert record["reasoning_call_id"]
                response = lifecycle.client.evaluate(
                    lifecycle.session_id,
                    "bash",
                    {"command": "touch guide.md", "workdir": "/workspace/local"},
                )
                artifact = lifecycle.hooks.observe_action(lifecycle.session_id)
                assert artifact["state"] == "CAPTURED"
                assert artifact["call_id"] == response["call_id"]
                assert artifact["artifact"]["version"] == "task-context-v3"
                component = lifecycle.hooks.capture(
                    client=lifecycle.client, session_id=lifecycle.session_id
                )
                assert component["component_only"]
                assert component["artifact"]["session"] == lifecycle.session_id
                assert component["generation_status"] == "UNESTABLISHED"
            finally:
                lifecycle.hooks.close()
        finally:
            factory.close()
        assert relay.records and all(
            row["stage"] != "compiler" for row in relay.records
        )
        assert budget.snapshot()["attempts"] == len(relay.records)


def test_runner_to_actual_factory_contracts_and_shared_proof(tmp_path, monkeypatch):
    path = os.getenv("INTARIS_SOURCE_RUNTIME_PATH")
    if not path:
        pytest.skip("Explicit qualified source runtime path required")
    from tools.benchmark import source_context as primary
    from tools.benchmark.source_history_manifest import manifest_sha256
    from tools.benchmark.source_history_runner import execute_history, plan
    from tools.benchmark.source_scope import expansion

    manifest = json.loads(
        primary.FIXTURE.with_name("source-context-comparison-v1.json").read_text()
    )
    scope = expansion(manifest_sha256())
    proof = {"approved_revision": os.environ["INTARIS_SOURCE_RUNTIME_REVISION"]}
    for name in (
        "clean_runtime",
        "baseline_off_unit_evidence",
        "public_role_recording_verified",
        "public_scoped_resolution_verified",
        "equal_admitted_clues_verified",
        "extraction_telemetry_verified",
        "human_resolved_by_user_filter_verified",
        "compact_l1_rendering_verified",
        "judge_captured_originals_verified",
        "source_artifact_hash_verified",
        "pinned_config_verified",
        "price_bound_verified",
        "single_switch_verified",
        "role_recording_verified",
        "public_approval_verified",
        "equal_policy_clues_verified",
    ):
        proof[name] = {"passed": True, "reference": __file__, "sha256": "a" * 64}
    job = next(
        job
        for job in primary.schedule(manifest)
        if job["lane"] == "focused"
        and job["arm"] == "source_context"
        and job["case_id"] == "email-requested"
        and job["backend"] == "llm"
    )
    monkeypatch.setattr(primary, "schedule", lambda manifest: [job])
    budget = CumulativeBudget(reserve_usd=0.0589824, max_attempts=6000)
    with ProviderRecorder(
        "local-test",
        "local-test",
        max_requests=6000,
        budget=budget,
        forbid_compiler=True,
        extraction_schema_name="task_context_extraction",
    ) as relay:
        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(mocked_provider))
        factory = LocalFactory(
            runtime=Path(path),
            revision=proof["approved_revision"],
            directory=tmp_path / "runtime",
            relay=relay,
            proof=proof,
        )
        try:
            primary.execute(manifest, factory, relay, tmp_path / "primary", scope=scope)
            rows = [
                json.loads(line)
                for line in (tmp_path / "primary/results.jsonl")
                .read_text()
                .splitlines()
            ]
            assert len(rows) == 1 and rows[0]["result"]["intaris"]["call_id"]
            before = budget.snapshot()["attempts"]
            assert before > 0
            # Exercise unsupported transport through actual initialized session:
            # it must return a retained measurement, not abort the matrix.
            packet = copy.deepcopy(job["packet"])
            packet["events"] = [{"mechanism": "public_human_resolution"}]
            rows = factory.replay(
                packet=packet,
                arm="source_context",
                backend="llm",
                judge="advisory",
                trial=1,
                relay=relay,
                session_id="unestablished-history-binding",
            )
            assert rows[0]["state"] == "UNESTABLISHED"
            assert rows[0]["terminal"]["decision"] is None
            original_open = factory.open

            def opened(**kwargs):
                client, hooks = original_open(**kwargs)
                original_capture = hooks.capture

                def capture(**arguments):
                    result = original_capture(**arguments)
                    relay.stopped = True  # Declared mocked stop, after recording one real checkpoint.
                    return result

                hooks.capture = capture
                return client, hooks

            monkeypatch.setattr(factory, "open", opened)
            with pytest.raises(RuntimeError, match="boundary"):
                execute_history(
                    plan(manifest),
                    primary_manifest=manifest,
                    factories=factory,
                    relay=relay,
                    output=tmp_path / "history",
                    scope=scope,
                )
            info = json.loads((tmp_path / "history/run.json").read_text())
            assert (
                info["recorded_checkpoints"] == 1 and info["missing_checkpoints"] == 287
            )
            assert budget.snapshot()["attempts"] > before
        finally:
            factory.close()


@pytest.mark.parametrize("arm", ["baseline", "source_context"])
@pytest.mark.parametrize(
    "case_id", ["applicable-human-record", "unrelated-human-record"]
)
def test_literal_historical_human_source_through_public_api(tmp_path, arm, case_id):
    path = os.getenv("INTARIS_SOURCE_RUNTIME_PATH")
    if not path:
        pytest.skip("Explicit qualified runtime path required")
    from tools.benchmark.source_context import FIXTURE, focused_packet

    case = next(
        case
        for case in json.loads(FIXTURE.read_text())["cases"]
        if case["id"] == case_id
    )
    packet = focused_packet(case)
    frozen_packet = copy.deepcopy(packet)
    revision = os.environ["INTARIS_SOURCE_RUNTIME_REVISION"]
    budget = CumulativeBudget(reserve_usd=0.0589824, max_attempts=6000)
    with ProviderRecorder(
        "local-test",
        "local-test",
        max_requests=6000,
        budget=budget,
        forbid_compiler=True,
        extraction_schema_name="task_context_extraction",
    ) as relay:
        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(mocked_provider))
        relay.context = {"quality_role": "primary", "trial": 1}
        context = copy.deepcopy(relay.context)
        factory = LocalFactory(
            runtime=Path(path),
            revision=revision,
            directory=tmp_path / "runtime",
            relay=relay,
            proof={},
        )
        try:
            rows = factory.replay(
                packet=packet,
                arm=arm,
                backend="llm",
                judge="advisory",
                trial=1,
                relay=relay,
                session_id="historical-human-api-test",
            )
            assert packet == frozen_packet
            assert relay.context == context
            assert len(rows) == 1
            row = rows[0]
            assert "intaris" in row, row
            assert len(row["historical_setup"]) == 1
            setup = row["historical_setup"][0]
            old_action = packet["events"][1]["action"]
            audit = setup["record"]["public_record"]
            assert (
                audit["resolved_by"] == "user" and audit["user_decision"] == "approve"
            )
            assert audit["tool"] == old_action["tool"]
            assert audit["args_redacted"] == old_action["args"]
            assert setup["record"]["call_id"] == audit["call_id"]
            assert audit["call_id"] != row["intaris"]["call_id"]
            assert setup["primary_quality_row"] is False
            assert setup["provider_attempt_indices"]
            assert any(
                record.get("quality_role") == "historical_setup_control"
                for record in relay.records
            )
            assert budget.snapshot()["attempts"] == len(relay.records)
            assert budget.snapshot()["attempts"] > len(
                setup["provider_attempt_indices"]
            )
            if case_id == "unrelated-human-record":
                assert audit["args_redacted"] != packet["steps"][0]["args"]
            else:
                assert audit["args_redacted"] == packet["steps"][0]["args"]
        finally:
            factory.close()


@pytest.mark.parametrize(
    "failure",
    [
        "resolution_http",
        "wrong_origin",
        "budget_stop",
        "exhausted_ledger",
    ],
)
def test_failed_setup_retains_actual_call_audit_attempts_and_context(
    tmp_path, monkeypatch, failure
):
    path = os.getenv("INTARIS_SOURCE_RUNTIME_PATH")
    if not path:
        pytest.skip("Explicit qualified runtime path required")
    from tools.benchmark.client import IntarisError
    from tools.benchmark.source_context import FIXTURE, focused_packet
    from tools.benchmark.source_local_factory import LocalClient

    case = next(
        case
        for case in json.loads(FIXTURE.read_text())["cases"]
        if case["id"] == "applicable-human-record"
    )
    packet = focused_packet(case)
    revision = os.environ["INTARIS_SOURCE_RUNTIME_REVISION"]
    budget = CumulativeBudget(reserve_usd=0.0589824, max_attempts=6000)
    with ProviderRecorder(
        "local-test",
        "local-test",
        max_requests=6000,
        budget=budget,
        forbid_compiler=True,
        extraction_schema_name="task_context_extraction",
    ) as relay:
        relay._client.close()
        relay._client = httpx.Client(transport=httpx.MockTransport(mocked_provider))
        relay.context = {"quality_role": "primary", "trial": 1}
        context = copy.deepcopy(relay.context)
        if failure == "wrong_origin":
            readback = LocalClient.get_audit_record

            def wrong(self, call_id):
                audit = readback(self, call_id)
                if audit.get("resolved_by") == "user":
                    audit = {**audit, "resolved_by": "judge"}
                return audit

            monkeypatch.setattr(LocalClient, "get_audit_record", wrong)
        elif failure == "exhausted_ledger":
            evaluate = LocalClient.evaluate

            def exhausted(self, *args, **kwargs):
                if relay.context.get("quality_role") == "historical_setup_control":
                    # Public ledger operations simulate prior measured usage;
                    # the subsequent real API/proxy must refuse another send.
                    while budget.snapshot()["attempts"] < budget.max_attempts:
                        attempt = budget.reserve()
                        assert attempt is not None
                        budget.finish(
                            attempt,
                            {
                                "provider": "groq",
                                "model": "openai/gpt-oss-20b",
                                "status": 200,
                                "usage": {"cost": 0.0},
                            },
                        )
                return evaluate(self, *args, **kwargs)

            monkeypatch.setattr(LocalClient, "evaluate", exhausted)
        else:

            def reject(self, call_id, decision, note=None):
                if failure == "budget_stop":
                    relay.stopped = True
                raise IntarisError(409, "Mocked setup stop", self.base_url)

            monkeypatch.setattr(LocalClient, "resolve_escalation", reject)
        factory = LocalFactory(
            runtime=Path(path),
            revision=revision,
            directory=tmp_path / "runtime",
            relay=relay,
            proof={},
        )
        try:
            rows = factory.replay(
                packet=packet,
                arm="source_context",
                backend="llm",
                judge="advisory",
                trial=1,
                relay=relay,
                session_id="failed-historical-human-setup",
            )
            assert len(rows) == 1 and rows[0]["state"] == "UNESTABLISHED"
            setup = rows[0]["historical_setup"][0]
            assert setup["state"] == "UNESTABLISHED"
            if failure != "exhausted_ledger":
                assert setup["call_id"] == setup["initial_evaluation"]["call_id"]
                assert setup["available_audit"]["call_id"] == setup["call_id"]
                assert setup["provider_attempt_indices"]
                assert "record" not in setup
            else:
                assert setup["http_status"] == 503
                assert (
                    setup["cumulative_budget"]["stop_reason"]
                    == "cumulative_attempt_cap"
                )
                assert setup["provider_stop"] is True
                assert budget.snapshot()["attempts"] == 6000
                assert budget.snapshot()["stop_reason"]
            assert setup["action"] == packet["events"][1]["action"]
            assert relay.context == context
            if failure != "exhausted_ledger":
                assert budget.snapshot()["attempts"] == len(relay.records)
                assert any(
                    record.get("quality_role") == "historical_setup_control"
                    for record in relay.records
                )
            if failure == "budget_stop":
                assert relay.stopped is True
        finally:
            factory.close()
