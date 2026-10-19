"""Read persisted outcomes without simulating a human decision."""

from __future__ import annotations

import time
from typing import Any

from tools.benchmark.client import IntarisClient


def terminal_outcome(response: dict, audit: dict | None) -> dict[str, Any]:
    """Keep policy floors authoritative; a Judge recommendation is not permission."""
    record = audit or {}
    decision = record.get("effective_decision") or response.get("decision")
    resolved = record.get("user_decision")
    if resolved in {"approve", "deny"} and record.get("resolved_by") in {
        "judge",
        "user",
    }:
        decision = resolved
    error = record.get("judge_error")
    state = (
        "judge_error"
        if error
        else "human_required"
        if decision == "escalate" and record.get("judge_decision") == "defer"
        else "pending"
        if decision == "escalate"
        else "terminal"
        if decision in {"approve", "deny"}
        else "error"
    )
    return {
        "initial_decision": response.get("decision"),
        "raw_decision": record.get("raw_decision") or response.get("raw_decision"),
        "decision": decision,
        "state": state,
        "human_required": decision == "escalate",
        "judge_decision": record.get("judge_decision"),
        "resolved_by": record.get("resolved_by"),
        "audit": audit,
    }


def read_terminal(
    client: IntarisClient,
    response: dict,
    *,
    judge_enabled: bool,
    timeout_s: float | None = None,
    poll_s: float = 0.2,
) -> dict[str, Any]:
    """Bounded readback. Missing/failed audit is an explicit measurement error."""
    if timeout_s is None:
        configured = getattr(client, "review_timeout_s", 20)
        timeout_s = configured if isinstance(configured, (int, float)) else 20
    started = time.monotonic()
    deadline = started + timeout_s
    result = terminal_outcome(response, None)
    call_id = response.get("call_id")
    if not call_id:
        return {**result, "state": "error", "readback_error": "missing_call_id"}
    try:
        while True:
            result = terminal_outcome(response, client.get_audit_record(call_id))
            if result["state"] != "pending" or not judge_enabled:
                if result["state"] == "pending" and not judge_enabled:
                    result["state"] = "human_required"
                break
            if time.monotonic() >= deadline:
                result["state"] = "unresolved_timeout"
                break
            time.sleep(min(poll_s, max(0, deadline - time.monotonic())))
    except Exception as exc:
        # Provider/server error text may contain request material or credentials.
        result["state"] = "readback_error"
        result["readback_error"] = type(exc).__name__
    result["readback_ms"] = (time.monotonic() - started) * 1000
    return result
