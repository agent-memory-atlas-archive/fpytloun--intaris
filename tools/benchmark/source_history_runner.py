"""Recorded-state evolving-history adapter. Never execute fixture tool payloads.

Fixture oracles are consumed only after runtime snapshots return. Context-only
captures are explicitly component evidence unless a public snapshot hook exists.
No intention authored by this benchmark replaces generated session intention.
"""

from __future__ import annotations

import copy
import json
import random
import secrets
from pathlib import Path
from typing import Protocol

from tools.benchmark.matched import MODEL_SETTINGS, digest, write_json
from tools.benchmark.source_context import verify as verify_primary
from tools.benchmark.source_history_manifest import (
    MANIFEST,
    checkpoint_contract,
    history_prefix,
    load_manifest,
    manifest_sha256,
)
from tools.benchmark.terminal import read_terminal


class Unestablished(RuntimeError):
    """The existing public/runtime route cannot establish required evidence."""


def plan(primary: dict, history_path: Path = MANIFEST) -> dict:
    """Freeze a separate side-stage plan without editing primary1800 inputs."""
    verify_primary(primary)
    data = load_manifest(history_path)
    probes = sum("probe" in c for s in data["stories"] for c in s["checkpoints"])
    users = sum(
        data["actors"][e["actor"]] == "client_attributed_user"
        for s in data["stories"]
        for e in s["events"]
    )
    decisions = sum(
        e["kind"] == "decision" for s in data["stories"] for e in s["events"]
    )
    # One dedicated LLM/advisory comparison per story/seed/mode. Classification
    # is not duplicated across Jev/disabled axes; primary retains those axes.
    copies = 3 * 2
    value = {
        "version": "source_history_execution_v1",
        "primary_manifest_sha256": primary["sha256"],
        "history_bytes_sha256": manifest_sha256(history_path),
        "models": copy.deepcopy(MODEL_SETTINGS),
        "engine_axes": {"backend": "llm", "judge": "advisory"},
        "arms": ["mixed", "source_context"],
        "trials": 3,
        "seed": primary["seed"],
        "unique_checkpoints": 48,
        "planned_checkpoint_observations": 48 * copies,
        "planned_selected_probes": probes * copies,
        "maximum_historical_decision_evaluations": decisions * copies,
        "user_recordings_may_generate_intention": users * copies,
        "maximum_cold_source_extractions": 48 * copies,
        "warm_snapshot_reads": 48 * copies,
        "incremental_attempt_estimate_no_retries": (
            users * copies + 48 * copies + (probes + decisions) * copies * 2
        ),
        "primary_no_retry_stage_envelope": 1800 * 2 + 45 * 3 * 2 * 2,
        "combined_no_retry_stage_envelope": (
            1800 * 2
            + 45 * 3 * 2 * 2
            + users * copies
            + 48 * copies
            + (probes + decisions) * copies * 2
        ),
        "budget_expansion_needed_for_full_envelope": True,
        "before_inference_budget_request": (
            "Approve >=4746 no-retry stage envelope plus bounded retry headroom, "
            "or retain4000 with explicit possible incomplete matrix; never omit primary1800"
        ),
        "primary_evaluations_preserved": 1800,
        "initial_cumulative_attempt_cap": 4000,
        "initial_known_price_cap_usd": 10,
        "unknown_primary_extraction_judge_retry_count": True,
        "no_guaranteed_full_matrix_under_cap": True,
        "expansion_rule": "Main approves revised ceiling before inference if forecast exceeds4000",
        "invariant_plans": {
            item["id"]: "requires_separate_runtime_fixture_evidence_not_model_proof"
            for item in data["invariant_plans"]
        },
        "live_ready": False,
    }
    value["sha256"] = digest(value)
    return value


