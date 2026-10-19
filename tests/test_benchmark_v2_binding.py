"""Boundary/mapping wiring only; no claim of model or policy acceptance."""

import json
from argparse import Namespace
from hashlib import sha1
from pathlib import Path

import pytest

from tools.benchmark.matched import write_json
from tools.benchmark.v2_binding import declaration_rule, project_final_result
from tools.benchmark.v2_comparison import freeze
from tools.benchmark.v2_pairs import run, scenario


def test_declaration_translation_preserves_complete_allow_and_any_deny():
    value = {
        "id": "scoped-read",
        "effect": "allow",
        "targets": ["/docs/*"],
        "effects": ["read"],
    }
    mapped = declaration_rule(value)
    assert mapped["id"] == value["id"]
    assert mapped["when"]["children"][0] == {
        "op": "path_matches",
        "quantifier": "all",
        "value": "/docs/*",
    }
    excluded = mapped["when"]["children"][1]["children"][0]["children"]
    assert {node["value"] for node in excluded} == {
        "write",
        "delete",
        "execute",
        "transmit",
        "unknown",
    }
    value["effect"] = "deny"
    assert declaration_rule(value)["when"]["children"][0]["quantifier"] == "any"
    assert declaration_rule(value)["when"]["children"][1] == {
        "op": "contains",
        "field": "effects",
        "value": "read",
    }


def final_frame(**changes):
    return {
        "raw_outcome": "approve",
        "decision": "approve",
        "override": None,
        "outstanding_obligations": [],
        "blocking_reasons": [],
        "discharged_obligation_ids": [],
        "waived_obligation_ids": [],
        "disposition": "satisfied",
        **changes,
    }


def test_known_plan_block_is_not_fabricated_as_unresolved_obligation():
    frame = final_frame(
        raw_outcome="deny",
        decision="deny",
        override="request.read_only_required",
        blocking_reasons=["request.read_only_required"],
        disposition="blocked",
    )
    projected = project_final_result(frame, {})
    assert projected["obligation_kinds"] == []
    assert projected["canonical_final_policy_result"] == frame
    assert projected["permission_disposition"] == "blocked"


def test_post_final_minimum_floor_and_human_discharge_preserved():
    obligation = {
        "id": "request.minimum_outcome",
        "basis": "request.minimum_outcome",
        "requirement": "human",
        "protected": True,
        "fields": [],
        "evidence": [],
    }
    frame = final_frame(
        decision="escalate",
        override="request.minimum_outcome",
        outstanding_obligations=[obligation],
        disposition="outstanding",
    )
    assert project_final_result(frame, {})["obligation_kinds"] == ["minimum_human"]
    discharged = final_frame(
        override="human_approval",
        disposition="satisfied_by_human",
        discharged_obligation_ids=["request.minimum_outcome"],
    )
    projection = project_final_result(discharged, {})
    assert projection["obligation_kinds"] == []
    assert projection["permission_disposition"] == "satisfied_by_human"
    assert projection["canonical_final_policy_result"] == discharged


def test_operator_waiver_never_projects_to_satisfied_and_keeps_extras():
    obligations = [
        {"id": "authority.review", "basis": "context", "requirement": "review"},
        {
            "id": "authority.target_coverage",
            "basis": "targets",
            "requirement": "coverage",
        },
    ]
    frame = final_frame(
        raw_outcome="escalate",
        override="waived_by_operator_bound",
        disposition="waived_by_operator_bound",
        outstanding_obligations=obligations,
        waived_obligation_ids=["authority.review", "authority.target_coverage"],
    )
    projected = project_final_result(frame, {})
    assert projected["permission_disposition"] == "waived_by_operator_bound"
    assert projected["obligation_kinds"] == [
        "authority.target_coverage",
        "independent_review",
    ]
    assert projected["canonical_final_policy_result"] == frame


def test_union_target_mapping_does_not_silently_change_coverage():
    with pytest.raises(ValueError, match="union-target"):
        declaration_rule(
            {
                "id": "two-scopes",
                "effect": "allow",
                "targets": ["/docs/*", "/tests/*"],
                "effects": ["read"],
            }
        )


def fixture(tmp_path):
    boundary = tmp_path / "boundary.md"
    boundary.write_text(
        "Fixture test of frozen comparison mapping; not a core policy oracle"
    )
    value = freeze(boundary)
    path = tmp_path / "manifest.json"
    write_json(path, value)
    return value, path


