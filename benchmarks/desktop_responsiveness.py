"""Opt-in disposable desktop probe: python -m benchmarks.desktop_responsiveness.

No production hooks. Logs contain timings/counts and function names, never arguments,
task contents, credentials or SQL. Automated geometry changes are NOT physical dragging.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import date
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import tempfile
import threading
import time
import tkinter as tk
import types
import uuid
from unittest.mock import patch


def source_hashes():
    """Detect concurrent source edits without retaining source or user data."""
    return {str(path).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ("app", "config") for path in sorted(Path(folder).rglob("*.py"))}


def reference_behavior():
    """Process-local ablation of this task's fixes on the CURRENT UI, never a file revert."""
    import customtkinter as ctk
    from app.ui.paint_widgets import AppOptionMenu, AppScrollableFrame, AppTextbox
    from app.ui.tk_lifecycle import DesktopCollection
    from app.ui.day_page import DaySchedulePage
    from app.ui.task_editor import DependencyPicker

    patches = ExitStack()
    patches.enter_context(patch.object(AppOptionMenu, "_draw", ctk.CTkOptionMenu._draw))
    patches.enter_context(patch.object(AppScrollableFrame, "__init__", ctk.CTkScrollableFrame.__init__))
    patches.enter_context(patch.object(AppTextbox, "__init__", ctk.CTkTextbox.__init__))

    def collect(collection):
        if not collection.closed:
            gc.collect()
            collection.timer = collection.root.after(2000, collection.collect)

    original_chips = DaySchedulePage._show_available

    def rebuild_chips(page, snapshot):
        for chip in page.chips:
            chip.destroy()
        page.chips = []
        original_chips(page, snapshot)

    original_choices = DependencyPicker.set_choices

    def rewrite_choices(picker, choices, selected=None):
        # Reproduce exactly one variable write per choice (including no-op writes).
        # Extra value reads are a small reference-harness overhead, disclosed in the report.
        old_vars = {key: (var, var.get()) for key, var in picker.vars.items()}
        original_choices(picker, choices, selected)
        for key, var in picker.vars.items():
            old_var, old_value = old_vars.get(key, (None, False))
            value = var.get()
            if (var is old_var and value == old_value) or (var is not old_var and not value):
                var.set(value)

    patches.enter_context(patch.object(DesktopCollection, "collect", collect))
    patches.enter_context(patch.object(DaySchedulePage, "_show_available", rebuild_chips))
    patches.enter_context(patch.object(DependencyPicker, "set_choices", rewrite_choices))
    return patches


def distribution(values):
    ordered = sorted(values)
    return {"n": len(ordered), "p95_ms": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else 0,
            "max_ms": max(ordered, default=0)}


def baseline_ui(path):
    """Load the hash-checked pre-change checkout snapshot only inside this probe."""
    import app.app as app_module
    import app.ui.day_page as day_module
    import app.ui.task_status_board as board_module

    sources = json.loads(path.read_text(encoding="utf-8"))
    patches = ExitStack()
    try:
        for filename, target, name in (("app/ui/task_status_board.py", board_module, "TaskStatusBoard"),
                                       ("app/ui/day_page.py", day_module, "DaySchedulePage")):
            item = sources[filename]
            assert hashlib.sha256(item["source"].encode("utf-8")).hexdigest() == item["sha256"]
            module = types.ModuleType("benchmark_baseline_" + name)
            exec(compile(item["source"], "baseline/" + filename, "exec"), module.__dict__)
            patches.enter_context(patch.object(target, name, getattr(module, name)))
        patches.enter_context(patch.object(app_module, "DaySchedulePage", day_module.DaySchedulePage))
    except BaseException:
        patches.close()
        raise
    return patches


class LatencyTransport:
    """Synthetic empty-account sync; no network and no personal account access."""
    base_url = "http://diagnostic.invalid"

    def login(self, email, password):
        from app.sync.transport import LoginResult
        return LoginResult("synthetic", "00000000-0000-0000-0000-000000000001", "probe@example.invalid")

    def push(self, token, operations):
        assert not operations, "Probe must not upload records"
        return []

    def pull(self, token, after, limit):
        from app.sync.transport import PullPage
        threading.Event().wait(2)  # simulated network latency, only in the sync worker
        return PullPage([], after, False)


