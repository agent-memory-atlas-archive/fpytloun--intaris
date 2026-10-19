#!/usr/bin/env python3
"""Private, content-free production replay. No provider client is constructed.

Production redacted args are processed only in memory. The local 0600 export
contains type shape and derived safety observations, never argument text, user
messages, tool results, session intentions or raw provider output.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intaris.evaluator import _CONTEXT_ARG_KEY, _effective_working_directory
from intaris.hybrid import evaluate_policy, validate_rules
from intaris.hybrid_runtime import _observe_action, finalize_result
from intaris.policy_activation import prepare_policy
from intaris.policy_coverage import source_constraint
from intaris.policy_evidence import Review, from_observations

SQL = """
BEGIN READ ONLY;
SET LOCAL statement_timeout = '120s';
SELECT json_build_object(
 'call_id', a.call_id, 'session_id', a.session_id, 'tool', a.tool,
 'args', a.args_redacted, 'decision', a.decision,
 'minimum_outcome', a.minimum_outcome, 'policy', s.policy,
 'agent', a.agent_id, 'details', s.details,
 'unattended', COALESCE(s.policy::jsonb->>'interaction_mode','') = 'none',
 'had_user_message', s.latest_user_evidence IS NOT NULL,
 'hybrid', COALESCE(a.evaluation_metadata::jsonb->>'evaluation_policy_version','') = 'hybrid_v1',
 'metadata', a.evaluation_metadata
) FROM audit_log a JOIN sessions s
 ON a.session_id=s.session_id AND a.user_id=s.user_id
WHERE a.timestamp::timestamptz >= now()-interval '7 days'
 AND a.record_type='tool_call'