def test_live_pair_requests_never_carry_internal_evidence(tmp_path):
    value, _ = fixture(tmp_path)
    for pair in value["pairs"]:
        requests = []
        for variant in pair["variants"]:
            mapped = scenario(pair, variant)
            assert set(mapped) == {
                "id",
                "cohort",
                "split",
                "intention",
                "details",
                "policy",
                "steps",
            }
            step = mapped["steps"][0]
            assert set(step) == {"id", "label", "tool", "args", "context"}
            assert step["label"] is None
            assert mapped["policy"] == variant["policy"]
            assert set(mapped["policy"]) == {
                "evaluation_policy_version",
                "profile",
                "rules",
                "intention_gate",
            }
            requests.append({key: step[key] for key in ("tool", "args", "context")})
        assert requests[0] == requests[1] == pair["request"]


def test_live_pair_gate_before_credentials_or_runtime(tmp_path):
    value, manifest = fixture(tmp_path)
    args = Namespace(
        manifest=manifest,
        ready_runtime_revision=None,
        # Only syntax/equality is exercised here, not a real checkout.
        revision=sha1(
            b"synthetic benchmark gate fixture", usedforsecurity=False
        ).hexdigest(),
    )
    with pytest.raises(ValueError, match="reviewed runtime"):
        run(args)
    args.ready_runtime_revision = args.revision
    args.wire_approved_manifest_hash = value["boundary_sha256"]
    with pytest.raises(ValueError, match="structured wire manifest"):
        run(args)


def test_no_canonical_types_imported_by_live_pair_module():
    import tools.benchmark.v2_pairs as module

    text = Path(module.__file__).read_text()
    assert "v2_binding" not in text
    assert "human_approved" not in text
    assert "Review(" not in text
    assert "independent_grant" not in text


@pytest.mark.parametrize("stop", [None, "compiler", "budget"])
def test_full_pair_recording_and_stop_preserves_row(tmp_path, monkeypatch, stop):
    """Mock boundary integration; no runtime/model invocation."""
    import tools.benchmark.v2_pairs as module
    from tools.benchmark.matched import digest

    value, manifest = fixture(tmp_path)
    value["pairs"] = value["pairs"][:1]
    value["trials"] = 1
    value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
    write_json(manifest, value)
    closed = []

    class Recorder:
        def __init__(self, *args, **kwargs):
            assert kwargs["forbid_compiler"]
            self.records, self.validation_errors, self.context = [], [], {}
            self.stopped = stop == "budget"
            if stop == "compiler":
                self.validation_errors.append(
                    {
                        "stage": "compiler",
                        "code": "compiler_forbidden_in_comparison_lane",
                    }
                )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def drain(self):
            pass

    class LocalRuntime:
        url, api_key = "http://127.0.0.1:8061", "unit-local-key"

        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            closed.append(True)

    class LocalClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def replay(*args, **kwargs):
        return [
            {
                "intaris": {
                    "decision": "approve",
                    "evaluation_metadata": {
                        "evaluation_policy_version": "hybrid_v1",
                        "facts": {"risk": "low"},
                        "stages": [],
                    },
                },
                "terminal": {"state": "terminal", "decision": "approve", "audit": {}},
                "provider_attempts": [],
                "http_attempts": [],
                "end_to_end_ms": 1,
            }
        ]

    monkeypatch.setattr(module, "ProviderRecorder", Recorder)
    monkeypatch.setattr(module, "Runtime", LocalRuntime)
    monkeypatch.setattr(module, "IntarisClient", LocalClient)
    monkeypatch.setattr(module, "replay_scenario", replay)
    monkeypatch.setattr(
        module, "secret_file", lambda path: "unit-not-provider-credential"
    )
    fixture_revision = sha1(
        b"synthetic replay runner fixture", usedforsecurity=False
    ).hexdigest()
    args = Namespace(
        manifest=manifest,
        runtime=tmp_path,
        revision=fixture_revision,
        ready_runtime_revision=fixture_revision,
        wire_approved_manifest_hash=value["sha256"],
        output=tmp_path / "run",
        groq_key=tmp_path / "unit-groq-key",
        jev_key=tmp_path / "unit-jev-key",
        arms=["llm"],
        judge="disabled",
    )
    if stop:
        with pytest.raises((AssertionError, RuntimeError)):
            run(args)
    else:
        run(args)
    rows = [
        json.loads(line)
        for line in (args.output / "results.jsonl").read_text().splitlines()
    ]
    info = json.loads((args.output / "run.json").read_text())
    assert len(rows) == (1 if stop else 2)
    assert info["recorded_rows"] == len(rows)
    assert info["missing_rows"] == 2 - len(rows)
    assert info["completed"] is (stop is None)
    assert all(row["assessment"]["conformant"] for row in rows)
    assert closed == [True]
