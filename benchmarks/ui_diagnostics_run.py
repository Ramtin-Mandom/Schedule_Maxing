"""Drive the desktop app through comparable scenarios with app/ui/diagnostics.py switched on.

    python -m benchmarks.ui_diagnostics_run --output benchmarks/results/ui_diagnostics.json
    python -m benchmarks.ui_diagnostics_run --native --output ...   # + Windows' real move/size loop, injected drag
    python -m benchmarks.ui_diagnostics_run --manual --output ...   # drag/resize by hand, then close the window

Synthetic tasks in a temporary database and a synthetic sync transport: no user data,
no network. Unlike benchmarks/desktop_responsiveness.py nothing in Tk is patched; the
numbers come from the application's own opt-in hooks.

The scripted "move"/"resize" phases call wm geometry: they exercise the application's
<Configure> handling but NOT Windows' modal move/size loop. --native enters that loop for
real: injected mouse input drags the title bar, then the bottom-right corner, for about
three seconds each (the pointer is taken over meanwhile and put back afterwards). Only --manual measures a physical mouse drag.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import date
from pathlib import Path

PAGES = ["day", "week", "month", "productivity", "projects", "settings"]
SCRIPTED = ["idle", "move", "resize", "navigation", "generate", "sync", "idle_after"]


def _origin() -> tuple[int, int]:
    try:
        from tests.window_placement import window_origin
        return window_origin() or (80, 60)
    except Exception:  # noqa: BLE001 - placement is a convenience
        return 80, 60


def _native_loop(hwnd: int, move: bool, done: threading.Event) -> None:
    """Drag the title bar (or the bottom-right corner) with injected mouse input (Win32 only; never calls Tk)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes, user32.WindowFromPoint.restype = [wintypes.POINT], wintypes.HWND
    user32.GetAncestor.argtypes, user32.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    start = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(start))
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    x, y = (rect.left + 200, rect.top + 14) if move else (rect.right - 4, rect.bottom - 4)
    seen: set[tuple[int, int, int, int]] = set()
    try:
        user32.SetCursorPos(x, y)
        time.sleep(.15)
        if user32.GetAncestor(user32.WindowFromPoint(wintypes.POINT(x, y)), 2) != hwnd:  # GA_ROOT
            print("native loop skipped: another window covers the probe window", flush=True)
            return
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # left button down: Windows enters its move/size loop
        try:
            for step in range(90):  # about three seconds of continuous dragging
                direction = 1 if step // 15 % 2 == 0 else -1
                user32.mouse_event(0x0001, 6 * direction, 3 * direction, 0, 0)  # relative move
                time.sleep(.033)
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                seen.add((rect.left, rect.top, rect.right, rect.bottom))
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # left button up
        time.sleep(.3)
        full_drag = wintypes.BOOL()
        user32.SystemParametersInfoW(0x0026, 0, ctypes.byref(full_drag), 0)  # SPI_GETDRAGFULLWINDOWS
        # One rectangle would mean the window did not follow the pointer live.
        print(f"native {'move' if move else 'resize'}: {len(seen)} window rectangles seen, "
              f"full-window drag={bool(full_drag.value)}", flush=True)
    finally:
        user32.SetCursorPos(start.x, start.y)
        done.set()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=5, help="minimum duration of each scripted phase")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", help="comma-separated scripted phases")
    parser.add_argument("--native", action="store_true", help="also drag and resize with injected mouse input")
    parser.add_argument("--manual", action="store_true", help="no script: drag and resize by hand, then close")
    parser.add_argument("--manual-timeout", type=int, default=180)
    parser.add_argument("--page", help="show this page first (e.g. about: an almost empty page, to compare resize cost)")
    parser.add_argument("--profile", type=Path, help="cProfile the scripted phases (slows them; not a baseline)")
    args = parser.parse_args()

    from benchmarks.desktop_responsiveness import LatencyTransport
    from app.app import ScheduleOptimizerApp
    from app.planning.models import Task
    from app.ui import diagnostics
    from app.ui.app_services import open_app_services
    from config import settings

    with tempfile.TemporaryDirectory(prefix="schedule-diagnostics-") as temp:
        path = Path(temp)
        settings.BACKEND_URL = None  # never contact a configured backend
        day = date(2026, 9, 21)
        services = open_app_services(path / "probe.db", timezone="UTC", project_root=temp, background_sync=False)
        for index in range(args.tasks):
            result = services.planning_controller.add_or_update_task(Task(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"schedule-diagnostics:{index}"), name=f"Synthetic {index}",
                category="study", estimated_duration_minutes=15, priority=1 + index % 10, preferred_dates=[day]))
            assert result.ok, "Synthetic fixture creation failed"
        services.close()

        os.environ[diagnostics.ENV_VAR] = str(path / "report.json")
        started = time.perf_counter()
        root = ScheduleOptimizerApp(db_path=path / "probe.db", ui_settings_path=path / "ui.json", project_root=temp,
                                    timezone="UTC", today=day, background_sync=False, storage="local")
        startup_ms = (time.perf_counter() - started) * 1000
        x, y = _origin()
        root.geometry(f"1100x760+{x}+{y}")
        recorder = diagnostics.active()
        assert recorder is not None
        profiler = None
        if args.profile:
            import cProfile
            profiler = cProfile.Profile()

        phases = args.only.split(",") if args.only else list(SCRIPTED)
        if args.native and sys.platform == "win32":
            phases += ["native_move_request", "native_resize_request"]

        def idle() -> bool:
            board_pending = getattr(root.pages["day"].status_board, "_render_timer", None)
            return not root.services.registry.outstanding and board_pending is None

        def begin(index: int = 0) -> None:
            if index == len(phases):
                diagnostics.mark("shutdown")
                if profiler:
                    profiler.disable()
                root.after(0, root._on_close)
                return
            phase = phases[index]
            diagnostics.mark(phase)
            print(phase, flush=True)
            deadline = time.perf_counter() + args.seconds
            native_done = threading.Event()

            def step(n: int = 0) -> None:
                if time.perf_counter() >= deadline and (not phase.startswith("native") or native_done.is_set()):
                    if idle():
                        begin(index + 1)
                    else:
                        root.after(100, step, n)
                    return
                if phase == "move":
                    root.geometry(f"+{x + n % 30 * 3}+{y + n % 20 * 2}")
                elif phase == "resize":
                    root.geometry(f"{1000 + n % 30 * 10}x{700 + n % 15 * 5}")
                elif phase == "navigation" and n % 5 == 0:
                    root.show_page(PAGES[n // 5 % len(PAGES)])
                elif phase == "generate" and n == 0:
                    root.show_page("day")
                    root.pages["day"].make_schedule()
                elif phase == "sync" and n == 0:
                    root.services.sync_service.set_transport(LatencyTransport())
                    root.services.sync_service.sign_in("", "")
                    root.sync_now()
                elif phase.startswith("native") and n == 0:
                    import ctypes
                    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
                    root.attributes("-topmost", True)  # the injected drag must land on this window
                    root.lift()
                    threading.Thread(target=_native_loop, daemon=True,
                                     args=(hwnd, phase == "native_move_request", native_done)).start()
                root.after(100, step, n + 1)

            step()

        if args.manual:
            diagnostics.mark("manual")
            root.after(args.manual_timeout * 1000, root._on_close)
        else:
            if args.page:
                root.show_page(args.page)
            if profiler:
                profiler.enable()
            root.after(1000, begin)
        root.mainloop()
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(args.profile))
        report = json.loads((path / "report.json").read_text(encoding="utf-8"))
        report.update({"tasks": args.tasks, "phase_seconds": args.seconds, "startup_ms": round(startup_ms, 1),
                       "manual": args.manual, "page": args.page, "native_injected_drag": args.native,
                       "profiled": bool(args.profile),
                       "physical_mouse_drag": "only if performed by hand in --manual"})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
