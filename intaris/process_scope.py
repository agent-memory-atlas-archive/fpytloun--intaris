"""Authenticated, action-bound task-process lifecycle declarations.

The controller owns registry identity; Intaris owns spawn admission. Neither an
ordinary client nor an executor-supplied description can establish this scope.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from intaris.hybrid import digest
from intaris.policy_activation import HybridError

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ProcessScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    operation: Literal["spawn", "terminate"]
    session_id: Identifier
    conversation_id: Identifier
    execution_scope: Identifier
    logical_call_id: Identifier
    executor_id: Identifier
    tool: Identifier
    evaluated_args_digest: Hash
    spawn_call_id: Identifier | None = None
    spawn_session_id: Identifier | None = None
    spawn_logical_call_id: Identifier | None = None
    shell_id: Identifier | None = None
    incarnation: Identifier | None = None


def verify_declaration(body, *, claims, user_id, agent_id, session_id, tool, args):
    """Verify identity and exact action before resolving any admission reference."""
    try:
        scope = ProcessScope.model_validate(body)
    except ValueError as exc:
        raise HybridError(
            "process_scope_invalid", "Invalid lifecycle declaration", 422
        ) from exc
    if (
        not claims
        or "intaris:process_lifecycle" not in claims.get("capabilities", [])
        or claims.get("sub") != user_id
        or claims.get("agent_id") != agent_id
        or claims.get("session_id") != session_id
        or claims.get("process_scope_digest") != digest(scope.model_dump())
        or scope.session_id != session_id
        or scope.tool != tool
        or scope.evaluated_args_digest != digest(args)
    ):
        raise HybridError(
            "process_scope_forbidden", "Lifecycle capability binding required", 403
        )
    refs = (
        scope.spawn_call_id,
        scope.spawn_session_id,
        scope.spawn_logical_call_id,
        scope.shell_id,
        scope.incarnation,
    )
    if scope.operation == "spawn":
        if tool != "bash" or args.get("run_in_background") is not True or any(refs):
            raise HybridError(
                "process_scope_invalid", "Managed background spawn required", 422
            )
    elif (
        tool != "bash_kill"
        or not all(refs)
        or args.get("shell_id") != scope.shell_id
        or args.get("target_executor", scope.executor_id) != scope.executor_id
    ):
        raise HybridError(
            "process_scope_invalid", "Exact registered termination required", 422
        )
    return scope


def verify_spawn(scope: ProcessScope, record: dict) -> dict:
    """Match an authenticated registry reference to the actual admitted spawn.

    Caller must load the record through the current user's audit store. This
    returns scope evidence, never an unconditional final policy approval.
    """
    metadata = record.get("evaluation_metadata") or {}
    admitted = metadata.get("process_lifecycle") or {}
    final = metadata.get("final_policy_result") or {}
    binding = admitted.get("declaration") or {}
    if (
        record.get("call_id") != scope.spawn_call_id
        or record.get("session_id") != scope.spawn_session_id
        or record.get("tool") != "bash"
        or final.get("decision") != "approve"
        or admitted.get("authenticated") is not True
        or binding.get("version") != 1
        or binding.get("operation") != "spawn"
        or binding.get("session_id") != scope.spawn_session_id
        or binding.get("logical_call_id") != scope.spawn_logical_call_id
        or binding.get("conversation_id") != scope.conversation_id
        or binding.get("execution_scope") != scope.execution_scope
        or binding.get("executor_id") != scope.executor_id
    ):
        raise HybridError(
            "process_scope_conflict", "Admitted lifecycle spawn does not match", 409
        )
    return {
        "authenticated": True,
        "matched": True,
        "declaration": scope.model_dump(),
        "permission_basis": "task_process_lifecycle",
        "not_human_approval": True,
    }