def verify_plan(value: dict) -> None:
    if value["sha256"] != digest({k: v for k, v in value.items() if k != "sha256"}):
        raise ValueError("History execution plan changed")
    if (
        value["models"] != MODEL_SETTINGS
        or value["history_bytes_sha256"] != manifest_sha256()
    ):
        raise ValueError("History inputs/models changed")
    if value["trials"] != 3 or value["arms"] != ["mixed", "source_context"]:
        raise ValueError("Frozen history axes changed")
    if value["engine_axes"] != {"backend": "llm", "judge": "advisory"}:
        raise ValueError("History engine axes changed")


class SnapshotHooks(Protocol):
    """Core-owned generation/capture hooks, never given checkpoint expectations."""

    def capture(self, *, client, session_id: str) -> dict: ...

    def observe_judge_resolution(
        self, *, client, session_id: str, action: dict
    ) -> dict: ...

    def close(self) -> None: ...


class PublicHistoryLifecycle:
    """Append actual typed events and user reasoning; verify actual audit linkage."""

    def __init__(self, client, session_id: str, hooks: SnapshotHooks) -> None:
        self.client, self.session_id, self.hooks = client, session_id, hooks
        self.records: dict[str, dict] = {}
        self.omissions: list[dict] = []

    def record(self, event: dict, *, origin: str, action: dict | None = None) -> dict:
        event = copy.deepcopy(event)
        if event["kind"] == "decision":
            return self._decision(event, action)
        role = {
            "client_attributed_user": "user",
            "agent": "assistant",
            "tool": "tool",
        }.get(origin)
        if role is None:
            raise Unestablished("Unsupported public event origin")
        payload = {"type": "message", "data": {"role": role, "text": event["text"]}}
        response = self.client.append_events(self.session_id, [payload])
        seq = response.get("first_seq")
        if (
            type(seq) is not int
            or seq <= 0
            or response.get("count") != 1
            or response.get("last_seq") != seq
        ):
            raise Unestablished("Event store returned no exact durable reference")
        self.client.flush_recorded_events(self.session_id)
        captured = self.client.read_recorded_events(self.session_id, seq)
        events = captured.get("events", [])
        actual = next((x for x in events if x.get("seq") == seq), None)
        if (
            not actual
            or actual.get("data") != payload["data"]
            or actual.get("type") != "message"
        ):
            raise Unestablished("Public event readback differs or is unavailable")
        record = {
            "event_seq": seq,
            "source_ref": f"event:{seq}",
            "role": role,
            "text": event["text"],
            "public_record": copy.deepcopy(actual),
        }
        self.records[event["id"]] = record
        if role == "user":
            antecedent = self.records.get(event.get("reply_to"))
            context = None
            if antecedent and antecedent["role"] == "assistant":
                context = antecedent[
                    "text"
                ]  # actual readback, never current probe args
            elif event.get("reply_to"):
                self.omissions.append(
                    {"event": event["id"], "kind": "unavailable_reference_context"}
                )
            if event.get("reference_context_status") == "unavailable":
                self.omissions.append(
                    {"event": event["id"], "kind": "unavailable_reference_context"}
                )
            response = self.client.submit_reasoning(
                self.session_id,
                "User message: " + event["text"],
                context=context,
                from_events=True,
            )
            call_id = response.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise Unestablished("User reasoning returned no real audit reference")
            audit = self.client.get_audit_record(call_id)
            if (
                audit.get("record_type") != "reasoning"
                or audit.get("session_id") != self.session_id
                or audit.get("content") != "User message: " + event["text"]
                or (
                    context is not None
                    and (audit.get("args_redacted") or {}).get("context") != context
                )
                or (audit.get("args_redacted") or {}).get("source_event_seq") != seq
            ):
                raise Unestablished("Recorded user/context linkage not preserved")
            record["reasoning_call_id"] = call_id
            record["reasoning_ref"] = f"reasoning:{call_id}"
            record["reference_context"] = copy.deepcopy(antecedent) if context else None
        return copy.deepcopy(record)

    def _decision(self, event: dict, action: dict | None) -> dict:
        decision = event["decision"]
        if not action:
            raise Unestablished("Historical action binding unavailable")
        if decision["resolver_origin"] == "judge":
            # This method must observe an actual server/Judge result; it must
            # never call POST /decision pretending to be that resolver.
            audit = self.hooks.observe_judge_resolution(
                client=self.client,
                session_id=self.session_id,
                action=copy.deepcopy(action),
            )
            if (
                audit.get("resolved_by") != "judge"
                or audit.get("user_decision") != decision["resolution"]
            ):
                raise Unestablished("Actual Judge resolution not observed")
        elif decision["resolver_origin"] == "human":
            response = self.client.evaluate(
                self.session_id, action["tool"], action["args"]
            )
            call_id = response.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise Unestablished(
                    "Historical action produced no actual call reference"
                )
            self.client.resolve_escalation(
                call_id, decision["resolution"], note=event["text"]
            )
            audit = self.client.get_audit_record(call_id)
            if (
                audit.get("resolved_by") != "user"
                or audit.get("user_decision") != decision["resolution"]
            ):
                raise Unestablished(
                    "Human resolution not stored with actual user origin"
                )
        else:
            raise Unestablished("Unsupported resolver")
        call_id = audit.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise Unestablished("Resolver audit has no actual identity")
        if (
            audit.get("session_id") != self.session_id
            or audit.get("tool") != action["tool"]
            or not historical_arguments_match(
                audit.get("args_redacted"),
                action["args"],
                legacy=getattr(self.client, "mode", None) == "legacy",
            )
        ):
            raise Unestablished("Recorded historical decision scope differs")
        record = {
            "source_ref": f"decision:{call_id}",
            "call_id": call_id,
            "role": decision["resolver_origin"],
            "text": event["text"],
            "public_record": copy.deepcopy(audit),
        }
        self.records[event["id"]] = record
        return copy.deepcopy(record)


