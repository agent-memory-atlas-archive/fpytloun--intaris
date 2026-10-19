"""Trace revision invariants; optional canonical integration has no model calls."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.benchmark.matched import digest
from tools.benchmark.v2_comparison import run_table
from tools.benchmark.v2_trace_contract import assert_unchanged_semantics, revise

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "tools/benchmark/fixtures/v2-comparison-v1.json"
)


def original():
    return json.loads(SOURCE.read_text())


def test_revision_preserves_all_inputs_profiles_raw_final_and_pairs():
    before = original()
    after = revise(before)
    assert before == original()
    assert_unchanged_semantics(before, after)
    assert len(after["trace_deltas"]) == 18
    assert after["pairs"] == before["pairs"]
    assert after["structured_wire_freeze"]["pairs_sha256"] == digest(before["pairs"])
    assert after["live_ready"] is False
    assert after["trace_contract"] == "main_adjudicated_finalization_trace_v2"
    assert json.loads(SOURCE.with_name("v2-comparison-v2.json").read_text()) == after


@pytest.mark.parametrize("change", ["input", "profile", "raw", "final", "pair"])
def test_nontrace_changes_rejected_even_with_recomputed_manifest_hash(change):
    before = original()
    after = revise(before)
    case = after["table_cases"][0]
    if change == "input":
        case["input"]["context"] = {"chat_mode": "build"}
    elif change == "profile":
        case["profile"] = "strict"
    elif change in {"raw", "final"}:
        case["expected"][change] = ["deny"]
    else:
        after["pairs"][0]["intention"] = "Different task"
    after["sha256"] = digest(
        {key: value for key, value in after.items() if key != "sha256"}
    )
    with pytest.raises(ValueError):
        assert_unchanged_semantics(before, after)


def test_exact_obligations_and_canonical_guards_cannot_be_omitted():
    value = revise(original())

    def scripted(case):
        expected = case["expected"]
        return {
            "raw": expected["raw"][0],
            "final": expected["final"][0],
            "obligation_kinds": expected["obligation_kinds"],
            "permission_disposition": expected["permission_disposition"],
            # Deliberately missing full canonical frames; cannot pass trace guards.
        }

    rows = run_table(value, scripted)["results"]
    revised = {item["case"] for item in value["trace_deltas"]}
    assert all(not row["passed"] for row in rows if row["case"] in revised)
    assert all(row["checks"]["raw"] and row["checks"]["final"] for row in rows)


def test_no_global_material_target_authority_synonym():
    after = revise(original())
    cases = {case["property"]: case for case in after["table_cases"]}
    assert cases["plan_unresolved_mutation"]["expected"]["obligation_kinds"] == [
        "plan_read_only",
        "authority.applicability",
        "material_target",
    ]
    assert cases["incomplete_targets_no_bad_witness"]["expected"]["canonical_trace"][
        "canonical_policy_result.unresolved_rules"
    ] == ["declared_release"]


def test_canonical_v1_and_v2_offline_decision_trace_split(tmp_path):
    runtime = os.getenv("INTARIS_V2_CORE_TEST_PATH")
    if not runtime:
        pytest.skip("Explicit reviewed core path required for canonical integration")
    before = original()
    manifest = SOURCE.with_name("v2-comparison-v2.json")
    assert json.loads(manifest.read_text()) == revise(before)
    results = []
    for source, name in ((SOURCE, "v1"), (manifest, "v2")):
        output = tmp_path / (name + "-result.json")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.benchmark.v2_binding",
                "--runtime",
                runtime,
                "--manifest",
                str(source),
                "--output",
                str(output),
            ],
            check=True,
            cwd=SOURCE.parents[3],
            capture_output=True,
            text=True,
        )
        results.append(json.loads(output.read_text()))
    assert results[0]["decision_passes"] == results[1]["decision_passes"] == 78
    assert results[0]["trace_passes"] == 60
    assert results[1]["trace_passes"] == 78
    for old, new in zip(results[0]["results"], results[1]["results"], strict=True):
        assert old["actual"] == new["actual"]
    assert before == original()
