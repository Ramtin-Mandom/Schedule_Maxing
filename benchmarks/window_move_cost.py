"""Opt-in Windows probe: what moving the window costs the compositor, per page arrangement.

    python -m benchmarks.window_move_cost --output benchmarks/results/window_move_cost.json
    python -m benchmarks.window_move_cost --baseline ../before-tree   (before/after, alternating)

Each scenario runs in a fresh child process with a disposable database. The child opens the app, shows
a sequence of pages, waits until the window is quiet, counts the native child windows of the main window,
then measures twice for the same length: the window standing still, and the window moved with
`SetWindowPos` every 16 ms. It reads `\\Process(dwm)\\% Processor Time` (percent of one core) through PDH
and the app's own CPU time. Scenarios alternate between rounds, so background load spreads over all of
them; the report gives the median per scenario.

The moves are not Windows' interactive move loop (no physical dragging). dwm.exe numbers depend on
everything else on the machine; compare scenarios measured in the same run only.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import date
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

#: name -> the steps run in order: a page key is shown; "section:<key>" opens that Settings section (a
#: no-op on a tree without sections, where Settings holds every section); "light" turns light caching on;
#: "empty" shows an empty page; "detach_day" detaches the Day page. The last page stays shown.
SCENARIOS: dict[str, tuple[str, ...]] = {
    "day": ("day",),
    "account": ("day", "account"),
    "guide": ("day", "guide"),
    "settings": ("day", "settings"),
    "settings_scheduling": ("day", "settings", "section:scheduling"),
    "settings_tasks": ("day", "settings", "section:tasks"),
    "settings_after_sections": ("day", "settings", "section:scheduling", "section:tasks"),
    "settings_after_tabs": ("day", "account", "guide", "settings"),
    "settings_after_tour": ("day", "week", "month", "productivity", "budget", "projects", "account", "guide",
                            "settings"),
    "settings_light": ("light", "day", "account", "guide", "settings", "section:scheduling", "section:tasks"),
    "empty": ("empty",),
}

_PDH_FMT_DOUBLE = 0x00000200
_SWP = 0x0001 | 0x0004 | 0x0010  # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE


class _CounterValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class DwmCounter:
    """`\\Process(dwm)\\% Processor Time` averaged between start() and stop()."""

    def __init__(self) -> None:
        self._pdh = ctypes.windll.pdh
        self._query, self._counter = wintypes.HANDLE(), wintypes.HANDLE()
        if self._pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)):
            raise OSError("PdhOpenQueryW failed")
        if self._pdh.PdhAddEnglishCounterW(self._query, "\\Process(dwm)\\% Processor Time", None,
                                           ctypes.byref(self._counter)):
            raise OSError("PdhAddEnglishCounterW failed")

    def start(self) -> None:
        self._pdh.PdhCollectQueryData(self._query)

    def stop(self) -> float:
        self._pdh.PdhCollectQueryData(self._query)
        value = _CounterValue()
        if self._pdh.PdhGetFormattedCounterValue(self._counter, _PDH_FMT_DOUBLE, None, ctypes.byref(value)):
            return float("nan")
        return value.doubleValue

    def close(self) -> None:
        self._pdh.PdhCloseQuery(self._query)


def child_windows(hwnd: int) -> int:
    """Native windows below `hwnd` (every level)."""
    count = 0

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def counted(_child, _param):
        nonlocal count
        count += 1
        return True

    ctypes.windll.user32.EnumChildWindows(wintypes.HWND(hwnd), counted, 0)
    return count


def run_scenario(name: str, seconds: float, tasks: int) -> dict:
    """Child process: one scenario, printed as one JSON line."""
    try:
        from tests import window_placement
        origin = window_placement.install() or (0, 0)
    except ImportError:
        origin = (0, 0)
    from app.app import ScheduleOptimizerApp
    from app.planning.models import Task
    from app.ui.app_services import open_app_services
    import customtkinter
    from config import settings

    steps = SCENARIOS[name]
    with tempfile.TemporaryDirectory(prefix="schedule-move-probe-") as temp:
        path = Path(temp)
        settings.BACKEND_URL = None  # never contact the configured backend
        day = date(2026, 9, 21)
        services = open_app_services(path / "probe.db", timezone="UTC", project_root=temp, background_sync=False)
        for index in range(tasks):
            assert services.planning_controller.add_or_update_task(Task(
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"window-move-probe:{index}"), name=f"Synthetic {index}",
                category="study", estimated_duration_minutes=15, points=1 + index % 10,
                preferred_dates=[day])).ok
        services.close()
        root = ScheduleOptimizerApp(db_path=path / "probe.db", ui_settings_path=path / "ui.json", project_root=temp,
                                    timezone="UTC", today=day, background_sync=False, storage="local")
        root.geometry(f"1400x880+{origin[0] + 20}+{origin[1] + 20}")
        result: dict = {"scenario": name}
        dwm = DwmCounter()
        frame = int(root.wm_frame(), 16)
        user32 = ctypes.windll.user32

        def settle(then, waited=0.0):
            # Quiet: no idle callback pending, no worker running (pages build rows in idle slices).
            busy = any(root.tk.splitlist(root.tk.call("after", "info", ident))[-1] == "idle"
                       for ident in root.tk.splitlist(root.tk.call("after", "info")))
            if (busy or root.services.registry.active) and waited < 4:
                root.after(100, settle, then, waited + 0.1)
            else:
                root.after(1500, then)

        def show(index=0):
            if index == len(steps):
                settle(measure)
                return
            step = steps[index]
            if step == "light":
                root.set_light_caching(True)
            elif step == "empty":
                root.shell.add_page("probe", customtkinter.CTkFrame(root.shell.host))
                root.show_page("probe")
            elif step == "detach_day":
                root.shell._detach(root.pages["day"])
            elif step.startswith("section:"):
                settings_page = root.pages["settings"]
                if hasattr(type(settings_page.page), "open_section"):
                    settings_page.open_section(step.split(":", 1)[1])
            else:
                root.show_page(step)
            root.after(400, lambda: settle(lambda: show(index + 1)))

        def measure():
            result["child_windows"] = child_windows(frame)
            result["detached_pages"] = len(root.shell._detached)
            pool = getattr(root.shell, "pool", None)
            result["held_detached"] = len(pool.held) if pool is not None else None
            result["built_pages"] = sorted(key for key, page in root.pages.items() if getattr(page, "built", True))
            dwm.start()
            started = time.perf_counter()
            root.after(int(seconds * 1000), still_done, started)

        def still_done(started):
            result["dwm_still"] = round(dwm.stop(), 2)
            dwm.start()
            move(time.perf_counter(), time.process_time(), 0)

        def move(started, cpu, n):
            elapsed = time.perf_counter() - started
            if elapsed < seconds:
                user32.SetWindowPos(wintypes.HWND(frame), None, origin[0] + 20 + n % 40 * 4,
                                    origin[1] + 20 + n % 25 * 3, 0, 0, _SWP)
                root.after(16, move, started, cpu, n + 1)
                return
            result["dwm_moving"] = round(dwm.stop(), 2)
            result["app_cpu_moving"] = round((time.process_time() - cpu) / elapsed * 100, 2)
            result["moves"] = n
            dwm.close()
            root.after(0, root._on_close)

        root.after(800, show)
        root.mainloop()
    print("RESULT " + json.dumps(result), flush=True)
    return result


def _child(tree: Path, name: str, args) -> dict | None:
    """One scenario in a fresh process whose `app` package is the one in `tree`."""
    import os

    env = dict(os.environ, PYTHONPATH=str(tree))
    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", name, "--seconds",
                                str(args.seconds), "--tasks", str(args.tasks)], cwd=tree, env=env,
                               capture_output=True, text=True, timeout=300)
    line = next((line for line in completed.stdout.splitlines() if line.startswith("RESULT ")), None)
    if line is None:
        print(f"{name}: failed\n{completed.stderr[-2000:]}", flush=True)
        return None
    return json.loads(line[len("RESULT "):])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=6)
    parser.add_argument("--tasks", type=int, default=50)
    parser.add_argument("--only", help="Comma-separated scenario names")
    parser.add_argument("--baseline", type=Path,
                        help="Another source tree (e.g. `git worktree add` of the previous commit): every scenario "
                             "also runs there, alternating with this tree, as before/<scenario>")
    parser.add_argument("--scenario", help=argparse.SUPPRESS)  # child process
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Windows only (dwm.exe and PDH)")
    if args.scenario:
        run_scenario(args.scenario, args.seconds, args.tasks)
        return
    trees = [("after" if args.baseline else "this", Path.cwd())]
    if args.baseline:
        trees.insert(0, ("before", args.baseline.resolve()))
    names = args.only.split(",") if args.only else list(SCENARIOS)
    jobs = [(label, tree, name) for name in names for label, tree in trees]
    runs: dict[str, list[dict]] = {f"{label}/{name}": [] for label, _tree, name in jobs}
    for round_index in range(args.rounds):
        for label, tree, name in (jobs if round_index % 2 == 0 else list(reversed(jobs))):
            run = _child(tree, name, args)
            if run is None:
                continue
            runs[f"{label}/{name}"].append(run)
            print(f"round {round_index + 1} {label}/{name}: {run['child_windows']} windows, dwm still "
                  f"{run['dwm_still']}% moving {run['dwm_moving']}%, app {run['app_cpu_moving']}%", flush=True)
    summary = {}
    for key, items in runs.items():
        if not items:
            continue
        summary[key] = {
            "child_windows": statistics.median(item["child_windows"] for item in items),
            "detached_pages": items[-1]["detached_pages"],
            "held_detached": items[-1].get("held_detached"),
            "built_pages": items[-1]["built_pages"],
            "dwm_still_median": round(statistics.median(item["dwm_still"] for item in items), 1),
            "dwm_moving_median": round(statistics.median(item["dwm_moving"] for item in items), 1),
            "dwm_added_median": round(statistics.median(item["dwm_moving"] - item["dwm_still"] for item in items), 1),
            "app_cpu_moving_median": round(statistics.median(item["app_cpu_moving"] for item in items), 1),
            "runs": len(items),
        }
    print(f"{'scenario':34} {'windows':>8} {'dwm still':>10} {'dwm moving':>11} {'added':>7} {'app':>6}")
    for key, item in summary.items():
        print(f"{key:34} {item['child_windows']:>8} {item['dwm_still_median']:>9}% {item['dwm_moving_median']:>10}%"
              f" {item['dwm_added_median']:>6}% {item['app_cpu_moving_median']:>5}%")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({
            "environment": {"python": platform.python_version(), "os": platform.platform()},
            "seconds": args.seconds, "tasks": args.tasks, "rounds": args.rounds,
            "baseline": str(args.baseline) if args.baseline else None,
            "physical_drag_performed": False, "summary": summary, "runs": runs}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
