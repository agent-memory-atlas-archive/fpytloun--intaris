"""Matched replay invariants, tested without model or fixture execution."""

from __future__ import annotations

import json
import time
from argparse import Namespace

import httpx
import pytest

from tools.benchmark.client import IntarisClient
from tools.benchmark.matched import (
    digest,
    freeze,
    replay_scenario,
    run,
    secret_file,
    summarize,
    verify_manifest,
)
from tools.benchmark.provider_recorder import ProviderRecorder, safe_usage
from tools.benchmark.terminal import read_terminal, terminal_outcome


def test_frozen_manifest_is_reproducible_and_detects_edits():
    manifest = freeze()
    assert manifest == freeze()
    verify_manifest(manifest)
    historical = [
        step
        for scenario in manifest["scenarios"]
        if scenario["cohort"] == "historical_gold"
        for step in scenario["steps"]
    ]
    assert len([step for step in historical if step["label"] is not None]) == 33
    assert len([step for step in historical if step["label"] is None]) == 1
    all_steps = [
        step for scenario in manifest["scenarios"] for step in scenario["steps"]
    ]
    assert len([step for step in all_steps if step["label"] is not None]) == 38
    manifest["seed"] += 1
    with pytest.raises(ValueError, match="hash"):
        verify_manifest(manifest)


def test_hybrid_cannot_be_silently_run_without_activation_adapter():
    with pytest.raises(ValueError, match="refusing to label"):
        run(Namespace(mode="hybrid_v1"))


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_terminal_reads_judge_resolution_without_calling_human(decision):
    outcome = terminal_outcome(
        {"decision": "escalate"},
        {
            "raw_decision": "escalate",
            "effective_decision": decision,
            "user_decision": decision,
            "resolved_by": "judge",
            "judge_decision": decision,
        },
    )
    assert outcome["initial_decision"] == "escalate"
    assert outcome["raw_decision"] == "escalate"
    assert outcome["decision"] == decision
    assert not outcome["human_required"]


def test_judge_recommendation_cannot_satisfy_human_floor():
    result = terminal_outcome(
        {"decision": "escalate"},
        {"effective_decision": "escalate", "judge_decision": "approve"},
    )
    assert result["decision"] == "escalate"
    assert result["human_required"]


def test_readback_timeout_and_error_are_not_denials():
    class Client:
        def get_audit_record(self, _call_id):
            return {"effective_decision": "escalate"}

    result = read_terminal(
        Client(),
        {"call_id": "audit-test-call", "decision": "escalate"},
        judge_enabled=True,
        timeout_s=0,
    )
    assert result["state"] == "unresolved_timeout"
    assert result["decision"] == "escalate"
    assert read_terminal(Client(), {}, judge_enabled=True)["state"] == "error"


def test_defer_stops_polling():
    class Client:
        def get_audit_record(self, _call_id):
            return {"effective_decision": "escalate", "judge_decision": "defer"}

    assert (
        read_terminal(
            Client(),
            {"call_id": "audit-test-deferral", "decision": "escalate"},
            judge_enabled=True,
        )["state"]
        == "human_required"
    )


def test_usage_whitelist_never_records_messages_or_credentials():
    assert safe_usage(
        {
            "prompt_tokens": 100,
            "cost": 0.001,
            "token": "sensitive",
            "messages": ["private"],
            "prompt_tokens_details": {"cached_tokens": 12, "text": "private"},
        }
    ) == {
        "prompt_tokens": 100,
        "cost": 0.001,
        "prompt_tokens_details": {"cached_tokens": 12},
    }


def test_provider_relay_counts_retries_and_keeps_payload_unchanged():
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [{"role": "user", "content": "inert"}],
    }
    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10}, "choices": []})

    with ProviderRecorder("test-secret", max_requests=2) as recorder:
        recorder._client.close()
        recorder._client = httpx.Client(transport=httpx.MockTransport(upstream))
        with httpx.Client() as client:
            headers = {"Authorization": f"Bearer {recorder.token}"}
            for _ in range(2):
                assert (
                    client.post(
                        recorder.url + "/l1/chat/completions",
                        headers=headers,
                        json=payload,
                    ).status_code
                    == 200
                )
            assert (
                client.post(
                    recorder.url + "/l1/chat/completions", headers=headers, json=payload
                ).status_code
                == 503
            )
            assert (
                client.post(
                    recorder.url + "/elsewhere", headers=headers, json=payload
                ).status_code
                == 404
            )
        assert seen == [payload, payload]
        assert len(recorder.records) == 2
        assert "test-secret" not in json.dumps(recorder.records)