class Probe:
    def __init__(self):
        self.phase = "startup"
        self.callbacks = defaultdict(list)
        self.delays = defaultdict(list)
        self.counts = defaultdict(Counter)
        self.samples = defaultdict(list)
        self.positions = defaultdict(list)
        self.original = tk.CallWrapper.__call__
        self.original_idle = tk.Misc.update_idletasks
        self.original_geometry = tk.Wm.geometry
        self.original_wm_geometry = tk.Wm.wm_geometry
        probe = self

        def geometry(widget, value=None):
            if value is not None:
                probe.counts[probe.phase]["explicit_geometry_calls"] += 1
            return probe.original_geometry(widget, value)

        tk.Wm.geometry = tk.Wm.wm_geometry = geometry

        def measured(wrapper, *args):
            phase = probe.phase
            func = wrapper.func
            # Tk.after wraps the original function in callit.
            if getattr(func, "__qualname__", "").endswith(".<locals>.callit"):
                for variable, cell in zip(func.__code__.co_freevars, func.__closure__ or ()):
                    if variable == "func":
                        func = cell.cell_contents
                        break
            name = getattr(func, "__qualname__", type(func).__name__)
            start = time.perf_counter()
            try:
                return probe.original(wrapper, *args)
            finally:
                probe.callbacks[phase, name].append((time.perf_counter() - start) * 1000)

        tk.CallWrapper.__call__ = measured

        def idle(widget):
            start = time.perf_counter()
            try:
                return probe.original_idle(widget)
            finally:
                probe.callbacks[probe.phase, "update_idletasks"].append((time.perf_counter() - start) * 1000)

        tk.Misc.update_idletasks = idle

    def attach(self, root):
        self.root = root
        root.bind_all("<Configure>", self.configure, add="+")
        self.deadline = time.perf_counter() + .05
        self.cpu, self.wall = time.process_time(), time.perf_counter()
        self.timer = root.after(50, self.tick)

    def configure(self, event):
        self.counts[self.phase]["configure_all"] += 1
        widget = event.widget
        names = []
        while isinstance(widget, tk.Misc):
            names.append(type(widget).__name__)
            widget = getattr(widget, "master", None)
        self.counts[self.phase]["tree:" + "/".join(reversed(names))] += 1
        if event.widget is self.root:
            self.counts[self.phase]["configure_root"] += 1
            self.positions[self.phase].append([time.perf_counter(), event.x, event.y, event.width, event.height])

    def tick(self):
        now, cpu = time.perf_counter(), time.process_time()
        self.delays[self.phase].append(max(0, now - self.deadline) * 1000)
        pending = self.root.tk.call("after", "info")
        polls = sum("poll" in str(self.root.tk.call("after", "info", ident)) for ident in pending)
        services = getattr(self.root, "services", None)
        self.samples[self.phase].append({"cpu_percent_one_core": (cpu - self.cpu) / (now - self.wall) * 100,
                                         "workers": services.registry.active if services else 0, "pending_polls": polls,
                                         "geometry": self.root.winfo_geometry(),
                                         "tk_scaling": float(self.root.tk.call("tk", "scaling")),
                                         "window_scaling": self.root._get_window_scaling(),
                                         "pending_after": len(pending)})
        self.cpu, self.wall = cpu, now
        self.deadline = time.perf_counter() + .05
        self.timer = self.root.after(50, self.tick)

    def report(self):
        phases = sorted(set(self.delays) | {p for p, _ in self.callbacks})
        return {p: {"delay": distribution(self.delays[p]), "counts": self.counts[p],
                    "callbacks": {name: distribution(v) for (phase, name), v in self.callbacks.items() if phase == p},
                    "raw_delay_ms": self.delays[p], "raw_samples": self.samples[p],
                    "root_positions": self.positions[p],
                    "raw_callbacks_ms": {name: v for (phase, name), v in self.callbacks.items() if phase == p}}
                for p in phases}

    def close(self):
        tk.CallWrapper.__call__ = self.original
        tk.Misc.update_idletasks = self.original_idle
        tk.Wm.geometry = self.original_geometry
        tk.Wm.wm_geometry = self.original_wm_geometry


