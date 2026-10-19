"""One privacy-preserving translation boundary for hybrid API failures."""

import logging

from fastapi import HTTPException

from intaris.policy_activation import HybridError

logger = logging.getLogger(__name__)


def hybrid_http_error(
    error: HybridError,
    *,
    session_id: str | None = None,
    tool: str | None = None,
    call_id: str | None = None,
    logical_call_id: str | None = None,
) -> HTTPException:
    """Log only correlation fields, never exception text or request payloads."""
    fields = {
        "code": error.code,
        "status": error.status,
        "session_id": session_id,
        "tool": tool,
        "call_id": error.call_id or call_id,
        "logical_call_id": error.logical_call_id or logical_call_id,
    }
    logger.warning("HybridError %r", fields, extra={"hybrid_error": fields})
    return HTTPException(status_code=error.status, detail=error.detail())