def test_client_records_http_attempts(monkeypatch):
    responses = iter(
        [httpx.Response(500, json={}), httpx.Response(200, json={"decision": "deny"})]
    )
    monkeypatch.setattr("time.sleep", lambda _delay: None)
    with IntarisClient(
        "http://127.0.0.1:8061", "local-test-key", "bench", "benchmark"
    ) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url=client.base_url,
            transport=httpx.MockTransport(lambda _request: next(responses)),
        )
        assert (
            client.evaluate("test-session", "bash", {"command": "inert"})["decision"]
            == "deny"
        )
        assert [item["status"] for item in client.attempts] == [500, 200]


def test_credentials_require_private_regular_file(tmp_path):
    path = tmp_path / "provider.key"
    path.write_text("test-secret")
    path.chmod(0o600)
    assert secret_file(path) == "test-secret"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        secret_file(path)


def test_errors_remain_in_denominator_and_unknown_cost_is_not_zero():
    row = {
        "arm": "legacy_llm",
        "trial": 1,
        "cohort": "historical_gold",
        "label": "harmful",
        "error": {"status": 500},
        "http_attempts": [],
        "provider_attempts": [{"stage": "l1", "status": 500, "usage": None}],
    }
    result = summarize([row])["legacy_llm/trial-1/historical_gold"]
    assert result["counts"]["planned"] == 1
    assert result["counts"]["harmful_planned"] == 1
    assert result["counts"]["errors_or_unresolved"] == 1
    assert result["provider_cost_usd"] is None
    assert digest(row) == digest(dict(reversed(list(row.items()))))


def test_unverified_initial_approval_is_not_terminal_success():
    row = {
        "arm": "legacy_llm",
        "trial": 1,
        "cohort": "historical_gold",
        "label": "benign",
        "intaris": {"decision": "approve"},
        "terminal": {"decision": "approve", "state": "readback_error"},
        "end_to_end_ms": 1,
        "http_attempts": [],
        "provider_attempts": [],
    }
    counts = summarize([row])["legacy_llm/trial-1/historical_gold"]["counts"]
    assert counts["initial_approve"] == 1
    assert counts["errors_or_unresolved"] == 1
    assert counts.get("benign_approved", 0) == 0


def test_creation_attempts_are_counted_once():
    with ProviderRecorder("test-secret") as relay:

        class Client:
            attempts = []

            def create_session(self, *_args, **_kwargs):
                self.attempts.append({"status": 200})
                relay.records.append(
                    {"stage": "compiler", "status": 200, "usage": {"cost": 0.002}}
                )
                return {}

            def evaluate(self, *_args, **_kwargs):
                self.attempts.append({"status": 200})
                return {"decision": "approve", "call_id": "test-create-accounting"}

            def get_audit_record(self, _call_id):
                return {"effective_decision": "approve"}

        scenario = {
            "id": "creation-test",
            "intention": "Read source",
            "details": {},
            "policy": None,
            "split": "holdout",
            "cohort": "compilation",
            "steps": [
                {"id": f"create-{n}", "label": "benign", "tool": "read", "args": {}}
                for n in range(2)
            ],
        }
        rows = replay_scenario(
            scenario, Client(), relay, arm="hybrid_llm", trial=1, judge="disabled"
        )
        summary = summarize(rows)["hybrid_llm/trial-1/compilation"]
        assert summary["provider_requests"] == 1
        assert summary["stage_requests"] == {"compiler": 1}
        assert summary["provider_cost_usd"] == 0.002
        assert summary["http_attempts"] == 3


def test_late_provider_accounting_is_drained_after_runtime_timeout():
    def delayed(_request):
        time.sleep(0.15)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 17}})

    with ProviderRecorder("test-secret") as recorder:
        recorder._client.close()
        recorder._client = httpx.Client(transport=httpx.MockTransport(delayed))
        with httpx.Client(timeout=0.05) as client:
            with pytest.raises(httpx.ReadTimeout):
                client.post(
                    recorder.url + "/l1/chat/completions",
                    headers={"Authorization": f"Bearer {recorder.token}"},
                    json={"model": "openai/gpt-oss-20b"},
                )
        recorder.drain()
        assert recorder.records[0]["usage"] == {"prompt_tokens": 17}
        assert recorder.records[0]["status"] == 200
        assert recorder.records[0]["latency_ms"] >= 150
