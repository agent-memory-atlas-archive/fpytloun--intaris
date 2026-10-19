"""Bounded event HTTP error codes; response detail bodies remain compatible."""

ERROR_HEADER = "X-Intaris-Event-Error"
EVENT_ROUTE = "/api/v1/session/{session_id}/events"
CODES = frozenset(
    {
        "recording_disabled",
        "session_not_found",
        "empty_batch",
        "batch_too_large",
        "invalid_event_type",
        "invalid_idempotency_key",
        "append_in_progress",
        "append_uncertain",
        "validation_failed",
        "unauthenticated",
        "forbidden",
        "request_too_large",
        "rate_limited",
        "unknown_rejection",
        "server_error",
    }
)


def error_category(status: int, code: str | None = None) -> str:
    """Never reflect arbitrary response headers into metrics."""
    if status < 400:
        return "success"
    if code in CODES:
        return code
    return {
        401: "unauthenticated",
        403: "forbidden",
        413: "request_too_large",
        422: "validation_failed",
        429: "rate_limited",
    }.get(status, "server_error" if status >= 500 else "unknown_rejection")
