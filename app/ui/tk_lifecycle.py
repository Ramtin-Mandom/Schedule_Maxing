"""Release Tcl-owned resources on the thread that created the application."""
import gc
import threading
import tkinter as tk
from tkinter.font import Font

_owners = 0
_restore_automatic = False


class DesktopCollection:
    """While Tk is alive, collect cycles on its thread rather than on sync workers.

    Reference counting still works normally. Only automatic cyclic collection is
    replaced by a bounded main-thread timer; the original GC mode is restored when
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
            gc.collect()
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