ORDER BY a.timestamp;
COMMIT;
"""


def decode(value):
    return json.loads(value) if isinstance(value, str) else value or {}


STRUCTURAL_KEYS = {
    "command",
    "cmd",
    "path",
    "file_path",
    "directory",
    "pattern",
    "query",
    "timezone",
    "skill_id",
    "limit",
    "offset",
    "content",
    "messages",
    "role",
    "text",
    "data",
    "headers",
    "args",
    "arguments",
    "options",
    "body",
}


def shape(value):
    if isinstance(value, dict):
        fields = {
            key: shape(item) for key, item in value.items() if key in STRUCTURAL_KEYS
        }
        unknown = [
            shape(item) for key, item in value.items() if key not in STRUCTURAL_KEYS
        ]
        # Untrusted map keys are argument content too. Never persist their names,
        # hashes or case variants; retain only value shapes/counts.
        return {"type": "object", "fields": fields, "other_values": unknown}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return type(value).__name__


def private_write(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def replay_psql_command():
    """Build a read-only replay connection from explicit operator configuration."""
    return [
        "kubectl",
        "-n",
        os.environ["INTARIS_REPLAY_DB_NAMESPACE"],
        "exec",
        "-i",
        os.environ["INTARIS_REPLAY_DB_TARGET"],
        "-c",
        os.environ["INTARIS_REPLAY_DB_CONTAINER"],
        "--",
        "psql",
        "-U",
        os.environ["INTARIS_REPLAY_DB_USER"],
        "-d",
        os.environ["INTARIS_REPLAY_DB_NAME"],
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
    ]


class EscalatingModels:
    """Deterministic stand-in: any call needing inference remains non-fast."""

    calls = 0

    def assess(self, state):
        self.calls += 1
        return state.model_copy(
            update={
                "assessment_failed": True,
                "review": Review(
                    user_would="ask",
                    basis="missing_permission",
                    obligation_ids=[],
                    evidence_refs=[],
                ),
            }
        )


class BoundedCompiler:
    """The existing full-match grammar supplies exact predicates; no inference."""

    def compile(self, tenant, clauses, scope):
        return [
            {
                "id": clause["id"],
                "effect": clause["effect"],
                "when": source_constraint(clause["text"], clause["effect"]),
            }
            for clause in clauses
        ]


def prepare(row):
    args = decode(row.pop("args"))
    policy = decode(row.pop("policy"))
    metadata = decode(row.pop("metadata"))
    details = decode(row.pop("details"))
    # Retain administrative policy/deny clauses requested for replay, not model
    # snapshots, evidence histories, or policy display descriptions.
    policy = {
        key: policy[key]
        for key in (
            "allow_tools",
            "deny_tools",
            "allow_paths",
            "deny_paths",
            "deny_commands",
            "deny_policies",
            "allow_policies",
            "read_only",
            "read_only_required",
            "maximum_outcome",
            "interaction_mode",
            "rules",
        )
        if key in policy
    }
    policy.update(evaluation_policy_version="hybrid_v1", profile="balanced")
    active = prepare_policy(policy, tenant="local-replay", compiler=BoundedCompiler())
    snapshot = active["active_policy"]
    context = args.pop(_CONTEXT_ARG_KEY, {})
    if args.get("context") == context:
        args.pop("context", None)
    overlay = metadata.get("overlay") or {
        "read_only_required": context.get("read_only_required", False)
        or context.get("chat_mode") == "plan",
        "chat_mode": context.get("chat_mode", "default"),
    }
    original = _observe_action(
        row["tool"],
        args,
        policy=active,
        working_directory=_effective_working_directory(
            row["tool"], args, context, details
        ),
        tool_preferences=None,
    )
    rules = validate_rules(snapshot["rules"])
    state = from_observations(
        original, sources={}, read_only=overlay["read_only_required"]
    )
    state = state.model_copy(
        update={"advisory_clauses": snapshot.get("advisory_clauses", [])}
    )
    result = evaluate_policy(rules, state, "balanced")
    models = EscalatingModels()
    original_result = result
    if result.outcome == "needs_assessment":
        state = models.assess(state)
        result = evaluate_policy(rules, state, "balanced", assessed=True)
    final = finalize_result(
        result,
        maximum=active.get("maximum_outcome"),
        minimum=row.get("minimum_outcome"),
        overlay=overlay,
        mutation=original["mutation"],
    )
    fast = final.decision == "approve" and result.permission_basis == "read_only_static"
    evidence = {
        key: original.get(key)
        for key in (
            "static_read",
            "mutation",
            "effects",
            "configured_deny",
            "configured_allow",
            "secret_argument",
            "paths_complete",
        )
    }
    return {
        **row,
        "args_shape": shape(args),
        "session_policy": policy,
        "observations": evidence,
        "target_digest": hashlib.sha256(
            json.dumps(original.get("paths", [])).encode()
        ).hexdigest(),
        "fast_approved": fast,
        "effective_decision": final.decision,
        "reason": "read_only_static"
        if fast
        else (
            (
                final.override
                if final.override != "assessment.invalid_response"
                else None
            )
            or (
                ",".join(item.id for item in original_result.outstanding_obligations)
                or result.permission_basis
                or result.outcome
            )
        ),
        "fake_inference_calls": models.calls,
    }


def summarize(rows):
    legacy = [row for row in rows if not row["hybrid"] and row["decision"] == "approve"]
    matched = sum(row["fast_approved"] for row in legacy)
    by_tool = defaultdict(Counter)
    for row in rows:
        by_tool[row["tool"]]["total"] += 1
        by_tool[row["tool"]]["fast_approved"] += int(row["fast_approved"])
    return {
        "window_days": 7,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total": len(rows),
        "fast_approved": sum(r["fast_approved"] for r in rows),
        "legacy_approved_proxy_count": len(legacy),
        "legacy_approved_fast_count": matched,
        "legacy_approved_fast_percent": round(100 * matched / len(legacy), 2)
        if legacy
        else None,
        "target_met": bool(legacy) and matched / len(legacy) >= 0.9,
        "tools": dict(by_tool),
        "non_fast_reasons": dict(
            Counter(row["reason"] for row in rows if not row["fast_approved"])
        ),
        "remainder": [
            {"call_id": r["call_id"], "tool": r["tool"], "reason": r["reason"]}
            for r in legacy
            if not r["fast_approved"]
        ],
        "limitations": [
            "Historical image version is not recorded; approved non-hybrid rows are a de115a5 proxy.",
            "Uses current session policy and current user-evidence presence, not historical snapshots.",
            "Replays deterministic policy boundary with real redacted args in memory, not HTTP.",
            "No human receipt reuse or runtime alignment/lifecycle gate is inferred from missing history.",
            "No paid inference; fake assessment/Judge always escalates, never promotes the fast numerator.",
            "Export keeps argument shapes/derived safety facts, never content.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/intaris-rc3-replay"))
    parser.add_argument(
        "--export-production",
        action="store_true",
        help="Explicitly authorize read-only kubectl PostgreSQL export",
    )
    options = parser.parse_args()
    if not options.export_production:
        parser.error("--export-production is required")
    options.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(options.output, 0o700)
    process = subprocess.run(
        replay_psql_command(),
        input=SQL,
        text=True,
        capture_output=True,
        check=True,
    )
    rows = [
        prepare(json.loads(line))
        for line in process.stdout.splitlines()
        if line.startswith("{")
    ]
    private_write(options.output / "export.json", rows)
    report = summarize(rows)
    private_write(options.output / "report.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "total",
                    "fast_approved",
                    "legacy_approved_proxy_count",
                    "legacy_approved_fast_percent",
                    "target_met",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
