"""Per-call read-only ceiling. Capabilities and network reads are not mutation."""

from __future__ import annotations

from intaris.hybrid import digest
from intaris.policy_activation import HybridError
from intaris.shell_observations import unquoted_redirect


def resolve(context: dict | None, policy: dict | None = None) -> dict:
    context, policy = context or {}, policy or {}
    mode = context.get("chat_mode", "default")
    if not isinstance(mode, str) or mode not in {"default", "plan", "build"}:
        raise HybridError(
            "invalid_chat_mode", "chat_mode must be default, plan or build"
        )
    for values in (context, policy):
        if (
            "read_only_required" in values
            and type(values["read_only_required"]) is not bool
        ):
            raise HybridError(
                "invalid_read_only", "read_only_required must be a boolean"
            )
    if "chat_mode_source" in context and not isinstance(
        context["chat_mode_source"], str
    ):
        raise HybridError(
            "invalid_chat_mode_source", "chat_mode_source must be a string"
        )
    return {
        "chat_mode": mode,
        "chat_mode_source": context.get("chat_mode_source"),
        "read_only_required": context.get("read_only_required", False)
        or policy.get("read_only_required", False),
    }


def identity(overlay: dict) -> str:
    # Source labels cannot grant authority. Mode transitions cannot borrow a
    # previous assessment or explicit approval for the same tool arguments.
    return digest(
        {"mode": overlay["chat_mode"], "read_only": overlay["read_only_required"]}
    )


def observe(tool: str, args: dict) -> str:
    if tool.lower() in {"write", "edit", "multiedit", "apply_patch"}:
        return "mutating"
    if tool.lower() == "bash":
        from intaris.classifier import verified_read_only_bash

        command = args.get("command", args.get("cmd", ""))
        if isinstance(command, str) and unquoted_redirect(command):
            return "mutating"
        if verified_read_only_bash(args):
            return "read_only"
    # Legacy exemptions classify capability, not actual effects. In particular,
    # namespaced service exemptions and shell wrappers must be assessed.
    from intaris.classifier import verified_read_only

    return "read_only" if verified_read_only(tool, args) else "unclear"


def enforce(
    decision: str, overlay: dict | None, mutation: str
) -> tuple[str, str | None]:
    if not (overlay or {}).get("read_only_required"):
        return decision, None
    if mutation == "mutating":
        return "deny", "request.read_only_required"
    if mutation != "read_only":
        return "deny", "request.read_only_unproven"
    return decision, None


def explanation(
    decision: str, overlay: dict | None, mutation: str, effects=()
) -> str | None:
    if (
        decision != "deny"
        or not (overlay or {}).get("read_only_required")
        or mutation == "read_only"
    ):
        return None
    label = "This explicit read-only restriction"
    effects = set(effects or ())
    if "delete" in effects:
        return (
            f"{label} allows only read-only actions, and this action would delete data."
        )
    if effects & {"transmit", "disclose"}:
        return (
            f"{label} allows only read-only actions, and this action would send data "
            "or act outside your environment."
        )
    if mutation == "mutating":
        return f"{label} allows only read-only actions, and this action would make changes."
    return f"{label} allows only read-only actions, and this command could not be verified as read-only."