def historical_arguments_match(actual, expected, *, legacy=False):
    """Exclude only the duplicated legacy server envelope, preserving raw audit."""
    if not legacy or not isinstance(actual, dict) or not isinstance(expected, dict):
        return actual == expected
    if {"context", "__intaris_context"} & expected.keys():
        return actual == expected
    envelope = actual.get("__intaris_context")
    if isinstance(envelope, dict) and actual.get("context") == envelope:
        return {
            key: value
            for key, value in actual.items()
            if key not in {"context", "__intaris_context"}
        } == expected
    return actual == expected


def semantic_checks(contract: dict, snapshot: dict, records: dict) -> dict:
    """Reference AND semantic checks; unresolved translation is not a pass.

    Semantic assertions are hook telemetry interpreted after capture, not
    fed back as a model prompt, evaluator facts, or an external jury.
    """
    artifact = snapshot.get("artifact") or {}
    actual_items = artifact.get("semantic_items")
    if (
        artifact.get("status") != "ready"
        or not isinstance(actual_items, list)
        or snapshot.get("semantic_projection_origin")
        not in {"canonical_core_export", "human_adjudication"}
    ):
        return {
            "state": "UNESTABLISHED",
            "reason": "canonical_semantic_projection_unavailable",
            "quote_validity_proves_semantics": False,
        }
    source_records = {
        reference: record
        for record in records.values()
        for reference in (record.get("source_ref"), record.get("reasoning_ref"))
        if reference
    }
    contamination, untracked = [], []
    for actual in actual_items:
        for reference in actual.get("source_refs", []):
            record = source_records.get(reference)
            if not record:
                untracked.append(reference)
            elif record["role"] not in {"user", "human"}:
                contamination.append(reference)
    checks = []
    matched_items = set()
    for expected in contract["items"]:
        refs = [records.get(source) for source in expected["sources"]]
        candidate = []
        for index, actual in enumerate(actual_items):
            if actual.get("source_refs") != [r.get("source_ref") for r in refs if r]:
                continue
            quote_ok = actual.get("quotes") == expected["quotes"] and all(
                record and quote in record["text"]
                for record, quote in zip(refs, expected["quotes"], strict=True)
            )
            semantic_ok = (
                actual.get("kind") == expected["kind"]
                and actual.get("scope") == expected["scope"]
                and actual.get("polarity") == expected["polarity"]
                and actual.get("status") == expected["status"]
            )
            reference_ids = expected.get("reference_context", [])
            reference_ok = actual.get("reference_context_refs", []) == [
                records[s]["source_ref"] for s in reference_ids if s in records
            ] and all(s in records for s in reference_ids)
            matched = quote_ok and semantic_ok and reference_ok
            candidate.append(matched)
            if matched:
                matched_items.add(index)
        checks.append(
            {
                "item": expected["id"],
                "established": bool(candidate),
                "passed": any(candidate),
            }
        )
    # Interpretations beyond source-linked fixture assertions require explicit
    # owner/human adjudication, not fuzzy quotation matching.
    unknown_ok = snapshot.get("semantic_uncertainties") == contract["unknown"]
    hierarchy_ok = snapshot.get("request_hierarchy_verified") is True
    unmatched = [
        index for index in range(len(actual_items)) if index not in matched_items
    ]
    passed = (
        all(c["passed"] for c in checks)
        and unknown_ok
        and hierarchy_ok
        and not contamination
        and not untracked
        and not unmatched
    )
    return {
        "state": "UNESTABLISHED"
        if untracked and not contamination
        else "PASS"
        if passed
        else "FAIL",
        "items": checks,
        "uncertainty_matches": unknown_ok,
        "request_hierarchy_verified": hierarchy_ok,
        "quote_validity_proves_semantics": False,
        "inadmissible_semantic_sources": contamination,
        "untracked_semantic_sources": untracked,
        "unmatched_semantic_items": unmatched,
    }


