"""Adapter tests with mocked API responses, NOT measured classifier evidence."""

from __future__ import annotations

import copy
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

from tools.benchmark.client import IntarisClient
from tools.benchmark.hybrid_suite import (
    assess,
    compilation_fidelity,
    compile_case,
    freeze_suite,
    gate_case,
    matrix_assessment,
    profile_assessment,
    run_suite,
    summary,
    trace,
    verify_hybrid_rows,
)
from tools.benchmark.matched import freeze
from tools.benchmark.provider_recorder import ProviderRecorder

FIXTURES = Path(__file__).resolve().parents[1] / "tools/benchmark/fixtures"


@pytest.fixture
def suite(tmp_path):
    contract = tmp_path / "contract.md"
    contract.write_text(
        "hybrid_v1 require_approval evaluation_policy_version path_matches No controller"
    )
    corpus = FIXTURES / "hybrid-real-world-cases.json"
    return freeze_suite(
        corpus, FIXTURES / "hybrid-policy-stage-matrix.json", contract, freeze(corpus)
    )


def test_complete_source_inventory_not_live_evidence(suite):
    ids = suite["mechanical_coverage"]["source_fixture_ids"]
    matrix = suite["mechanical_coverage"]["stage_ids"]
    assert len(ids) == len(set(ids)) == 46
    assert len(matrix) == len(set(matrix)) == 22
    mapped = (
        [x["id"] for x in suite["actions"]]
        + [x["id"] for x in suite["compilation"]]
        + [x["id"] for x in suite["lifecycle"]]
    )
    assert set(ids) <= set(mapped)
    assert len(suite["profile_matrix"]) == 54
    assert suite["live_results"] is False
    for action in suite["actions"]:
        assert action["assertions_are_not_model_input"]
        assert "controller_context" not in action["steps"][0]
        assert "classification_assertions" not in action["steps"][0]


def test_profile_outcomes_are_substantively_different_not_tuning_labels(suite):
    medium = [
        x for x in suite["profile_matrix"] if x["family"] == "supporting_write_medium"
    ]
    assert {x["policy"]["profile"]: x["expected_terminal"] for x in medium} == {
        "balanced": "approve",
        "strict": "escalate",
        "bounded_autonomy": "approve",
    }
    assert all(x["classification_condition"]["risk"] == ["medium"] for x in medium)
    source = next(x for x in suite["actions"] if x["id"] == "push_requires_human")
    assert source["policy"]["rules"][0]["effect"] == "require_approval"
    assert source["policy"]["maximum_outcome"] == "approve"


def test_missing_activation_and_unverified_decisions_fail_closed():
    rows = [
        {
            "intaris": {"decision": "approve"},
            "terminal": {"state": "terminal", "decision": "approve"},
        }
    ]
    verify_hybrid_rows(rows)
    assert rows[0]["error"] == "hybrid_activation_not_confirmed"
    assert assess(rows[0], ["approve"])["conformant"] is False
    assert assess(
        {"terminal": {"state": "readback_error", "decision": "approve"}}, ["deny"]
    )["unsafe_approval"]


def test_raw_and_final_stage_trace_preserves_actual_provenance():
    row = {
        "intaris": {
            "classification": "write",
            "raw_decision": "escalate",
            "evaluation_metadata": {
                "stages": [{"source": "l1"}],
                "facts": {"risk": "high"},
            },
        },
        "terminal": {
            "decision": "approve",
            "audit": {
                "classification": "write",
                "raw_decision": "escalate",
                "evaluation_metadata": json.dumps(
                    {
                        "profile": "balanced",
                        "stages": [{"source": "l1"}, {"source": "judge"}],
                        "facts": {"risk": "medium"},
                        "human_approval_required": False,
                    }
                ),
            },
        },
    }
    result = trace(row)
    assert result["initial_raw"] == "escalate"
    assert result["final_decision"] == "approve"
    assert result["initial_facts"]["risk"] == "high"
    assert result["final_facts"]["risk"] == "medium"
    assert len(result["final_stages"]) == 2


def test_profile_premise_not_observed_is_not_success():
    row = {
        "terminal": {
            "decision": "approve",
            "state": "terminal",
            "audit": {"evaluation_metadata": {"facts": {"risk": "low"}}},
        }
    }
    result = profile_assessment(
        row,
        {
            "expected_terminal": "approve",
            "classification_condition": {"risk": ["medium"]},
        },
    )
    assert result["premise_not_observed"]
    assert not result["conformant"]


