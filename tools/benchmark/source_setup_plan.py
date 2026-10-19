"""Historical controls are additional setup, never primary quality rows."""

import json

from tools.benchmark.matched import digest
from tools.benchmark.source_context import FIXTURE


def focused_setup_plan() -> dict:
    cases = json.loads(FIXTURE.read_text())["cases"]
    controls = sum(
        event.get("mechanism") == "public_human_resolution"
        for case in cases
        for event in case["events"]
    )
    copies = 3 * 2 * 2 * 2  # trials, backends, Judge modes, context arms
    calls = controls * copies
    value = {
        "version": 1,
        "fixture_control_sources": controls,
        "historical_setup_evaluations": calls,
        "primary_quality_evaluations": 1800,
        "history_checkpoint_observations": 288,
        "setup_quality_role": "historical_setup_control",
        "setup_requires_actual_user_resolved_audit": True,
        "normal_extra_stage_envelope": {
            "classification": calls,
            "judge_if_entered": calls,
            "treatment_extraction_if_rebuilt": calls // 2,
        },
        "normal_extra_attempt_envelope": calls * 2 + calls // 2,
        "retries_and_actual_regeneration": "Count all observed attempts in shared6000/$10 ledger",
        "scope_expansion_or_budget_reset": False,
        "seeded_probe_or_extraction_verdicts": False,
    }
    value["sha256"] = digest(value)
    return value
