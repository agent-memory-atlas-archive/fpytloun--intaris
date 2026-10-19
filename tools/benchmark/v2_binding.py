"""Offline fixture binding to the core's canonical Evidence/table/finalizer.

No Evidence, source index, review receipt or human flag constructed here is
accepted by the live replay adapter. This module has no model/API client.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from tools.benchmark.matched import harness_sources, write_json
from tools.benchmark.v2_comparison import BOUNDARY_CONTRACT, run_table, verify


def declaration_rule(declaration: dict) -> dict:
    """Translate explicit fixture coverage into typed predicates, not decisions."""
    effect = declaration["effect"]
    if len(declaration["targets"]) != 1:
        raise ValueError(
            "Fixture union-target mapping needs owner-reviewed quantified union"
        )
    target_nodes = [
        {
            "op": "path_matches",
            "quantifier": "all" if effect == "allow" else "any",
            "value": target,
        }
        for target in declaration["targets"]
    ]
    target = (
        target_nodes[0]
        if len(target_nodes) == 1
        else {"op": "any", "children": target_nodes}
    )
    effects = declaration["effects"]
    if effect == "allow":
        # Require known effects to be a subset of the explicitly declared set.
        excluded = [
            {"op": "contains", "field": "effects", "value": item}
            for item in ("read", "write", "delete", "execute", "transmit", "unknown")
            if item not in effects
        ]
        effect_node = {"op": "not", "children": [{"op": "any", "children": excluded}]}
    else:
        nodes = [
            {"op": "contains", "field": "effects", "value": item} for item in effects
        ]
        effect_node = nodes[0] if len(nodes) == 1 else {"op": "any", "children": nodes}
    children = [target, effect_node]
    condition = declaration.get("condition")
    if condition:
        children.append(
            {"op": "eq", "field": condition["field"], "value": condition["equals"]}
        )
    return {
        "id": declaration["id"],
        "effect": effect,
        "when": {"op": "all", "children": children},
    }


def project_final_result(final: dict, rule_effects: dict[str, str]) -> dict:
    """Project canonical final metadata, without synthesizing missing obligations."""
    kinds = []
    for obligation in final["outstanding_obligations"]:
        identifier, basis = obligation["id"], obligation["basis"]
        if identifier.startswith("rule:") and rule_effects.get(basis) == "deny":
            kind = "applicable_prohibition"
        elif (
            identifier.startswith("rule:")
            and rule_effects.get(basis) == "require_approval"
        ):
            kind = "mandatory_human"
        else:
            kind = {
                "authority.review": "independent_review",
                "authority.strict_high": "declared_or_human_authority",
                "human.checkpoint": "mandatory_human",
                "request.minimum_outcome": "minimum_human",
                "mode.read_only": "plan_read_only",
                "targets.coverage": "material_target",
                "targets.applicability": "material_target",
            }.get(identifier, identifier)
        kinds.append(kind)
    return {
        "raw": final["raw_outcome"],
        "final": final["decision"],
        "obligation_kinds": sorted(set(kinds)),
        "permission_disposition": final["disposition"],
        "canonical_final_policy_result": copy.deepcopy(final),
        "finalization_override": final["override"],
    }


def bind_case(case: dict) -> dict:
    """Call actual core types/owners; retain discrepancies against frozen oracle."""
    from intaris.hybrid import Rule, evaluate_policy
    from intaris.hybrid_runtime import finalize_result
    from intaris.policy_assessment import receipt
    from intaris.policy_evidence import Evidence, Observation
    from intaris.read_only import resolve

    source = copy.deepcopy(case["input"])
    observations = {}
    for key, value in source["observations"].items():
        target = "paths" if key == "targets" else key
        observations[target] = Observation(
            value=value.get("values", value.get("value")),
            source=value["source"],
            coverage=value.get("coverage", "complete"),
            references=("action",),
        )
        if key == "targets":
            observations["paths_complete"] = Observation(
                value=value["coverage"] == "complete",
                source=value["source"],
                coverage="complete",
                references=("action",),
            )
    assessment = copy.deepcopy(source["assessments"])
    assessment["contextual_restrictions"] = assessment.pop(
        "context_compatibility", "unresolved"
    )
    assessment.setdefault(
        "consequence", "routine" if assessment.get("risk") == "low" else "consequential"
    )
    # The neutral fixture's within_scope claim projects to the finite comparison
    # fields; it remains a MODEL assessment, not an immutable observation.
    assessment.setdefault(
        "target_scope",
        "consistent"
        if assessment.get("authorization_scope") == "within_scope"
        else "unresolved",
    )
    assessment.setdefault("target_reference", "action")
    sources = {
        item["id"]: {
            "text": item["text"],
            "source": item["source"],
            "independent_grant": True,
        }
        for item in source.get("context_evidence", [])
    }
    if len(sources) == 1:
        reference = next(iter(sources))
        assessment.setdefault("authorization_source", reference)
        assessment.setdefault("restriction_reference", reference)
    overlay = resolve(source["context"])
    human = source.get("review") or {}
    human_valid = (
        human.get("source") == "human_record"
        and human.get("action_bound") is True
        and human.get("current_revision") is True
        and human.get("scope") == "whole_call"
        and human.get("targets") == source["observations"]["targets"]["values"]
        and set(human.get("effects", []))
        >= set(source["observations"]["effects"]["values"])
    )
    state = Evidence(
        observations=observations,
        assessment=assessment,
        sources=sources,
        human_approved=human_valid,
        read_only_required=overlay["read_only_required"],
    )
    rules = [
        Rule.model_validate(declaration_rule(item)) for item in source["declarations"]
    ]
    initial = evaluate_policy(rules, state, case["profile"], assessed=True)
    review = source.get("review") or {}
    if (
        review.get("source") == "engine_record"
        and review.get("action_bound")
        and review.get("current_revision")
    ):
        answers = {
            obligation.id: {
                "status": review.get("applicability", "unresolved"),
                "reference": (review.get("references") or ["action"])[0],
            }
            for obligation in initial.outstanding_obligations
            if obligation.requirement in review.get("obligations_addressed", [])
        }
        state = state.model_copy(update={"review": receipt(state, initial, answers)})
    result = evaluate_policy(rules, state, case["profile"], assessed=True)
    bounds = source["bounds"]
    final = finalize_result(
        result,
        maximum=bounds["maximum"],
        minimum=bounds["minimum"],
        human_approved=state.human_approved,
        overlay=overlay,
        mutation=state.value("mutation", "unclear"),
    )
    rule_effects = {item.id: item.effect for item in rules}
    return {
        **project_final_result(final.model_dump(mode="json"), rule_effects),
        "canonical_policy_result": result.model_dump(),
        "canonical_initial_result": initial.model_dump(),
        "canonical_evidence": state.model_dump(),
    }


bind_case.benchmark_contract = BOUNDARY_CONTRACT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    verify(manifest)
    # No intaris imports above; resolve exclusively to explicit core checkout.
    sys.path.insert(0, str(args.runtime.resolve()))
    result = run_table(manifest, bind_case)
    import intaris.hybrid
    import intaris.hybrid_runtime
    import intaris.policy_evidence

    owners = [intaris.hybrid, intaris.hybrid_runtime, intaris.policy_evidence]
    result["canonical_binding_confirmed"] = all(
        Path(module.__file__).resolve().is_relative_to(args.runtime.resolve())
        for module in owners
    )
    if not result["canonical_binding_confirmed"]:
        raise RuntimeError("Canonical imports escaped the explicit core checkout")
    result["binding_scope"] = "offline_controlled_internal_frames_only"
    result["binding_module_sha256"] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    result["harness_sources"] = harness_sources()
    result["core_revision"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=args.runtime, text=True
    ).strip()
    result["core_dirty"] = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=args.runtime, text=True
        ).strip()
    )
    result["canonical_sources"] = {
        name: hashlib.sha256((args.runtime / "intaris" / name).read_bytes()).hexdigest()
        for name in (
            "policy_evidence.py",
            "policy_assessment.py",
            "hybrid.py",
            "hybrid_runtime.py",
            "read_only.py",
        )
    }
    result["decision_passes"] = sum(
        row["checks"]["raw"] and row["checks"]["final"] for row in result["results"]
    )
    result["trace_passes"] = sum(
        all(
            passed
            for key, passed in row["checks"].items()
            if key not in {"raw", "final"}
        )
        for row in result["results"]
    )
    expectations = {case["id"]: case["expected"] for case in manifest["table_cases"]}
    result["counterexamples"] = [
        {
            "case": row["case"],
            "checks": row["checks"],
            "expected": expectations[row["case"]],
            "actual": row["actual"],
        }
        for row in result["results"]
        if not row["passed"]
    ]
    write_json(args.output, result)
    print(
        f"Offline canonical table: {sum(row['passed'] for row in result['results'])}/{len(result['results'])}; no model calls"
    )


if __name__ == "__main__":
    main()
