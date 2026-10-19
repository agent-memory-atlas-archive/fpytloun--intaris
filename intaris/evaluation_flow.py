"""Drivers for the evaluator's narrow backend-selection continuation.

Only backend selection suspends. All decision logic remains in the evaluator;
no prefix is replayed when metadata becomes available.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Generator
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, TypeVar

from intaris.evaluator_backend import EvaluatorBackend

T = TypeVar("T")


@dataclass(frozen=True)
class BackendSelection:
    """Resume this exact selection site with a client or its selection error."""

    backend: EvaluatorBackend


def run_sync(flow: Generator[BackendSelection, Any, T]) -> T:
    """Drive the same evaluation continuation for synchronous callers."""
    value, error = None, None
    try:
        while True:
            try:
                selection = flow.throw(error) if error else flow.send(value)
            except StopIteration as done:
                return done.value
            try:
                value, error = selection.backend.select(), None
            except Exception as exc:
                value, error = None, exc
    finally:
        flow.close()


async def run_async(flow: Generator[BackendSelection, Any, T]) -> T:
    """Run synchronous segments in workers; await metadata outside worker slots."""
    context = copy_context()
    value, error = None, None
    pending: asyncio.Task | None = None
    canceled = threading.Event()

    def advance() -> tuple[bool, Any]:
        if canceled.is_set():
            flow.close()
            return True, None
        try:
            return False, flow.throw(error) if error else flow.send(value)
        except StopIteration as done:
            return True, done.value

    def cleanup(task: asyncio.Task) -> None:
        # Cancellation cannot stop a running Python segment. Close only after
        # it has finished, and retrieve its exception without resuming the flow.
        try:
            task.result()
        except BaseException:
            pass
        context.run(flow.close)

    try:
        while True:
            pending = asyncio.create_task(asyncio.to_thread(context.run, advance))
            done, result = await asyncio.shield(pending)
            pending = None
            if done:
                return result
            try:
                value, error = await result.backend.select_async(), None
            except Exception as exc:
                value, error = None, exc
    finally:
        canceled.set()
        if pending is not None:
            pending.add_done_callback(cleanup)
        else:
            context.run(flow.close)
