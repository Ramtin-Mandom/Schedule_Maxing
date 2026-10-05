"""
tests/tk_cleanup.py

Tk/CustomTkinter lifecycle hygiene for tests that open real windows.

CustomTkinter keeps class-level registries of every root window it has seen
(ScalingTracker.window_dpi_scaling_dict / window_widgets_dict,
AppearanceModeTracker.app_list) and never removes a destroyed root from some of
them. Each root owns its own Tcl interpreter, so a long test run kept hundreds
of dead interpreters alive and their periodic `after` loops firing at
destroyed windows ("invalid command name ...update"). These helpers:

    cancel_stale_after_jobs(root)    right after a root was destroyed: its
                                     interpreter's leftover timers (CustomTkinter's
                                     update/DPI loops) are cancelled instead of
                                     firing during later tests' event loops; a
                                     root still alive (a close deferred while
                                     workers finish) is left alone;
    purge_destroyed_roots()          after each test: forget destroyed roots
                                     in those registries (and tkinter's
                                     default root), so they can be freed.

Nothing here touches a live window, and the application code is unchanged.
"""

from __future__ import annotations

import sys


def _dead(window) -> bool:
    try:
        return not window.winfo_exists()
    except Exception:  # noqa: BLE001 - "application has been destroyed" and similar mean dead
        return True


def cancel_stale_after_jobs(root) -> None:
    """Cancel the timers left in a destroyed root's interpreter (no-op while the root is still alive)."""
    if not _dead(root):
        return
    try:
        jobs = root.tk.call("after", "info")
    except Exception:  # noqa: BLE001 - the interpreter is already gone
        return
    for job in jobs if isinstance(jobs, tuple) else (jobs,):
        try:
            root.tk.call("after", "cancel", job)
        except Exception:  # noqa: BLE001 - it ran meanwhile
            pass


def purge_destroyed_roots() -> None:
    """Drop destroyed roots from CustomTkinter's and tkinter's global registries (no-op without Tk loaded)."""
    if "customtkinter" in sys.modules:
        from customtkinter.windows.widgets.appearance_mode.appearance_mode_tracker import AppearanceModeTracker
        from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

        for registry in (ScalingTracker.window_dpi_scaling_dict, ScalingTracker.window_widgets_dict):
            for window in [window for window in registry if _dead(window)]:
                del registry[window]
        AppearanceModeTracker.app_list[:] = [app for app in AppearanceModeTracker.app_list if not _dead(app)]
    tkinter = sys.modules.get("tkinter")
    if tkinter is not None and tkinter._default_root is not None and _dead(tkinter._default_root):
        tkinter._default_root = None
