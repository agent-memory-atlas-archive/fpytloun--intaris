"""Deterministic analytics projections, never authorization or execution receipts."""

from __future__ import annotations

import json
from typing import Any

VERSION = 1
PROMPT_VERSION = "analytics-evidence-v1"


def metadata(value: Any) -> dict[str, Any]:
    """Old rows have unknown coverage."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def statistics(calls: list[dict[str, Any]]) -> dict[str, int]:
    """Keep initial-decision counters separate from known dispositions."""
    result = {"total": len(calls)}
    for decision, name in (
        ("approve", "approved"),
        ("deny", "denied"),
        ("escalate", "escalated"),
    ):
        result[f"{name}_count"] = sum(c.get("decision") == decision for c in calls)
        result[f"final_{name}_count"] = sum(
            (c.get("user_decision") or c.get("effective_decision") or c.get("decision"))
            == decision
            for c in calls
        )
        result[f"resolved_{name}_count"] = sum(
            c.get("user_decision") == decision for c in calls
        )
    return result


def select_summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select compaction plus uncovered windows using exact membership."""
    windows = [r for r in rows if r.get("summary_type") == "window"]
    compactions = [r for r in rows if r.get("summary_type") == "compacted"]
    for compacted in sorted(
        compactions, key=lambda r: str(r.get("created_at", "")), reverse=True
    ):
        evidence = metadata(compacted.get("evidence_metadata"))
        covered = set(evidence.get("window_ids", []))
        if evidence.get("version") == VERSION and covered:
            return [compacted] + [r for r in windows if r.get("id") not in covered]
    # Legacy compactions used wall-clock end times, not source watermarks.
    return windows or compactions


def message_from_audit(record: dict[str, Any]) -> dict[str, Any]:
    args = metadata(record.get("args_redacted"))
    eligible = args.get("intention_eligible", True)
    return {
        "ts": record.get("timestamp", ""),
        "role": "user",
        "text": record.get("content", "").removeprefix("User message:").strip(),
        "seq": args.get("source_event_seq"),
        "source_call_id": record.get("call_id"),
        "intention_eligible": eligible if isinstance(eligible, bool) else False,
    }


def aggregate_dispositions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Latest observed resolution per proposal, without double-counting updates."""
    calls: dict[str, dict[str, Any]] = {}
    unknown = 0
    for row in rows:
        evidence = metadata(row.get("evidence_metadata"))
        if "call_dispositions" not in evidence:
            unknown += 1
        calls.update(evidence.get("call_dispositions", {}))
    return {
        "call_dispositions": calls,
        "dispositions": statistics(list(calls.values())),
        "unknown_statistics_windows": unknown,
    }


def merge_messages(
    conversation: list[dict[str, Any]], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deduplicate durable identities, never repeated text."""
    result = [dict(e) for e in conversation]
    by_seq = {e.get("seq"): e for e in result if e.get("seq") is not None}
    for record in records:
        entry = message_from_audit(record)
        existing = by_seq.get(entry.get("seq"))
        if existing is not None and existing.get("role") == "user":
            existing["source_call_id"] = entry.get("source_call_id")
            existing["intention_eligible"] = (
                existing.get("intention_eligible", True) and entry["intention_eligible"]
            )
        else:
            result.append(entry)
    return sorted(result, key=lambda e: (str(e.get("ts", "")), e.get("seq") or 0))
