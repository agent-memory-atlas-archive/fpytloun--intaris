"""Content-free, context-local inference attribution."""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from uuid import uuid4

OPERATIONS = frozenset(
    {
        "summary_window",
        "compaction",
        "l3",
        "intention",
        "source_context",
        "policy_compile",
        "policy_assess",
        "alignment",
        "evaluation",
        "judge",
    }
)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


@dataclass(frozen=True)
class AttributionScope:
    operation: str
    operation_id: str
    session_id: str | None = None
    task_id: str | None = None


_scope: ContextVar[AttributionScope | None] = ContextVar(
    "inference_attribution", default=None
)


@contextmanager
def llm_attribution(
    *, operation: str, session_id=None, task_id=None, operation_id=None
):
    """Attribute one logical operation, including all its repair/retry calls."""
    if operation not in OPERATIONS:
        raise ValueError("Unsupported inference operation")
    # Attribution must not make a previously valid application call fail.
    session_id = (
        session_id
        if isinstance(session_id, str) and _ID.fullmatch(session_id)
        else None
    )
    task_id = task_id if isinstance(task_id, str) and _ID.fullmatch(task_id) else None
    parent = _scope.get()
    if operation_id is not None and (
        not isinstance(operation_id, str) or not _ID.fullmatch(operation_id)
    ):
        raise ValueError("Invalid inference operation ID")
    scope = AttributionScope(
        operation,
        operation_id or uuid4().hex,
        session_id if session_id is not None else parent.session_id if parent else None,
        task_id if task_id is not None else parent.task_id if parent else None,
    )
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def attribution_headers():
    """Return allowlisted metadata for one managed HTTP submission."""
    scope = _scope.get()
    if scope is None:
        return {}
    values = {
        "Operation": scope.operation,
        "Operation-ID": scope.operation_id,
        "Session-ID": scope.session_id,
        "Task-ID": scope.task_id,
        "Client-Request-ID": uuid4().hex,
    }
    return {
        f"X-Cognis-Inference-{key}": value
        for key, value in values.items()
        if value is not None
    }


def attributed(operation):
    """Scope a synchronous owner inside its worker thread."""

    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with llm_attribution(
                operation=operation, session_id=kwargs.get("session_id")
            ):
                return function(*args, **kwargs)

        return call

    return decorate