def test_readiness_guard_never_starts_dirty_runtime():
    with pytest.raises(ValueError, match="ready runtime"):
        run_suite(
            Namespace(
                ready_runtime_revision=None,
                revision="1234567890abcdef1234567890abcdef12345678",  # Synthetic revision.
            )
        )


class FakeAPI:
    """Mock API/cache for measurement code tests; no model or shell execution."""

    def __init__(self, relay, state=None, user_id="unit-tenant"):
        self.relay = relay
        self.state = state if state is not None else {"sessions": {}, "cache": set()}
        self.user_id = user_id
        self.attempts = []
        self.pending_seen = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def fork_tenant(self, user_id):
        return FakeAPI(self.relay, self.state, user_id)

    def _policy(self, p):
        clause = p.get("deny_policies") or p.get("allow_policies")
        if clause:
            key = (self.user_id, json.dumps(clause))
            if key not in self.state["cache"]:
                self.relay.records.append(
                    {
                        "stage": "compiler",
                        "model": "openai/gpt-oss-120b",
                        "status": 200,
                        "usage": {"cost": 0.001},
                    }
                )
                if "unsupported" in str(clause):
                    from tools.benchmark.client import IntarisError

                    raise IntarisError(
                        422, "unsupported_policy", "http://127.0.0.1/api/v1/intention"
                    )
                self.state["cache"].add(key)
        return copy.deepcopy(p)

    def create_session(self, sid, intention, *, policy, details):
        self.attempts.append({"status": 200})
        stored = self._policy(policy)
        self.state["sessions"][(self.user_id, sid)] = {
            "intention": intention,
            "policy": stored,
            "policy_revision": 1,
            "active_policy": {
                "rules": stored.get("rules")
                or [
                    {
                        "id": "deny_policies.0",
                        "effect": "deny",
                        "when": {"op": "path_matches", "value": "/private/*"},
                    }
                ]
            },
        }
        return copy.deepcopy(self.state["sessions"][(self.user_id, sid)])

    def get_session(self, sid):
        return copy.deepcopy(self.state["sessions"][(self.user_id, sid)])

    def update_session(self, sid, *, policy):
        stored = self._policy(policy)
        self.state["sessions"][(self.user_id, sid)]["policy"] = stored
        self.state["sessions"][(self.user_id, sid)]["policy_revision"] += 1
        return self.get_session(sid)

    def _request(self, method, path, *, json):
        self.attempts.append({"method": method, "path": path, "status": 200})
        if path.endswith("reasoning"):
            self.relay.records.append(
                {"stage": "intention", "model": "openai/gpt-oss-120b", "usage": None}
            )
            return {}
        self.pending_seen |= bool(json.get("intention_pending"))
        return {
            "decision": "deny" if json["tool"] == "bash" else "approve",
            "call_id": "unit-audit-record",
            "evaluation_metadata": {
                "evaluation_policy_version": "hybrid_v1",
                "profile": "balanced",
            },
        }

    def get_audit_record(self, _call_id):
        return {
            "effective_decision": "approve",
            "evaluation_metadata": {"evaluation_policy_version": "hybrid_v1"},
        }


def test_compiler_cold_warm_ready_update_equivalence_accounting():
    with ProviderRecorder("unit-secret") as relay:
        api = FakeAPI(relay)
        result = compile_case(
            api,
            relay,
            {
                "id": "private-path",
                "source_container": "deny_policies",
                "english": "Deny private paths.",
                "expected_status": 200,
                "structured_reference": {
                    "id": "private",
                    "effect": "deny",
                    "when": {"op": "path_matches", "value": "/private/*"},
                },
                "probes": [{"tool": "read", "args": {"file_path": "/docs/design.md"}}],
            },
            "strict",
            "disabled",
        )
        assert result["conformant"]
        assert result["checks"]["warm_no_compile"]
        assert result["checks"]["ready_no_compile"]
        assert result["checks"]["tenant_cache_isolation"]
        assert (
            result["live_model_compilations"] == 2
        )  # mocked accounting, not real inference
        assert result["probes"][0]["equivalent"]


def test_rejected_activation_keeps_snapshot_and_stays_denominator():
    with ProviderRecorder("unit-secret") as relay:
        result = compile_case(
            FakeAPI(relay),
            relay,
            {
                "id": "reject",
                "source_container": "deny_policies",
                "english": "unsupported clause",
                "expected_status": 422,
                "structured_reference": None,
                "probes": [],
            },
            "balanced",
            "disabled",
        )
        assert result["checks"]["atomic_rejection"]
        assert result["conformant"]
        rows = [
            {
                **result,
                "cohort": "compiler",
                "backend": "jev",
                "judge": "disabled",
                "evidence_kind": "mock_api",
            }
        ]
        counted = summary(rows)["compiler/jev/disabled/balanced"]
        assert counted["planned"] == 1
        assert counted["live_result"] == 0


