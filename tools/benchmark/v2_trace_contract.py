"""Main-approved trace taxonomy revision; no policy/outcome implementation."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from tools.benchmark.matched import digest, write_json
from tools.benchmark.v2_comparison import verify


def revise(original: dict) -> dict:
    verify(original)
    if original["version"] != 1:
        raise ValueError("Trace v2 must derive from preserved v1")
    value = copy.deepcopy(original)
    del value["sha256"]
    value["version"] = 2
    value["trace_contract"] = "main_adjudicated_finalization_trace_v2"
    value["parent_manifest_sha256"] = original["sha256"]
    value["trace_adjudication"] = {
        "authority": "main explicit six-family adjudication",
        "scope": "trace expectations only, no decision/input/profile/policy changes",
        "obligation_matching": "exact named set; no subset or global synonyms",
    }
    deltas = []
    for case in value["table_cases"]:
        expected, family = case["expected"], case["property"]
        old = copy.deepcopy(expected)
        guards = {}
        if family in {"incomplete_targets_no_bad_witness", "unknown_targets"}:
            authority = (
                "declared_or_human_authority"
                if case["profile"] == "strict"
                else "independent_review"
            )
            expected["obligation_kinds"] = ["authority.applicability", authority]
            coverage = (
                "partial"
                if family == "incomplete_targets_no_bad_witness"
                else "unknown"
            )
            guards = {
                "canonical_policy_result.unresolved_rules": ["declared_release"],
                "canonical_policy_result.matched_rules": [],
                "canonical_policy_result.permission_basis": None,
                "canonical_policy_result.constraint_matches.0.rule_id": "declared_release",
                "canonical_policy_result.constraint_matches.0.effect": "allow",
                "canonical_policy_result.constraint_matches.0.applicability": 2,
                "canonical_evidence.observations.paths.coverage": coverage,
                "canonical_evidence.observations.effects.coverage": coverage,
                "canonical_evidence.observations.paths.value": case["input"][
                    "observations"
                ]["targets"]["values"],
                "canonical_evidence.observations.effects.value": case["input"][
                    "observations"
                ]["effects"]["values"],
                "canonical_evidence.observations.paths_complete.value": False,
                "canonical_evidence.sources": {},
            }
            rationale = "Typed grant stays unproved; source-less semantic applicability/review are separate obligations."
        elif family == "plan_mutation_maximum_approve":
            expected["obligation_kinds"] = []
            guards = {
                "canonical_final_policy_result.blocking_reasons": [
                    "request.read_only_required"
                ],
                "canonical_final_policy_result.outstanding_obligations": [],
                "canonical_final_policy_result.waived_obligation_ids": [],
                "canonical_evidence.read_only_required": True,
            }
            rationale = "Known plan mutation is a conclusive blocker, not unresolved assessment."
        elif family == "plan_unresolved_mutation":
            expected["obligation_kinds"] = [
                "plan_read_only",
                "authority.applicability",
                "material_target",
            ]
            guards = {
                "canonical_evidence.read_only_required": True,
                "canonical_final_policy_result.outstanding_obligations.0.id": "mode.read_only",
                "canonical_final_policy_result.outstanding_obligations.0.protected": True,
                "canonical_final_policy_result.outstanding_obligations.0.requirement": "assessment",
                "canonical_final_policy_result.waived_obligation_ids": [],
            }
            rationale = "Retain exact three unresolved obligations and protected plan assessment."
        elif family == "high_impact_scoped_human_authority":
            expected["permission_disposition"] = "satisfied_by_human"
            guards = {
                "canonical_evidence.human_approved": True,
                "canonical_final_policy_result.override": "human_approval",
                "canonical_final_policy_result.outstanding_obligations": [],
                "canonical_evidence.sources.raw_release_request.source": "client_user_instruction",
            }
            rationale = (
                "Human-bound satisfaction is distinct from ordinary satisfaction."
            )
        elif family == "minimum_human_not_satisfied_by_judge":
            expected["obligation_kinds"] = ["minimum_human"]
            guards = {
                "canonical_final_policy_result.outstanding_obligations.0.id": "request.minimum_outcome",
                "canonical_final_policy_result.outstanding_obligations.0.basis": "request.minimum_outcome",
                "canonical_final_policy_result.outstanding_obligations.0.requirement": "human",
                "canonical_final_policy_result.outstanding_obligations.0.protected": True,
                "canonical_final_policy_result.waived_obligation_ids": [],
            }
            rationale = "Protected request floor retains its source; not a custom rule human checkpoint."
        else:
            continue
        expected["canonical_trace"] = guards
        deltas.append(
            {
                "case": case["id"],
                "old": old,
                "new": copy.deepcopy(expected),
                "rationale": rationale,
            }
        )
    value["trace_deltas"] = deltas
    value["structured_wire_freeze"] = {
        "pairs_sha256": digest(value["pairs"]),
        "pairs": 21,
        "mapping": "unchanged public typed rules; no internal evidence/review/source flags",
        "status": "frozen for main integrated-runtime approval; no live authorization",
    }
    value["sha256"] = digest(value)
    assert_unchanged_semantics(original, value)
    return value


def assert_unchanged_semantics(original: dict, revision: dict) -> None:
    """Only approved trace keys may change; guard every original case field."""
    verify(original)
    verify(revision)
    if revision["parent_manifest_sha256"] != original["sha256"]:
        raise ValueError("Trace revision parent mismatch")
    for key, value in original.items():
        if (
            key not in {"sha256", "version", "table_cases"}
            and revision.get(key) != value
        ):
            raise ValueError(f"Non-trace manifest field changed: {key}")
    if len(original["table_cases"]) != len(revision["table_cases"]):
        raise ValueError("Case inventory changed")
    for before, after in zip(
        original["table_cases"], revision["table_cases"], strict=True
    ):
        for key, val in before.items():
            if key != "expected" and after.get(key) != val:
                raise ValueError(f"Case input/profile changed: {before['id']}")
        if set(before) != set(after):
            raise ValueError("Case shape changed")
        for key in ("raw", "final"):
            if before["expected"][key] != after["expected"][key]:
                raise ValueError(f"Decision expectation changed: {before['id']}")
        if set(after["expected"]) - set(before["expected"]) - {"canonical_trace"}:
            raise ValueError("Unapproved expectation key")
        if set(before["expected"]) - set(after["expected"]):
            raise ValueError("Original expectation removed")
        for key, val in before["expected"].items():
            if (
                key not in {"obligation_kinds", "permission_disposition"}
                and after["expected"][key] != val
            ):
                raise ValueError("Non-trace expectation changed")
    deltas = {item["case"]: item for item in revision["trace_deltas"]}
    if len(deltas) != 18 or len(revision["trace_deltas"]) != 18:
        raise ValueError("Approved trace inventory changed")
    for before, after in zip(
        original["table_cases"], revision["table_cases"], strict=True
    ):
        if before["id"] in deltas:
            delta = deltas[before["id"]]
            if delta["old"] != before["expected"] or delta["new"] != after["expected"]:
                raise ValueError("Trace delta audit mismatch")
        elif before["expected"] != after["expected"]:
            raise ValueError("Unadjudicated case trace changed")


def trace_checks(actual: dict, expected: dict) -> dict[str, bool]:
    checks = {}
    for path, reference in expected.items():
        value = actual
        try:
            for part in path.split("."):
                value = value[int(part)] if isinstance(value, list) else value[part]
            checks[path] = value == reference
        except (KeyError, IndexError, TypeError, ValueError):
            checks[path] = False
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve() or args.output.exists():
        raise ValueError("Preserve original and previous manifest revisions")
    write_json(args.output, revise(json.loads(args.source.read_text())))


if __name__ == "__main__":
    main()
