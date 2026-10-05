"""
app/ui/layout.py

Two small tools that keep the desktop's layout stable while the window is
moved, resized, minimized or restored (Milestone 4 desktop shell):

- Coalescer: many requests (e.g. one per <Configure> event) become one call
  on the Tk event loop -- after the pending events are handled
  (after_idle), or after a short delay. A newer request never adds a second
  pending call, and nothing runs once the widget is gone.

- BoundedAnimation: a fixed number of steps, each scheduled with after();
  starting it again cancels the previous run, and it stops as soon as the
  widget is destroyed. It can never schedule more than `steps` callbacks.

Both only call the widget's after/after_idle/after_cancel/winfo_exists, so
tests drive them with a stand-in widget.
"""

from __future__ import annotations

from collections.abc import Callable

from app.ui import diagnostics


def _alive(widget) -> bool:
    try:
        return bool(widget.winfo_exists())
    except Exception:  # noqa: BLE001 - a destroyed Tk application raises TclError here
        return False


class Coalescer:
    def __init__(self, widget, callback: Callable[[], None], *, delay_ms: int = 0) -> None:
        self._widget = widget
        self._callback = callback
        self._delay_ms = delay_ms
        self._pending = None
        self.runs = 0

    @property
    def pending(self) -> bool:
        return self._pending is not None

    def request(self) -> None:
        """Run the callback once, soon; further requests before it runs change nothing."""
        if self._pending is not None or not _alive(self._widget):
            return
        if self._delay_ms:
            self._pending = self._widget.after(self._delay_ms, self._run)
        else:
            self._pending = self._widget.after_idle(self._run)

    def cancel(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and _alive(self._widget):
            try:
                self._widget.after_cancel(pending)
            except Exception:  # noqa: BLE001 - already run or the interpreter is going away
                pass

    def flush(self) -> None:
        """Run a pending callback now (e.g. before reading the layout it computes)."""
        if self._pending is not None:
            self.cancel()
            self._run()

    def _run(self) -> None:
        self._pending = None
        if not _alive(self._widget):
            return
        self.runs += 1
        with diagnostics.span("coalesced", self._callback):
            self._callback()


class BoundedAnimation:
    def __init__(
        self,
        widget,
        on_step: Callable[[float], None],
        *,
        steps: int = 8,
        interval_ms: int = 16,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if steps < 1:
            raise ValueError("an animation needs at least one step")
        self._widget = widget
        self._on_step = on_step
        self._steps = steps
        self._interval_ms = interval_ms
        self._on_done = on_done
        self._pending = None
        self._step = 0
        self.scheduled = 0

    @property
    def running(self) -> bool:
        return self._pending is not None

    def start(self) -> None:
        self.stop()
        self._step = 0
        self._schedule()

    def stop(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and _alive(self._widget):
            try:
                self._widget.after_cancel(pending)
            except Exception:  # noqa: BLE001
                pass

    def finish(self) -> None:
        """Jump to the final state now (no further callbacks)."""
        self.stop()
        if _alive(self._widget):
            self._on_step(1.0)
            if self._on_done is not None:
                self._on_done()

    def _schedule(self) -> None:
        if not _alive(self._widget):
            self._pending = None
            return
        self.scheduled += 1
        self._pending = self._widget.after(self._interval_ms, self._tick)

    def _tick(self) -> None:
        self._pending = None
        if not _alive(self._widget):
            return
        self._step += 1
        fraction = min(1.0, self._step / self._steps)
        with diagnostics.span("animation.step", self._on_step):
            self._on_step(fraction)
        if self._step < self._steps:
            self._schedule()
        elif self._on_done is not None:
            self._on_done()
