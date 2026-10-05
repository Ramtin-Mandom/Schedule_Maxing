"""Release Tcl-owned resources on the thread that created the application."""
import gc
import threading
import tkinter as tk
from tkinter.font import Font

from app.ui import diagnostics

_owners = 0
_restore_automatic = False


class DesktopCollection:
    """While Tk is alive, collect cycles on its thread rather than on sync workers.

    Reference counting still works normally. Only automatic cyclic collection is
    replaced by an allocation-driven main-thread timer; the original GC mode is restored when
    the last desktop closes. Multiple test windows share this lifetime policy.
    """
    def __init__(self, root):
        global _owners, _restore_automatic
        self.root, self.closed = root, False
        if _owners == 0:
            _restore_automatic = gc.isenabled()
            gc.disable()
        _owners += 1
        self.timer = root.after(2000, self.collect)

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
            self.timer = self.root.after(2000, self.collect)

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
