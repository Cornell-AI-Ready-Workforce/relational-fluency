"""Best-effort upstream attempt records for session logs and metric listeners."""
from __future__ import annotations

import asyncio
import time
from typing import Callable


_listeners: list[Callable[[dict], None]] = []


def add_listener(fn: Callable[[dict], None]) -> None:
    try:
        _listeners.append(fn)
    except BaseException:
        pass


def record(*, upstream: str, route: str, purpose: str, outcome: str,
           duration_ms: float, status: int | None = None, attempt: int = 1,
           queue_wait_ms: float | None = None, session_id: str | None = None,
           detail: str | None = None) -> None:
    """Emit once per attempt. Neither a listener nor a store can fail a call.

    Listeners get epoch seconds in ``t``. The session event keeps the store's
    relative ``t`` and epoch ``wall``, like every other session event.
    """
    try:
        fields = dict(upstream=upstream, route=route, purpose=purpose,
                      outcome=outcome, duration_ms=duration_ms, status=status,
                      attempt=attempt, queue_wait_ms=queue_wait_ms,
                      session_id=session_id, detail=detail)
        if session_id is not None:
            try:
                from .session import registry
                registry.get(session_id)._store_event("upstream_attempt", **fields)
            except BaseException:
                pass
        event = dict(fields, t=time.time())
        for fn in tuple(_listeners):
            try:
                # A listener may mutate its copy without changing another sink.
                fn(dict(event))
            except BaseException:
                pass
    except BaseException:
        pass


class _Attempt:
    """Internal timer shared by the HTTP and websocket call sites."""

    def __init__(self, **fields):
        self.fields = fields
        self.started = time.perf_counter()
        self.finished = False

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        self.finish(exc=exc)

    def finish(self, *, outcome=None, exc=None):
        if self.finished:
            return
        self.finished = True
        try:
            fields = dict(self.fields)
            if outcome is not None:
                fields["outcome"] = outcome
            if "outcome" not in fields:
                fields["outcome"] = "ok"
                if isinstance(exc, asyncio.CancelledError):
                    fields["outcome"] = "cancelled"
                elif exc is not None:
                    # Both httpx and the SDK's vendored httpx2 use these names.
                    names = {cls.__name__ for cls in type(exc).__mro__}
                    if names & {"TimeoutError", "TimeoutException", "APITimeoutError"}:
                        fields["outcome"] = "timeout"
                    elif names & {"ConnectionClosed", "StopAsyncIteration"}:
                        fields["outcome"] = "closed"
                    else:
                        fields["outcome"] = "exception"
            status = fields.get("status")
            if not isinstance(status, int):
                status = None
            fields["status"] = status
            if status is not None and status >= 300:
                fields["outcome"] = "http_error"
            if exc is not None:
                # Exception bodies can repeat credentials or participant text.
                fields["detail"] = type(exc).__name__
            fields["duration_ms"] = max(
                0.0, (time.perf_counter() - self.started) * 1000)
            record(**fields)
        except BaseException:
            pass
