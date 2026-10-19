"""Compose delegated source policy at the existing Intaris activation boundary."""

import json
from copy import deepcopy

from intaris.decision import clamp_outcome
from intaris.policy_activation import HybridError, digest


def source_policy(policy):
    result = deepcopy(policy or {})
    for key in ("active_policy", "policy_hash"):
        result.pop(key, None)
    return result


def authorization_identity(session):
    """Bind descendants to policy, human evidence/lifecycle revision and ancestry."""
    policy = session.get("policy") or {}
    if isinstance(policy, str):
        policy = json.loads(policy)
    return digest(
        {
            "policy": policy,
            "policy_revision": session["policy_revision"],
            "state_revision": session["state_revision"],
            "status": session["status"],
            "parent_policy_hash": session.get("parent_policy_hash"),
        }
    )


def validate_parent_chain(cur, session, *, postgresql=False):
    """Lock ancestors root-first, then fence the captured authorization chain.

    Call before locking/updating the child, and retain the cursor transaction
    through activation/admission commit. Parent relationships are immutable.
    SQLite's write transaction provides serialization; PostgreSQL needs row locks.
    """

    current = session
    seen = set()
    chain = []
    while current.get("parent_session_id"):
        parent_id = current["parent_session_id"]
        if parent_id in seen or len(seen) >= 32:
            raise HybridError("policy_state_changed", "Invalid policy ancestry", 409)
        seen.add(parent_id)
        cur.execute(
            "SELECT session_id, user_id, parent_session_id, policy, parent_policy_hash, "
            "policy_revision, state_revision, status FROM sessions "
            "WHERE session_id = ? AND user_id = ?",
            (parent_id, session["user_id"]),
        )
        row = cur.fetchone()
        parent = dict(row) if row is not None else None
        if parent is None:
            raise HybridError(
                "policy_state_changed",
                "Parent authorization is unavailable",
                409,
                retryable=True,
            )
        chain.append(parent)
        current = parent
    locked = {}
    for parent in reversed(chain):
        cur.execute(
            "SELECT session_id, user_id, parent_session_id, policy, parent_policy_hash, "
            "policy_revision, state_revision, status FROM sessions "
            "WHERE session_id = ? AND user_id = ?"
            + (" FOR UPDATE" if postgresql else ""),
            (parent["session_id"], session["user_id"]),
        )
        row = cur.fetchone()
        if row is None or row["parent_session_id"] != parent["parent_session_id"]:
            raise HybridError(
                "policy_state_changed", "Parent chain changed", 409, retryable=True
            )
        locked[parent["session_id"]] = dict(row)
    current = session
    while current.get("parent_session_id"):
        parent = locked[current["parent_session_id"]]
        if parent["status"] in {"suspended", "terminated"}:
            raise HybridError(
                "parent_session_inactive",
                "Parent session is suspended or terminated",
                403,
            )
        if current.get("parent_policy_hash") != authorization_identity(parent):
            raise HybridError(
                "policy_state_changed",
                "Parent authorization changed",
                409,
                retryable=True,
            )
        current = parent


def inherit_policy(parent, child):
    parent, child = source_policy(parent), source_policy(child)
    result = deepcopy(parent)
    # Child runtime context is independent of the parent's interaction surface.
    if (
        parent.get("interaction_mode") != "none"
        and child.get("interaction_mode") == "none"
    ):
        result["interaction_mode"] = child["interaction_mode"]
    for field in ("deny_tools", "deny_commands", "deny_paths", "deny_policies"):
        values = deepcopy(parent.get(field) or [])
        for value in child.get(field) or []:
            if value not in values:
                values.append(value)
        if values:
            result[field] = values
    # A delegate cannot manufacture additional auto-grants from its own home,
    # workdir or role. Restrict automatic matchers to literal shared grants.
    for field in ("allow_tools", "allow_commands", "allow_paths"):
        if field in child:
            result[field] = [
                value
                for value in parent.get(field) or []
                if value in (child.get(field) or [])
            ]
    # Prose may contain restrictions even inside an allowance. An independently
    # authored child prose allowance cannot safely be intersected syntactically.
    extra = [
        value
        for value in child.get("allow_policies") or []
        if value not in (parent.get("allow_policies") or [])
    ]
    if extra:
        raise HybridError(
            "invalid_delegated_policy",
            "Child prose allowances must be authorized in the parent policy.",
        )
    rules = deepcopy(parent.get("rules") or [])
    ids = {rule["id"] for rule in rules}
    for rule in child.get("rules") or []:
        if rule in rules:
            continue
        if rule.get("effect") == "allow":
            raise HybridError(
                "invalid_delegated_policy",
                "Child cannot add an independent allow rule.",
            )
        rule = deepcopy(rule)
        if rule["id"] in ids:
            from intaris.policy_activation import digest

            rule["id"] = "child." + digest(rule)
        ids.add(rule["id"])
        rules.append(rule)
    if rules:
        result["rules"] = rules
    if parent.get("read_only_required") or child.get("read_only_required"):
        result["read_only_required"] = True
    if child.get("minimum_outcome") is not None:
        result["minimum_outcome"] = clamp_outcome(
            child["minimum_outcome"], parent.get("minimum_outcome")
        )
    if child.get("maximum_outcome") is not None:
        cap = clamp_outcome(
            parent.get("maximum_outcome") or "deny", child["maximum_outcome"]
        )
        result["maximum_outcome"] = cap
    if parent.get("intention_gate") == "jev" or child.get("intention_gate") == "jev":
        result["intention_gate"] = "jev"
    judge = deepcopy(parent.get("judge") or {})
    local = child.get("judge") or {}
    if "allowed_decisions" in local:
        allowed = [
            value
            for value in local["allowed_decisions"]
            if value in judge.get("allowed_decisions", ["approve", "deny", "defer"])
        ]
        if not allowed:
            raise HybridError(
                "invalid_delegated_policy", "Incompatible child Judge decisions."
            )
        judge["allowed_decisions"] = allowed
    if "on_uncertain" in local:
        to_outcome = {"approve": "approve", "deny": "deny", "defer": "escalate"}
        outcome = clamp_outcome(
            to_outcome[local["on_uncertain"]],
            to_outcome.get(judge.get("on_uncertain")),
        )
        judge["on_uncertain"] = "defer" if outcome == "escalate" else outcome
    if local.get("allow_human_escalation") is False:
        judge["allow_human_escalation"] = False
    # Other Judge controls cannot silently replace inherited values.
    for key, value in local.items():
        if key in {"allowed_decisions", "on_uncertain", "allow_human_escalation"}:
            continue
        if key in judge and judge[key] != value:
            raise HybridError(
                "invalid_delegated_policy",
                f"Incompatible inherited Judge setting: {key}",
            )
        judge[key] = deepcopy(value)
    if judge:
        result["judge"] = judge
    return result
