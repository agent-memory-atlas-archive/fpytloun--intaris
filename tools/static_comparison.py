"""Compare exact legacy static code and RC3 on identical private export IDs."""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intaris.classifier import _read_segments
from tools.rc3_replay import SQL, decode, prepare, private_write, replay_psql_command

LEGACY_SHA = "de115a5036966514c3ff5b0c13681b565ada5537"


def query(sql):
    completed = subprocess.run(
        replay_psql_command(), input=sql, text=True, capture_output=True, check=True
    )
    return [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]


def load_rows(export):
    ids = [row["call_id"] for row in json.loads(export.read_text())]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids):
        raise ValueError("Invalid audit identifier")
    sql = SQL.replace(
        "a.timestamp::timestamptz >= now()-interval '7 days'",
        "a.call_id IN (" + ",".join("'" + value + "'" for value in ids) + ")",
    )
    sql = sql.replace(
        "'call_id', a.call_id",
        "'evaluation_path', a.evaluation_path, 'call_id', a.call_id",
    )
    rows = query(sql)
    if set(ids) != {row["call_id"] for row in rows}:
        raise ValueError(
            "Export IDs cannot all be reconstructed; refuse denominator drift"
        )
    for row in rows:
        for field in ("args", "policy", "details", "metadata"):
            row[field] = decode(row[field])
    return rows


def compare(rows, baseline):
    sha = subprocess.check_output(
        ["git", "-C", str(baseline), "rev-parse", "HEAD"], text=True
    ).strip()
    if (
        sha != LEGACY_SHA
        or subprocess.check_output(
            ["git", "-C", str(baseline), "status", "--porcelain"], text=True
        ).strip()
    ):
        raise ValueError("Legacy tree must be clean at pinned SHA")
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("legacy_static_worker.py")),
            str(baseline),
        ],
        input="\n".join(json.dumps(row) for row in rows),
        text=True,
        capture_output=True,
        check=True,
    )
    legacy = {
        row["call_id"]: row for row in map(json.loads, process.stdout.splitlines())
    }
    tools = defaultdict(Counter)
    differences = defaultdict(list)
    results = []
    for original in rows:
        current = prepare(copy.deepcopy(original))
        old = legacy[original["call_id"]]
        result = {**current, **old}
        result["security_exception"] = security_exception(original, current)
        results.append(result)
        counts = tools[original["tool"]]
        counts["total"] += 1
        counts["legacy_static"] += int(old["legacy_static"])
        counts["rc3_static"] += int(current["fast_approved"])
        if old["legacy_static"] and not current["fast_approved"]:
            counts["regressions"] += 1
            differences[
                (original["tool"], current["reason"], result["security_exception"])
            ].append(original["call_id"])
    return results, {
        "legacy_sha": sha,
        "total": len(rows),
        "legacy_static": sum(row["legacy_static"] for row in results),
        "rc3_static": sum(row["fast_approved"] for row in results),
        "tools": dict(tools),
        "unjustified_regressions": sum(
            r["legacy_static"]
            and not r["fast_approved"]
            and r["security_exception"] is None
            for r in results
        ),
        "differences": [
            {
                "tool": tool,
                "reason": reason,
                "security_exception": exception,
                "count": len(ids),
                "sample_call_ids": ids[:10],
            }
            for (tool, reason, exception), ids in sorted(
                differences.items(), key=lambda item: str(item[0])
            )
        ],
        "limitations": [
            "Exact de115a5 classifier and working-directory resolver, cold path cache.",
            "Same IDs, current policy/details and redacted args fetched read-only for both.",
            "No historical image/policy snapshot or prior human receipt/path-cache reconstruction.",
            "No context read-only annotations are consumed by de115a5; tool_preferences are separate and not stored.",
            "No provider calls. Non-static classification is a fake escalation, excluded from static approvals.",
        ],
    }


def security_exception(row, current):
    if current["fast_approved"]:
        return None
    reason = current["reason"]
    if "human.checkpoint" in reason:
        return "explicit_human_checkpoint"
    if "operator.applicability" in reason:
        return "applicable_advisory_restriction"
    if current["observations"].get("secret_argument"):
        return "disclosure_protection"
    if row["tool"] != "bash":
        return None
    command = row["args"].get("command", row["args"].get("cmd", ""))
    segments = _read_segments(command)
    if not segments:
        return "unresolved_shell_syntax"
    if any(
        any(char in token for char in "*?[]{}~")
        for segment in segments
        for token in segment
    ):
        return "unresolved_target_expansion"
    if all(segment[0] == "git" for segment in segments):
        return "unverified_git_options_or_subcommand"
    if any(segment[0] == "ps" for segment in segments):
        return "process_environment_or_unverified_ps_format"
    if any(
        segment[0] == "git" and len(segment) > 1 and segment[1] == "merge-tree"
        for segment in segments
    ):
        return "git_object_write"
    if any(
        segment[0] in {"awk", "sed", "jq", "find", "sort", "env", "command", "kubectl"}
        for segment in segments
    ):
        return "script_filter_or_wrapper_requires_assessment"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export", type=Path, default=Path("/tmp/intaris-rc3-replay/export.json")
    )
    parser.add_argument(
        "--baseline", type=Path, default=Path("/tmp/intaris-de115a5-static-baseline")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/intaris-rc3-replay/comparison")
    )
    parser.add_argument("--read-production", action="store_true", required=True)
    options = parser.parse_args()
    options.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(options.output, 0o700)
    results, report = compare(load_rows(options.export), options.baseline)
    private_write(options.output / "calls.json", results)
    private_write(options.output / "report.json", report)
    lines = [
        "# Exact legacy static comparison",
        "",
        f"Baseline: `{LEGACY_SHA}`. Calls: {report['total']}.",
        "",
        "| Tool | Legacy static | RC3 static | Retained differences |",
        "|---|---:|---:|---:|",
    ]
    for tool, counts in sorted(report["tools"].items()):
        lines.append(
            f"| {tool.replace('|', '/')} | {counts['legacy_static']} | "
            f"{counts['rc3_static']} | {counts.get('regressions', 0)} |"
        )
    lines.extend(
        [
            "",
            f"Unjustified regressions: {report['unjustified_regressions']}.",
            "",
            "## Retained security exceptions and samples",
        ]
    )
    for item in report["differences"]:
        lines.extend(
            [
                "",
                f"### {item['tool']} — {item['security_exception']} ({item['count']})",
                f"Reason: `{item['reason']}`",
                "Sample call IDs: "
                + ", ".join(f"`{value}`" for value in item["sample_call_ids"]),
            ]
        )
    lines.extend(
        [
            "",
            "## Measurement limitations",
            *("- " + line for line in report["limitations"]),
        ]
    )
    fd = os.open(
        options.output / "REPORT.md", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("\n".join(lines) + "\n")
    print(
        json.dumps(
            {key: report[key] for key in ("total", "legacy_static", "rc3_static")}
        )
    )


if __name__ == "__main__":
    main()
