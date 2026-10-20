"""Release Tcl-owned resources on the thread that created the application.

Also: tune_customtkinter_loops(), the polling intervals of CustomTkinter's own
trackers, set once before the first window.
"""
import gc
import threading
import tkinter as tk
from tkinter.font import Font

from app.ui import diagnostics

_owners = 0
_restore_automatic = False

#: CustomTkinter 5.2.2 polls the system appearance every 30 ms and every window's monitor DPI every 100 ms,
#: forever. The app never uses the "system" appearance (light or dark is chosen in Settings), so the first only
#: matters for that mode; a DPI change (the window dragged to a monitor with another scale) is still picked up
#: within half a second. Pinned by tests/ui/test_activity.py; recheck when upgrading CustomTkinter.
APPEARANCE_POLL_MS = 1000
DPI_POLL_MS = 500


def tune_customtkinter_loops() -> None:
    """Slow CustomTkinter's tracker loops down (idempotent; call before the first window)."""
    from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker
    from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

    AppearanceModeTracker.update_loop_interval = APPEARANCE_POLL_MS
    ScalingTracker.update_loop_interval = DPI_POLL_MS


class DesktopCollection:
    """While Tk is alive, collect cycles on its thread rather than on sync workers.

    Reference counting still works normally. Only automatic cyclic collection is
    replaced by an allocation-driven main-thread timer; the original GC mode is restored when
    the last desktop closes. Multiple test windows share this lifetime policy.
    `interval_ms` is how often the timer looks (set_interval: slower while minimized).
    """
    INTERVAL_MS = 2000

    def __init__(self, root):
        global _owners, _restore_automatic
        self.root, self.closed = root, False
        self.interval_ms = self.INTERVAL_MS
        if _owners == 0:
            _restore_automatic = gc.isenabled()
            gc.disable()
        _owners += 1
        self.timer = root.after(self.interval_ms, self.collect)

    def set_interval(self, interval_ms: int) -> None:
        """Look every `interval_ms` from now on (the next look is re-planned when the interval shrinks)."""
        interval_ms = max(100, int(interval_ms))
        shorter = interval_ms < self.interval_ms
        self.interval_ms = interval_ms
        if shorter and not self.closed:
            try:
                self.root.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = self.root.after(self.interval_ms, self.collect)

    def collect(self):
        if not self.closed:
            # Match generational allocation pressure, not elapsed wall time. A full
            # heap scan every two seconds stalls even an entirely idle desktop.
            # Collection stays on Tk's owner thread: worker finalizers may call Tcl.
            counts, thresholds = gc.get_count(), gc.get_threshold()
            if thresholds[0] and counts[0] >= thresholds[0]:
                generation = 0
                if counts[1] >= thresholds[1]:
                    generation = 2 if counts[2] >= thresholds[2] else 1
                with diagnostics.span("gc.timer_collect"):
                    gc.collect(generation)
            self.timer = self.root.after(self.interval_ms, self.collect)

    def close(self):
        global _owners
        if self.closed:
            return
        self.closed = True
        try:
            self.root.after_cancel(self.timer)
        except tk.TclError:
            pass
        _owners -= 1
        if _owners == 0 and _restore_automatic:
            gc.enable()


def release_resources(root):
    """After widgets/workers stop, finalize this interpreter's variables/fonts.

    Destroying a Tk widget does not remove Python reference cycles. If those are
    later collected by an HTTP/sync thread, Tk's destructors would call Tcl from
    that thread. Finalize resources here while the interpreter's thread is known.
    Other Tk roots are deliberately untouched.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Tk resources must be released on the desktop thread")
    for resource in gc.get_objects():
        if isinstance(resource, tk.Variable) and getattr(resource, "_tk", None) is root.tk:
            try:
                resource.__del__()
            except tk.TclError:
                pass  # a destroyed widget may already have removed a trace command
            finally:
                resource._tk = None
        elif isinstance(resource, Font) and getattr(resource, "_tk", None) is root.tk:
            resource.__del__()
            resource.delete_font = False
