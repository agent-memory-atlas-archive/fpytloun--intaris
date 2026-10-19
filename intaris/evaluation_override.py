"""Operator-only reversible emergency routing; never mutate persisted policy."""

import os


def force_legacy() -> bool:
    value = os.environ.get("EVALUATION_POLICY_FORCE", "")
    if value not in {"", "legacy"}:
        raise ValueError("EVALUATION_POLICY_FORCE must be unset or legacy")
    return value == "legacy"


def session_view(session: dict, forced: bool) -> dict:
    if not forced:
        return session
    return {
        **session,
        "policy": {
            **(session.get("policy") or {}),
            "evaluation_policy_version": "legacy",
        },
    }