def _freshness(snapshot: dict) -> dict:
    required = (
        "source_digest",
        "decision_watermark",
        "source_watermark",
        "coverage",
        "cache_state",
        "latest_user_text",
        "generated_intention",
    )
    missing = [key for key in required if key not in snapshot or snapshot[key] is None]
    invalid = []
    if not isinstance(snapshot.get("source_digest"), str) or (
        len(snapshot["source_digest"]) != 64
        or any(char not in "0123456789abcdef" for char in snapshot["source_digest"])
    ):
        invalid.append("source_digest")
    coverage = snapshot.get("coverage")
    if not isinstance(coverage, dict) or not (
        coverage.get("source_history") in {"complete", "partial"}
        and coverage.get("source_reference_context") in {"complete", "partial"}
        and isinstance(coverage.get("unavailable_reference_context"), list)
        and all(
            isinstance(ref, str) and ref
            for ref in coverage["unavailable_reference_context"]
        )
    ):
        invalid.append("coverage")
    for key in ("source_watermark", "decision_watermark"):
        mark = snapshot.get(key)
        if not (
            type(mark) is int
            and mark >= 0
            or isinstance(mark, str)
            and len(mark) == 64
            and all(char in "0123456789abcdef" for char in mark)
        ):
            invalid.append(key)
    if not isinstance(snapshot.get("cache_state"), str) or snapshot[
        "cache_state"
    ] not in {
        "cold",
        "hit",
        "joined",
        "not_applicable",
    }:
        invalid.append("cache_state")
    if snapshot.get("generation_status") != "ready":
        invalid.append("generation_status")
    if not isinstance(snapshot.get("generated_intention"), str) or not snapshot.get(
        "generated_intention"
    ):
        invalid.append("generated_intention")
    return {
        "state": "UNESTABLISHED" if missing or invalid else "CAPTURED",
        "missing": missing,
        "invalid": invalid,
    }