def manual_keys(root, probe):
    """Separate physical movement from resizing; keys only label observations."""
    for key, phase in (("F6", "manual_move"), ("F7", "manual_resize"), ("F8", "manual_idle")):
        root.bind(f"<{key}>", lambda event, phase=phase: setattr(probe, "phase", phase), add="+")
    probe.phase = "manual_idle"


def display_info(root):
    info = {"screen": [root.winfo_screenwidth(), root.winfo_screenheight()],
            "tk_scaling": float(root.tk.call("tk", "scaling")),
            "window_scaling": root._get_window_scaling()}
    if platform.system() == "Windows":
        import ctypes
        info["monitor_count"] = ctypes.windll.user32.GetSystemMetrics(80)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=10)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manual", action="store_true", help="Leave window open for physical dragging; close to save.")
    parser.add_argument("--experiment", choices=["normal", "light", "no-board", "no-chips"], default="normal")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--only", help="Comma-separated phases for controlled experiments")
    parser.add_argument("--minimal", action="store_true", help="Minimal CTk manual comparison window")
    parser.add_argument("--manual-timeout", type=int, default=180, help="Save manual recording after this many seconds")
    parser.add_argument("--baseline-ui", type=Path, help="Exact pre-change UI snapshot for matched comparisons")
    parser.add_argument("--fixed-work", action="store_true", help="Deliver 80 moves, 24 resizes, and all seven pages")
    parser.add_argument("--reference-behavior", action="store_true",
                        help="Compare this current UI with only the responsiveness fixes disabled in this process.")
    args = parser.parse_args()
    import faulthandler
    faulthandler.dump_traceback_later(180, repeat=True)
    # Import before instrumentation so import costs do not masquerade as Tk callbacks.
    hashes_before = source_hashes()
    from app.app import ScheduleOptimizerApp
    from app.planning.models import Task
    from app.ui.app_services import open_app_services
    from app.ui.layout import Coalescer
    import customtkinter
    from config import settings
    print("Imports ready", flush=True)

    if args.minimal:
        probe = Probe()
        try:
            root = customtkinter.CTk()
            root.title("Minimal CTk drag comparison — F6 move / F7 resize / F8 idle")
            root.geometry("1100x760+80+60")
            probe.attach(root)
            manual_keys(root, probe)
            environment = display_info(root)
            root.after(args.manual_timeout * 1000, root.destroy)
            root.mainloop()
            args.output.write_text(json.dumps({"display": environment, "phases": probe.report(),
                                               "physical_drag_performed": False}, indent=2), encoding="utf-8")
        finally:
            probe.close()
            faulthandler.cancel_dump_traceback_later()
        return

    with tempfile.TemporaryDirectory(prefix="schedule-probe-") as temp:
        path = Path(temp)
        settings.BACKEND_URL = None  # never contact the user's configured backend
        services = open_app_services(path / "probe.db", timezone="UTC", project_root=temp, background_sync=False)
        day = date(2026, 9, 21)
        for index in range(args.tasks):
            result = services.planning_controller.add_or_update_task(Task(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"schedule-probe:{index}"),
                name=f"Synthetic {index}", category="study", estimated_duration_minutes=15,
                priority=1 + index % 10, preferred_dates=[day]))
            assert result.ok, "Synthetic fixture creation failed"
        services.close()
        print("Fixture ready", flush=True)
        probe = Probe()
        reference = reference_behavior() if args.reference_behavior else ExitStack()
        if args.baseline_ui:
            reference.enter_context(baseline_ui(args.baseline_ui))
        from app.ui.day_page import DaySchedulePage
        from app.ui.task_status_board import TaskStatusBoard
        if args.experiment == "no-board":
            reference.enter_context(patch.object(TaskStatusBoard, "render", lambda *args: None))
        if args.experiment == "no-chips":
            reference.enter_context(patch.object(DaySchedulePage, "_show_available", lambda *args: None))
        original_run = Coalescer._run

        def counted(coalescer):
            probe.counts[probe.phase]["coalescer_runs"] += 1
            probe.counts[probe.phase][coalescer._callback.__qualname__] += 1
            phase, started = probe.phase, time.perf_counter()
            try:
                return original_run(coalescer)
            finally:
                probe.callbacks[phase, "coalesced:" + coalescer._callback.__qualname__].append(
                    (time.perf_counter() - started) * 1000)

        Coalescer._run = counted
        try:
            started = time.perf_counter()
            root = ScheduleOptimizerApp(db_path=path / "probe.db", ui_settings_path=path / "ui.json",
                                        project_root=temp, timezone="UTC", today=day, background_sync=False, storage="local")
            startup_ms = (time.perf_counter() - started) * 1000
            tk_patchlevel = root.tk.call("info", "patchlevel")
            display = display_info(root)
            print("Window ready", flush=True)
            probe.attach(root)
            if args.experiment == "light":
                root.shell.add_page("probe", customtkinter.CTkFrame(root.shell.host))
                root.show_page("probe")
            profiler = None
            if args.profile:
                import cProfile
                profiler = cProfile.Profile()
                profiler.enable()
            phases = ["idle", "move", "resize", "pages", "generate", "sync_unconfigured", "sync_latency", "idle_after"]
            if args.only:
                phases = args.only.split(",")

            def begin(index=0):
                if index == len(phases):
                    probe.phase = "shutdown"
                    if profiler:
                        profiler.disable()
                    root.after(0, root._on_close)
                    return
                probe.phase = phases[index]
                print(probe.phase, flush=True)
                deadline = time.perf_counter() + args.seconds

                def step(n=0):
                    phase = phases[index]
                    count = {"move": 80, "resize": 24, "pages": 61}.get(phase) if args.fixed_work else None
                    finished = n >= count if count is not None else time.perf_counter() >= deadline
                    board_pending = getattr(root.pages["day"].status_board, "_render_timer", None)
                    if finished:
                        if not root.services.registry.active and board_pending is None:
                            begin(index + 1)
                        else:
                            root.after(100, step, n)
                        return
                    if phase == "move":
                        probe.counts[phase]["input_operations"] += 1
                        root.geometry(f"+{80 + n % 30 * 3}+{60 + n % 20 * 2}")
                    elif phase == "resize":
                        probe.counts[phase]["input_operations"] += 1
                        root.geometry(f"{1000 + n % 30 * 10}x{700 + n % 15 * 5}")
                    elif phase == "pages" and n % 10 == 0:
                        probe.counts[phase]["input_operations"] += 1
                        pages = ["day", "week", "month", "productivity", "settings", "projects", "allocation"]
                        root.show_page(pages[n // 10 % len(pages)])
                    elif phase == "generate" and n == 0:
                        root.show_page("day")
                        root.pages["day"].make_schedule()
                    elif phase == "sync_unconfigured" and n == 0:
                        root.sync_now()
                    elif phase == "sync_latency" and n == 0:
                        root.services.sync_service.set_transport(LatencyTransport())
                        root.services.sync_service.sign_in("", "")
                        root.sync_now()
                    root.after(100, step, n + 1)

                step()

            if args.manual:
                manual_keys(root, probe)
                root.after(args.manual_timeout * 1000, root._on_close)
            else:
                root.after(1000, begin)
            root.mainloop()
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(args.profile))
            output = {"environment": {"python": platform.python_version(), "os": platform.platform(),
                                       "tk": tk_patchlevel, "customtkinter": customtkinter.__version__},
                      "tasks": args.tasks, "seconds": args.seconds, "startup_ms": startup_ms,
                      "display": display,
                      "physical_drag_performed": False, "reference_behavior": args.reference_behavior,
                      "experiment": args.experiment,
                      "fixed_work": args.fixed_work,
                      "baseline_ui": str(args.baseline_ui) if args.baseline_ui else None,
                      "source_sha256": hashes_before, "source_unchanged": hashes_before == source_hashes(),
                      "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                      "phases": probe.report()}
            output["effective_source_sha256"] = dict(hashes_before)
            if args.baseline_ui:
                for name, item in json.loads(args.baseline_ui.read_text(encoding="utf-8")).items():
                    output["effective_source_sha256"][name] = item["sha256"]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
        finally:
            Coalescer._run = original_run
            probe.close()
            reference.close()
            faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
