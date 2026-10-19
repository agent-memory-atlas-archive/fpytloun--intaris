"""Shared transient-retry policy for model calls (LLM and Jev).

Each transport owns its retry loop (``LLMClient._call_with_transient_retries``
and ``JevClient._request``); this module holds the policy they share: the
attempt count, full-jitter backoff, the transient status set and the
per-evaluation deadline.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.25
BACKOFF_CAP_SECONDS = 2.0
# A retry with less time than this left before the deadline is not started.
MIN_ATTEMPT_SECONDS = 1.0
# Connection errors and timeouts are transient as well; 4xx validation and
# auth errors (400/401/403/404/422) never are.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
# Cognis inference reports an unavailable upstream with these error codes.
UPSTREAM_UNAVAILABLE_CODES = frozenset({"upstream_error", "model_unavailable"})

_deadline: ContextVar[float | None] = ContextVar("model_call_deadline", default=None)


@contextmanager
def evaluation_deadline(seconds: float | None) -> Iterator[None]:
    """Bound retries of model calls made in this context to ``seconds``.

    A nested deadline never extends an outer one. ``asyncio.to_thread``
    copies the context, so a deadline set by a request covers its worker.
    """
    if seconds is None:
        yield
        return
    proposed = time.monotonic() + max(0.0, seconds)
    current = _deadline.get()
    token = _deadline.set(proposed if current is None else min(current, proposed))
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def model_call_deadline(attempt_seconds: float) -> Iterator[None]:
    """Preserve the caller's retry budget, or bound a standalone model call.

    The transport timeout still limits each attempt. Nesting that timeout as a
    total deadline would prevent a retry after the first timed-out attempt.
    """
    if _deadline.get() is not None:
        yield
    else:
        with evaluation_deadline(attempt_seconds):
            yield


def backoff_delay(retry: int, retry_after: float | None = None) -> float:
    """Delay before retry number ``retry`` (1-based), at most the cap.

    A provider retry-after is honored up to the cap; otherwise full jitter
    over an exponential window: uniform(0, min(cap, base * 2**(retry-1))).
    """
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, BACKOFF_CAP_SECONDS)
    window = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (retry - 1))
    return random.uniform(0.0, window)


def remaining_seconds() -> float | None:
    """Remaining shared evaluation budget, or None outside an evaluation."""
    deadline = _deadline.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def retry_timeout(
    delay: float, attempt_timeout: float, *, first_attempt: bool = False
) -> float | None:
    """Timeout for a retry after ``delay``, or None when it must not start.

    Without a deadline the configured timeout applies. Under a deadline the
    retry's timeout is capped to the time left after the delay, so a started
    retry always ends within the budget; it does not start when less than
    ``MIN_ATTEMPT_SECONDS`` would remain.
    """
    deadline = _deadline.get()
    if deadline is None:
        return attempt_timeout
    remaining = deadline - time.monotonic() - delay
    minimum = 0 if first_attempt else min(MIN_ATTEMPT_SECONDS, attempt_timeout)
    if remaining <= 0 or remaining < minimum:
        return None
    return min(attempt_timeout, remaining)


def is_transient_status(status: int | None, body: object = None) -> bool:
    """Transient HTTP status, or Cognis's upstream-unavailable error shape.

    A Cognis error marked ``category: request`` is a request (validation)
    failure whatever its status, and is never transient.
    """
    error = body.get("error", body) if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    if error.get("category") == "request":
        return False
    if status in TRANSIENT_STATUSES:
        return True
    return (status is None or status >= 500) and error.get(
        "code"
    ) in UPSTREAM_UNAVAILABLE_CODES