def coverage_check(expected: dict, snapshot: dict, records: dict) -> dict:
    """Validate declared boundary using actual public identities, not fixture IDs."""
    actual = snapshot.get("coverage") or {}
    unavailable = expected["unavailable_reference_context"]
    required = [records.get(identifier) for identifier in unavailable]
    actual_refs = actual.get("unavailable_reference_context")
    linked = (
        isinstance(actual_refs, list)
        and len(actual_refs) == len(required)
        and all(required)
        and all(
            any(
                ref in {record.get("source_ref"), record.get("reasoning_ref")}
                for ref in actual_refs
            )
            for record in required
        )
        and len(set(actual_refs)) == len(actual_refs)
    )
    return {
        "source_history": actual.get("source_history") == expected["source_history"],
        "source_reference_context": (
            actual.get("source_reference_context")
            == expected["source_reference_context"]
        ),
        "unavailable_reference_context_linked": linked,
    }


def probe_observation(response: dict, terminal: dict, authorization: str) -> dict:
    """Authorization reference is not engine-verdict gold."""
    return {
        "authorization_reference": authorization,
        "initial_decision": response.get("decision"),
        "terminal_decision": terminal.get("decision"),
        "terminal_verified": terminal.get("state") in {"terminal", "human_required"},
        "engine_verdict_expectation": None,
        "requires_scope_review": (
            terminal.get("decision") == "approve" and authorization != "explicit_scoped"
        ),
    }


