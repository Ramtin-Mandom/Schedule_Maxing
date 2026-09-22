"""
background.py

A small helper for running work that touches the database (via
ExecutionService/ProductivityService/PlanningService) off the Tk main
thread, so a slow or merely non-trivial call never blocks the UI. Results
are handed back to the Tk main thread by a main-thread `after` poll; the
worker thread itself never calls Tk.

ControllerResult is the structured-error-handling mechanism used throughout
app/ui/: every ExecutionController/ProductivityController method that can
fail returns one of these instead of raising, so a Tk callback can always
branch on `.ok` and show `.error` to the user (e.g. via messagebox) instead
of letting an exception reach -- and potentially crash -- the UI.

Shutdown: every worker started through run_in_background is counted by a
WorkerRegistry (the installed one -- see install_registry -- unless another
is passed).
WorkerRegistry.shutdown() refuses new work and waits for the running
workers to finish, so the application can close its database connection
only once nothing is still using it (see app/ui/app_services.py). A result
is never delivered to a widget that no longer exists, nor after shutdown
has begun.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

_POLL_INTERVAL_MS = 15


@dataclass(frozen=True)
class ControllerResult(Generic[T]):
    """The outcome of one controller operation: either a value, or an error message."""

    ok: bool
    value: T | None = None
    error: str | None = None

    @classmethod
    def success(cls, value: T) -> ControllerResult[T]:
        return cls(ok=True, value=value, error=None)

    @classmethod
    def failure(cls, error: str) -> ControllerResult[T]:
        return cls(ok=False, value=None, error=error)


class WorkerRegistry:
    """Counts running background workers and lets shutdown wait for them."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False

    @property
    def closing(self) -> bool:
        with self._condition:
            return self._closing

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    def begin(self) -> bool:
        """Register a new worker; False (start nothing) once shutdown has begun."""
        with self._condition:
            if self._closing:
                return False
            self._active += 1
            return True

    def end(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def shutdown(self, timeout: float | None = None) -> bool:
        """Refuse new work, then wait for running workers. True if all finished in time."""
        with self._condition:
            self._closing = True
            return self._condition.wait_for(lambda: self._active == 0, timeout=timeout)


default_registry = WorkerRegistry()
_current_registry = default_registry


def install_registry(registry: WorkerRegistry) -> None:
    """Make `registry` the default for run_in_background (each app instance installs its own)."""
    global _current_registry
    _current_registry = registry


def current_registry() -> WorkerRegistry:
    return _current_registry


def run_in_background(
    widget: tk.Misc,
    work: Callable[[], T],
    on_done: Callable[[T], None],
    *,
    registry: WorkerRegistry | None = None,
) -> bool:
    """
    Run `work` on a background thread; deliver its return value to `on_done`
    back on the Tk main thread.

    `work` is expected to return a ControllerResult (every controller method
    does), so failures are just a value flowing through `on_done` -- there is
    no separate error callback here, since ControllerResult.ok/.error is
    already the single place a caller needs to check.

    The worker thread never calls into Tk: it only stores its result. This
    function (called on the Tk main thread) schedules a short `after` poll
    on the main thread that hands the result to `on_done` once it is ready.
    Tk calls from other threads need a running mainloop and can deadlock or
    fail, so none are made.

    Returns False (and runs nothing) if the registry is shutting down.
    """
    registry = registry or _current_registry
    if not registry.begin():
        return False

    done = threading.Event()
    outcome: list[T] = []

    def target() -> None:
        try:
            outcome.append(work())
        finally:
            done.set()
            registry.end()

    def poll() -> None:
        # Runs on the Tk main thread: never touch a destroyed widget or deliver after shutdown.
        if registry.closing:
            return
        try:
            if not widget.winfo_exists():
                return
            if not done.is_set():
                widget.after(_POLL_INTERVAL_MS, poll)
                return
        except tk.TclError:
            return
        if outcome:
            on_done(outcome[0])

    threading.Thread(target=target, daemon=True).start()
    widget.after(_POLL_INTERVAL_MS, poll)
    return True
