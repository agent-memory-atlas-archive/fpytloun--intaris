"""FastAPI dependency injection for request identity.

Reads ContextVars set by the APIKeyMiddleware and provides a
SessionContext to all API endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException


@dataclass
class SessionContext:
    """Resolved identity for the current request.

    Attributes:
        user_id: Tenant identifier (always required for data operations).
        agent_id: Agent identifier (optional, from X-Agent-Id header).
        user_bound: True if user_id was bound from API key mapping
                    (prevents user switching in UI).
    """

    user_id: str
    agent_id: str | None = None
    user_bound: bool = False
    controller_claims: dict | None = None


def get_session_context() -> SessionContext:
    """Resolve session identity from ContextVars.

    Reads the ContextVars set by APIKeyMiddleware and returns a
    SessionContext. Raises 401 if user_id could not be resolved.

    Usage in endpoints:
        ctx: SessionContext = Depends(get_session_context)
    """
    from intaris.server import (
        _controller_claims,
        _session_agent_id,
        _session_user_bound,
        _session_user_id,
    )

    user_id = _session_user_id.get()
    if not user_id:
        raise HTTPException(
            status_code=401,
            detail=(
                "User identity required. Provide an API key mapped to a user_id "
                "(INTARIS_API_KEYS) or set the X-User-Id header."
            ),
        )

    return SessionContext(
        user_id=user_id,
        agent_id=_session_agent_id.get(),
        user_bound=_session_user_bound.get(),
        controller_claims=_controller_claims.get(),
    )


def authorize_controller(
    context: dict | None,
    session_id: str,
    ctx: SessionContext,
    *,
    declaration_digest: str | None = None,
) -> None:
    """Optional provenance is never inferred from ordinary authentication."""
    if declaration_digest is not None:
        claims = ctx.controller_claims or {}
        if (
            claims.get("sub") != ctx.user_id
            or not ctx.agent_id
            or claims.get("agent_id") != ctx.agent_id
            or claims.get("session_id") != session_id
            or claims.get("declaration_digest") != declaration_digest
            or not all(
                isinstance(claims.get(key), str) and 0 < len(claims[key]) <= 256
                for key in ("governing_execution_id", "governing_snapshot_id")
            )
        ):
            raise HTTPException(
                status_code=403,
                detail="Workflow declaration capability binding required",
            )
    if context is None:
        return
    if set(context) != {
        "governing_execution_id",
        "governing_snapshot_id",
        "executor_id",
    } or any(
        not isinstance(value, str) or not value or len(value) > 256
        for value in context.values()
    ):
        raise HTTPException(status_code=422, detail="Invalid controller context")
    claims = ctx.controller_claims
    if not claims or claims.get("session_id") != session_id:
        raise HTTPException(
            status_code=403, detail="Controller capability/session binding required"
        )
    for field in ("governing_execution_id", "governing_snapshot_id"):
        if claims.get(field) != context.get(field):
            raise HTTPException(
                status_code=403, detail=f"Controller {field} binding mismatch"
            )
