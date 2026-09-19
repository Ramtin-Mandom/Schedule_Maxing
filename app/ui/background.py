"""
background.py

A small helper for running work that touches the database (via
ExecutionService/ProductivityService) off the Tk main thread, so a slow or
merely non-trivial call never blocks the UI. Results are delivered back onto
the Tk main thread via `widget.after`, which is the only thread-safe way to
touch Tk/CustomTkinter widgets from work started on another thread.

ControllerResult is the structured-error-handling mechanism used throughout
app/ui/: every ExecutionController/ProductivityController method that can
fail returns one of these instead of raising, so a Tk callback can always
branch on `.ok` and show `.error` to the user (e.g. via messagebox) instead
of letting an exception reach -- and potentially crash -- the UI.
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


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


def run_in_background(
    widget: tk.Misc,
    work: Callable[[], T],
    on_done: Callable[[T], None],
) -> None:
    """
    Run `work` on a background thread; deliver its return value to `on_done`
    back on the Tk main thread via `widget.after(0, ...)`.

    `work` is expected to return a ControllerResult (every controller method
    does), so failures are just a value flowing through `on_done` -- there is
    no separate error callback here, since ControllerResult.ok/.error is
    already the single place a caller needs to check.
    """

    def target() -> None:
        result = work()
        widget.after(0, lambda: on_done(result))

    threading.Thread(target=target, daemon=True).start()
