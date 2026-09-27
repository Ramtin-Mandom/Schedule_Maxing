"""
Test-only placement of Tk windows (not used by the application).

When the tests open real windows (widget tests, the launch probes), they are
placed on the monitor to the LEFT of the primary one, so they do not cover the
main screen while the suite runs. Nothing changes where there is no such
monitor (one screen, CI's virtual display, macOS/Linux) -- the windows then
open where Tk puts them.

    SCHEDULE_MAXING_TEST_WINDOW_ORIGIN="x,y"   place windows at this origin instead
    SCHEDULE_MAXING_TEST_WINDOW_ORIGIN=off      leave placement to Tk

tests/conftest.py installs it for the pytest process and exports the origin,
so the child-process probes (tests/isolation_probe.py,
tests/direct_isolation_probe.py) place their windows the same way.
"""

from __future__ import annotations

import os
import sys

ENV_VAR = "SCHEDULE_MAXING_TEST_WINDOW_ORIGIN"
#: Distance from the monitor's top-left corner.
MARGIN = 40

_installed = False


def _left_monitor_origin() -> tuple[int, int] | None:
    """The top-left of the work area of the monitor left of the primary one (Windows), else None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        try:  # the coordinates Tk uses: customtkinter makes the process per-monitor DPI aware the same way
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        monitors: list[tuple[int, int, int, bool]] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC,
                                           ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def collect(handle, _dc, _rect, _data) -> int:
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                work = info.rcWork
                monitors.append((work.left, work.top, work.right, bool(info.dwFlags & 1)))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    except Exception:  # noqa: BLE001 - placement is a convenience; never fail a test over it
        return None
    primary = next((m for m in monitors if m[3]), None)
    if primary is None:
        return None
    left_of_primary = [m for m in monitors if not m[3] and m[2] <= primary[0]]
    if not left_of_primary:
        return None
    nearest = max(left_of_primary, key=lambda m: m[2])  # the one directly to the left
    return nearest[0] + MARGIN, nearest[1] + MARGIN


def window_origin() -> tuple[int, int] | None:
    configured = os.environ.get(ENV_VAR, "").strip()
    if configured.lower() == "off":
        return None
    if configured:
        try:
            x, y = (int(part) for part in configured.split(","))
            return x, y
        except ValueError:
            return None
    return _left_monitor_origin()


def install() -> tuple[int, int] | None:
    """Place every new Tk root window at window_origin() (idempotent); returns the origin used."""
    global _installed
    origin = window_origin()
    if origin is None or _installed:
        return origin
    import tkinter

    original = tkinter.Tk.__init__
    x, y = origin

    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            # Tk's own command (a CTk window is not set up yet here): position only; the size stays its own.
            self.tk.call("wm", "geometry", self._w, f"+{x}+{y}")
        except tkinter.TclError:
            pass

    tkinter.Tk.__init__ = __init__
    _installed = True
    os.environ[ENV_VAR] = f"{x},{y}"  # child-process probes place their windows the same way
    return origin


def place(geometry: str) -> str:
    """A test's fixed window geometry ("WxH+X+Y") shifted onto the placement monitor (unchanged without one)."""
    import re

    origin = window_origin()
    match = re.fullmatch(r"(\d+x\d+)\+(-?\d+)\+(-?\d+)", geometry)
    if origin is None or match is None:
        return geometry
    size, x, y = match.group(1), int(match.group(2)), int(match.group(3))
    return f"{size}+{x + origin[0] - MARGIN}+{y + origin[1] - MARGIN}"