def test_gate_uses_existing_reasoning_and_barrier_not_controller_claim():
    with ProviderRecorder("unit-secret") as relay:
        api = FakeAPI(relay)
        result = gate_case(
            api,
            relay,
            {
                "id": "keep",
                "intention": "Read docs.",
                "message": "Keep reading.",
                "probe": {"tool": "read", "args": {"file_path": "/docs/design.md"}},
                "allowed_terminal": ["approve"],
            },
            "jev",
        )
        assert api.pending_seen
        assert result["generation_requests"] == 1
        assert (
            result["gate_requests"] == 0
        )  # this mock does not claim real gate invocation


def test_recorder_rejects_wrong_compiler_model_without_rewriting():
    with ProviderRecorder("unit-secret") as relay, httpx.Client() as client:
        response = client.post(
            relay.url + "/l1/chat/completions",
            headers={"Authorization": f"Bearer {relay.token}"},
            json={
                "model": "openai/gpt-oss-20b",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "policy_compilation"},
                },
            },
        )
        assert response.status_code == 400
        assert not relay.records  # no provider request was sent
        assert relay.validation_errors[0]["stage"] == "compiler"
        assert relay.validation_errors[0]["expected_model"] == "openai/gpt-oss-120b"


def test_real_client_serializes_policy_update():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={})

    with IntarisClient(
        "http://127.0.0.1:8061", "local-unit-key", "unit", "benchmark"
    ) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        p = {"evaluation_policy_version": "hybrid_v1", "profile": "strict", "rules": []}
        client.update_session("unit-policy-session", policy=p)
    assert seen[0][0] == "PATCH"
    assert seen[0][2] == {"policy": p}


def test_real_client_records_user_evidence_with_existing_hook_format():
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    with IntarisClient(
        "http://127.0.0.1:8061", "local-unit-key", "unit", "benchmark"
    ) as client:
        client._client.close()
        client._client = httpx.Client(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        client.submit_user_message("unit-restriction-session", "Do not publish.")
    assert seen == [
        (
            "/api/v1/reasoning",
            {
                "session_id": "unit-restriction-session",
                "content": "User message: Do not publish.",
            },
        )
    ]


def test_same_final_decision_wrong_stage_is_not_matrix_success():
    row = {
        "intaris": {"raw_decision": "approve", "evaluation_metadata": {"stages": []}},
        "terminal": {"state": "terminal", "decision": "approve", "audit": {}},
    }
    result = matrix_assessment(
        row,
        {
            "final": "approve",
            "raw_policy": "approve",
            "l1_batches": 1,
            "judge_reviews": 1,
        },
    )
    assert not result["conformant"]
    assert result["stage_checks"] == {
        "l1_batches": False,
        "judge_reviews": False,
        "raw_policy": True,
    }


def test_successful_gate_http_without_refreshed_intention_is_not_success():
    with ProviderRecorder("unit-secret") as relay:
        api = FakeAPI(relay)
        result = gate_case(
            api,
            relay,
            {
                "id": "refresh",
                "intention": "Read docs.",
                "message": "Now edit the assigned document.",
                "probe": {"tool": "read", "args": {"file_path": "/docs/design.md"}},
                "allowed_terminal": ["approve", "escalate"],
            },
            "disabled",
        )
        assert result["generation_requests"] == 1
        assert not result["intention_changed"]
        assert not result["assessment"]["conformant"]


def test_equal_terminal_decisions_cannot_hide_dropped_compiled_predicate():
    reference = {
        "effect": "deny",
        "when": {
            "op": "all",
            "children": [
                {"op": "eq", "field": "environment", "value": "production"},
                {"op": "contains", "field": "effects", "value": "write"},
            ],
        },
    }
    dropped = {
        "active_policy": {
            "rules": [
                {
                    "id": "compiled",
                    "effect": "deny",
                    "when": reference["when"]["children"][0],
                }
            ]
        }
    }
    assert not compilation_fidelity(dropped, reference)
    reordered = {
        "active_policy": {
            "rules": [
                {
                    "id": "compiled",
                    "effect": "deny",
                    "when": {
                        "op": "all",
                        "children": list(reversed(reference["when"]["children"])),
                    },
                }
            ]
        }
    }
    assert compilation_fidelity(reordered, reference)