def execute_history(
    value: dict, *, primary_manifest: dict, factories, relay, output: Path, scope=None
) -> None:
    """Run all 48 checkpoints per frozen arm/seed through successive public state.

    ``factories`` is an approved runtime binding with open(client, hooks,
    session_id) and validate(config), not access to fixture/oracle contents.
    """
    verify_plan(value)
    verify_primary(primary_manifest)
    from tools.benchmark.source_scope import attempt_cap

    cap = attempt_cap(scope, primary_manifest["sha256"], value["history_bytes_sha256"])
    if value["primary_manifest_sha256"] != primary_manifest["sha256"]:
        raise ValueError("Primary manifest changed")
    if (
        relay.budget is None
        or relay.budget.max_attempts != cap
        or relay.budget.max_usd != 10
    ):
        raise RuntimeError(
            "Existing authorized shared ledger required, never reset for history"
        )
    if not relay.forbid_compiler:
        raise RuntimeError("Compiler guard required")
    config = {
        key: copy.deepcopy(value[key])
        for key in (
            "sha256",
            "models",
            "engine_axes",
            "arms",
            "trials",
        )
    }
    config["execution_scope"] = copy.deepcopy(scope)
    proof = factories.validate(config)
    if not isinstance(proof, dict) or (
        not isinstance(proof.get("approved_revision"), str)
        or len(proof["approved_revision"]) != 40
        or any(char not in "0123456789abcdef" for char in proof["approved_revision"])
        or proof.get("clean_runtime") is not True
        or proof.get("public_lifecycle_verified") is not True
        or not isinstance(proof.get("evidence_reference"), str)
        or not proof["evidence_reference"]
    ):
        raise RuntimeError("Reviewed history runtime hooks required")
    data = load_manifest()
    output.mkdir(exist_ok=False, parents=True)
    write_json(output / "execution-plan.json", value)
    rows = []
    info = {
        "planned_checkpoints": 288,
        "recorded_checkpoints": 0,
        "unestablished": 0,
        "missing_checkpoints": 288,
        "completed": False,
        "qa_status": "UNESTABLISHED",
        "end_to_end_passes": 0,
        "execution_scope": copy.deepcopy(scope),
    }
    write_json(output / "run.json", info)
    try:
        with (output / "results.jsonl").open("w") as stream:
            for trial in range(1, 4):
                stories = list(data["stories"])
                random.Random(value["seed"] + trial).shuffle(stories)
                for story in stories:
                    arms = list(value["arms"])
                    random.Random(value["seed"] + trial + len(rows)).shuffle(arms)
                    # Independent sessions preserve entire incremental lifecycle
                    # per arm; no checkpoint restart/ideal-intention substitution.
                    opened = []
                    try:
                        for arm in arms:
                            session_id = secrets.token_hex(16)
                            client, hooks = factories.open(
                                arm=arm,
                                backend="llm",
                                judge="advisory",
                                session_id=session_id,
                            )
                            opened.append(
                                (
                                    arm,
                                    PublicHistoryLifecycle(client, session_id, hooks),
                                    set(),
                                )
                            )
                        for checkpoint in story["checkpoints"]:
                            prefix = history_prefix(data, story["id"], checkpoint["id"])
                            oracle = checkpoint_contract(story, checkpoint["id"])
                            for arm, lifecycle, seen in opened:
                                relay.context = {
                                    "lane": "history",
                                    "trial": trial,
                                    "story": story["id"],
                                    "checkpoint": checkpoint["id"],
                                    "arm": arm,
                                }
                                row = {
                                    **relay.context,
                                    "state": "UNESTABLISHED",
                                    "recording_omissions": [],
                                }
                                try:
                                    for event in prefix["events"]:
                                        if event["id"] in seen:
                                            continue
                                        action = prefix["action_bindings"].get(
                                            event.get("decision", {}).get(
                                                "action_binding"
                                            ),
                                        )
                                        try:
                                            lifecycle.record(
                                                event,
                                                origin=prefix["actors"][event["actor"]],
                                                action=action,
                                            )
                                        except Unestablished as exc:
                                            lifecycle.omissions.append(
                                                {
                                                    "event": event["id"],
                                                    "kind": str(exc),
                                                },
                                            )
                                        seen.add(event["id"])
                                    snapshot = lifecycle.hooks.capture(
                                        client=lifecycle.client,
                                        session_id=lifecycle.session_id,
                                    )
                                    row["snapshot"] = copy.deepcopy(snapshot)
                                    warm = lifecycle.hooks.capture(
                                        client=lifecycle.client,
                                        session_id=lifecycle.session_id,
                                    )
                                    row["warm_snapshot"] = copy.deepcopy(warm)
                                    row["cache_artifact_consistency"] = all(
                                        snapshot.get(key) is not None
                                        and snapshot[key] == warm.get(key)
                                        for key in (
                                            "source_digest",
                                            "source_watermark",
                                            "decision_watermark",
                                            "generated_intention",
                                        )
                                    )
                                    row["evidence_level"] = snapshot.get(
                                        "evidence_level", "UNESTABLISHED"
                                    )
                                    row["freshness"] = _freshness(snapshot)
                                    row["coverage_checks"] = coverage_check(
                                        oracle["coverage"],
                                        snapshot,
                                        lifecycle.records,
                                    )
                                    users = [
                                        r
                                        for r in lifecycle.records.values()
                                        if r["role"] == "user"
                                    ]
                                    row["latest_user_linkage"] = bool(users) and (
                                        snapshot.get("latest_user_text")
                                        == users[-1]["text"]
                                    )
                                    row["generated_intention_semantics_verified"] = (
                                        snapshot.get(
                                            "generated_intention_semantics_verified"
                                        )
                                        is True
                                    )
                                    row["semantics"] = semantic_checks(
                                        oracle,
                                        snapshot,
                                        lifecycle.records,
                                    )
                                    row["recorded_reference_map"] = copy.deepcopy(
                                        lifecycle.records
                                    )
                                    row["recording_omissions"] = copy.deepcopy(
                                        lifecycle.omissions
                                    )
                                    declared_unavailable = set(
                                        oracle["coverage"][
                                            "unavailable_reference_context"
                                        ]
                                    )
                                    row["blocking_recording_omissions"] = [
                                        omission
                                        for omission in lifecycle.omissions
                                        if not (
                                            omission.get("kind")
                                            == "unavailable_reference_context"
                                            and omission.get("event")
                                            in declared_unavailable
                                        )
                                    ]
                                    row["declared_oracle_coverage"] = copy.deepcopy(
                                        oracle["coverage"]
                                    )
                                    if "probe" in checkpoint:
                                        probe = {
                                            key: copy.deepcopy(checkpoint["probe"][key])
                                            for key in ("tool", "args")
                                        }
                                        row["probe"] = lifecycle.client.evaluate(
                                            lifecycle.session_id,
                                            probe["tool"],
                                            probe["args"],
                                        )
                                        row["probe_authorization_reference"] = (
                                            checkpoint["probe"]["authorization"]
                                        )
                                        observer = getattr(
                                            lifecycle.hooks, "observe_action", None
                                        )
                                        row["probe_captured_artifact"] = (
                                            observer(lifecycle.session_id)
                                            if callable(observer)
                                            else {
                                                "state": "UNESTABLISHED",
                                                "reason": "API artifact binding unavailable",
                                            }
                                        )
                                        row["probe_terminal"] = read_terminal(
                                            lifecycle.client,
                                            row["probe"],
                                            judge_enabled=True,
                                        )
                                        terminal = row["probe_terminal"]
                                        row["probe_observation"] = probe_observation(
                                            row["probe"],
                                            terminal,
                                            checkpoint["probe"]["authorization"],
                                        )
                                        if not row["probe_observation"][
                                            "terminal_verified"
                                        ]:
                                            lifecycle.omissions.append(
                                                {"kind": "unverified_probe_terminal"}
                                            )
                                    if (
                                        not row["blocking_recording_omissions"]
                                        and row["freshness"]["state"] == "CAPTURED"
                                        and row["semantics"]["state"] == "PASS"
                                        and row["latest_user_linkage"]
                                        and row[
                                            "generated_intention_semantics_verified"
                                        ]
                                        and row["cache_artifact_consistency"]
                                        and all(row["coverage_checks"].values())
                                        and row.get("probe_observation", {}).get(
                                            "terminal_verified", True
                                        )
                                        and row["evidence_level"]
                                        in {"component", "end_to_end"}
                                    ):
                                        row["state"] = "PASS"
                                    elif (
                                        row["semantics"]["state"] == "FAIL"
                                        and not row["blocking_recording_omissions"]
                                    ):
                                        row["state"] = "FAIL"
                                except Exception as exc:
                                    row["error"] = type(exc).__name__
                                row["end_to_end_state"] = (
                                    row["state"]
                                    if row.get("evidence_level") == "end_to_end"
                                    else "UNESTABLISHED"
                                )
                                rows.append(row)
                                stream.write(json.dumps(row, sort_keys=True) + "\n")
                                stream.flush()
                                info["recorded_checkpoints"] = len(rows)
                                info["missing_checkpoints"] = 288 - len(rows)
                                info["unestablished"] = sum(
                                    r["state"] == "UNESTABLISHED" for r in rows
                                )
                                info["end_to_end_passes"] = sum(
                                    r["end_to_end_state"] == "PASS" for r in rows
                                )
                                write_json(output / "run.json", info)
                                relay.drain()
                                if relay.stopped or relay.validation_errors:
                                    raise RuntimeError(
                                        "History provider/budget boundary; rows preserved"
                                    )
                    finally:
                        for _, lifecycle, _ in opened:
                            lifecycle.hooks.close()
                            lifecycle.client.close()
            info["completed"] = True
            info["qa_status"] = (
                "PASS"
                if info["end_to_end_passes"] == 288
                else "FAIL"
                if any(row["end_to_end_state"] == "FAIL" for row in rows)
                else "UNESTABLISHED"
            )
    finally:
        relay.drain()
        write_json(output / "provider-attempts.json", relay.records)
        write_json(output / "provider-validation-errors.json", relay.validation_errors)
        write_json(output / "budget.json", relay.budget.snapshot())
        write_json(output / "run.json", info)
