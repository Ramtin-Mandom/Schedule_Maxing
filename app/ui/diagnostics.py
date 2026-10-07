"""
app/ui/diagnostics.py

Opt-in measurements of why the desktop window lags while it is dragged,
resized or used. Nothing here runs unless SCHEDULE_MAXING_UI_DIAGNOSTICS is
set (to a JSON output path, or to 1 for a file in the temp directory): every
hook in the application is `with diagnostics.span(...)`, which is a shared
no-op object while diagnostics are off.

What is measured (time.perf_counter throughout):

- heartbeat lateness: a 20 ms `after` timer on the Tk thread; how late each
  tick runs is how long the event loop could not serve a callback.
- callback durations: the spans placed in app/app.py, background.py,
  layout.py, shell.py and tk_lifecycle.py.
- garbage collection: duration and generation, from gc.callbacks.
- layout/redraw counts: <Configure> and <Expose> events, counted by a Tcl
  script appended to the `all` binding tag (no Python call per event).
- active background jobs, sampled on every tick.

A late tick is attributed to one cause: `app` when the measured spans or
collections of that interval explain at least half of it, `native_modal`
when Windows was inside its own move/size loop (GetGUIThreadInfo's
GUI_INMOVESIZE -- that loop runs inside DefWindowProc and delays Tk whether
or not the application is busy), `worker_contention` when a background job
was running (it shares the interpreter lock with the Tk thread), otherwise
`unattributed` (Tk/CustomTkinter redraw and geometry work, which is not
instrumented because Tk is not monkey-patched).

Bounded: one fixed-size summary per (phase, name), the latest 256 samples of
each for percentiles and the latest 200 slow events. Names are code-defined
labels and function names only -- never arguments, task contents, account
data or SQL.
"""

from __future__ import annotations

import gc
import json
import math
import os
import platform
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path

ENV_VAR = "SCHEDULE_MAXING_UI_DIAGNOSTICS"

HEARTBEAT_MS = 20
#: A tick this late is a visible stall (about two display frames beyond Windows' ~16 ms timer granularity).
LATE_MS = 33.0
#: A span or collection this long is logged individually.
SLOW_MS = 16.0
_SAMPLES = 256
_SLOW_EVENTS = 200
_EPISODES = 50
_EPISODE_TICKS = 4096


class _NoSpan:
    __slots__ = ()

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc) -> bool:
        return False


_NO_SPAN = _NoSpan()
_active: UIDiagnostics | None = None


def label(target) -> str:
    """The code name of a callback (never its arguments or anything it holds)."""
    return getattr(target, "__qualname__", type(target).__name__)


def span(name: str, target=None):
    """Time a block under `name` (plus the code name of `target`); a no-op unless diagnostics are on."""
    recorder = _active
    if recorder is None:
        return _NO_SPAN
    return _Span(recorder, name if target is None else f"{name}:{label(target)}")


def count(name: str, amount: int = 1) -> None:
    if _active is not None:
        _active.count(name, amount)


def mark(phase: str) -> None:
    """Label what happens next (e.g. "resize"), so scenarios can be compared."""
    if _active is not None:
        _active.set_phase(phase)


def active() -> UIDiagnostics | None:
    return _active


def install_from_env(root, *, jobs: Callable[[], int] = lambda: 0) -> UIDiagnostics | None:
    """Start diagnostics for `root` when the environment asks for them; otherwise do nothing."""
    value = os.environ.get(ENV_VAR, "").strip()
    if not value or value.lower() in ("0", "false", "off", "no"):
        return None
    if value.lower() in ("1", "true", "on", "yes"):
        output = Path(tempfile.gettempdir()) / "schedule-maxing-ui-diagnostics.json"
    else:
        output = Path(value)
    return install(root, jobs=jobs, output=output)


def install(root, *, jobs: Callable[[], int] = lambda: 0, output: Path | None = None,
            clock: Callable[[], float] = time.perf_counter) -> UIDiagnostics:
    global _active
    if _active is not None:
        _active.close()
    _active = UIDiagnostics(root, jobs=jobs, output=output, clock=clock)
    return _active


class _Span:
    __slots__ = ("_recorder", "_name", "_start")

    def __init__(self, recorder: UIDiagnostics, name: str) -> None:
        self._recorder, self._name = recorder, name

    def __enter__(self) -> None:
        self._start = self._recorder.enter()

    def __exit__(self, *_exc) -> bool:
        self._recorder.leave(self._name, self._start)
        return False


class _Stat:
    __slots__ = ("n", "total", "max", "recent")

    def __init__(self) -> None:
        self.n, self.total, self.max = 0, 0.0, 0.0
        self.recent: deque[float] = deque(maxlen=_SAMPLES)

    def add(self, value: float) -> None:
        self.n += 1
        self.total += value
        self.max = max(self.max, value)
        self.recent.append(value)

    def summary(self) -> dict:
        ordered = sorted(self.recent)

        def rank(fraction: float) -> float:
            return round(ordered[math.ceil(len(ordered) * fraction) - 1], 3) if ordered else 0.0

        return {"n": self.n, "total_ms": round(self.total, 3), "mean_ms": round(self.total / self.n, 3) if self.n else 0.0,
                "p50_ms": rank(.5), "p95_ms": rank(.95), "max_ms": round(self.max, 3)}


def _move_size_probe() -> Callable[[], bool]:
    """Whether this thread is inside Windows' own move/size loop (always False elsewhere)."""
    if sys.platform != "win32":
        return lambda: False
    try:
        import ctypes
        from ctypes import wintypes

        class GuiThreadInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                        ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                        ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND),
                        ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GuiThreadInfo)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        thread = ctypes.WinDLL("kernel32").GetCurrentThreadId()
        info = GuiThreadInfo()
        info.cbSize = ctypes.sizeof(GuiThreadInfo)

        def in_move_size() -> bool:
            return bool(user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.flags & 0x2)  # GUI_INMOVESIZE

        in_move_size()
        return in_move_size
    except Exception:  # noqa: BLE001 - diagnostics never stop the application
        return lambda: False


class UIDiagnostics:
    def __init__(self, root, *, jobs: Callable[[], int] = lambda: 0, output: Path | None = None,
                 clock: Callable[[], float] = time.perf_counter, move_size: Callable[[], bool] | None = None) -> None:
        self.root, self.output = root, output
        self._jobs, self._clock = jobs, clock
        self._move_size = move_size or _move_size_probe()
        self._owner = threading.get_ident()
        self._lock = threading.RLock()  # workers record too; re-entrant because a collection can start inside it
        self._stats: dict[tuple[str, str], _Stat] = {}
        self._counts: dict[str, Counter] = {}
        self.slow: deque[dict] = deque(maxlen=_SLOW_EVENTS)
        self.episodes: deque[dict] = deque(maxlen=_EPISODES)
        self.phase = "startup"
        self.closed = False
        self._started = clock()
        self._depth = 0
        self._busy = 0.0  # seconds of measured Tk-thread work since the last tick
        self._gc_start: float | None = None
        self._episode: dict | None = None
        self._events = (0, 0, 0)
        self._tk_counters = self._bind_event_counters()
        gc.callbacks.append(self._on_gc)
        self._due = clock() + HEARTBEAT_MS / 1000
        self._timer = root.after(HEARTBEAT_MS, self._tick)

    # -- recording --------------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def count(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts.setdefault(self._span_phase(), Counter())[name] += amount

    def enter(self) -> float:
        if threading.get_ident() == self._owner:
            self._depth += 1
        return self._clock()

    def leave(self, name: str, start: float) -> None:
        elapsed = self._clock() - start
        if threading.get_ident() == self._owner:
            self._depth -= 1
            if self._depth == 0:  # nested spans are already inside their outermost one
                self._busy += elapsed
        self._record(self._span_phase(), name, elapsed * 1000, slow_at=SLOW_MS)

    def _span_phase(self) -> str:
        return "native_move_size" if self._episode is not None else self.phase

    def _record(self, phase: str, name: str, ms: float, *, slow_at: float | None = None, **detail) -> None:
        with self._lock:
            stat = self._stats.get((phase, name))
            if stat is None:
                stat = self._stats[phase, name] = _Stat()
            stat.add(ms)
            if slow_at is not None and ms >= slow_at:
                self.slow.append({"at_s": round(self._clock() - self._started, 3), "phase": phase, "name": name,
                                  "ms": round(ms, 3), **detail})

    def _on_gc(self, when: str, info: dict) -> None:
        if threading.get_ident() != self._owner:
            if when == "stop":
                self.count("gc.off_tk_thread")
            return
        if when == "start":
            self._gc_start = self._clock()
        elif self._gc_start is not None:
            elapsed, self._gc_start = self._clock() - self._gc_start, None
            if self._depth == 0:
                self._busy += elapsed
            self._record(self._span_phase(), f"gc.gen{info.get('generation')}", elapsed * 1000, slow_at=SLOW_MS,
                         collected=info.get("collected"))

    # -- heartbeat --------------------------------------------------------------------

    def _bind_event_counters(self) -> bool:
        tk = getattr(self.root, "tk", None)
        if tk is None:
            return False
        try:
            tk.eval("array set ::smdiag {configure 0 root 0 expose 0}")
            tk.eval('bind all <Configure> {+incr ::smdiag(configure); if {"%W" eq "."} {incr ::smdiag(root)}}')
            tk.eval("bind all <Expose> {+incr ::smdiag(expose)}")
            return True
        except Exception:  # noqa: BLE001
            return False

    def _read_event_counters(self) -> None:
        if not self._tk_counters:
            return
        try:
            now = tuple(int(part) for part in
                        self.root.tk.eval("list $::smdiag(configure) $::smdiag(root) $::smdiag(expose)").split())
        except Exception:  # noqa: BLE001 - the interpreter is going away
            return
        before, self._events = self._events, now
        counts = self._counts.setdefault(self._span_phase(), Counter())
        for name, new, old in zip(("configure_events", "root_configure_events", "expose_events"), now, before):
            if new != old:
                counts[name] += new - old
        if self._episode is not None:
            self._episode["configure_events"] += now[0] - before[0]
            self._episode["root_configure_events"] += now[1] - before[1]

    def _tick(self) -> None:
        if self.closed:
            return
        now = self._clock()
        late_ms = max(0.0, (now - self._due) * 1000)
        busy_ms, self._busy = self._busy * 1000, 0.0
        try:
            jobs = int(self._jobs())
        except Exception:  # noqa: BLE001
            jobs = 0
        modal = self._move_size()
        if modal:
            if self._episode is None:
                self._episode = {"started": now, "size": self._size(), "resized": False, "ticks": [],
                                 "configure_events": 0, "root_configure_events": 0}
            elif not self._episode["resized"] and self._size() != self._episode["size"]:
                self._episode["resized"] = True
            if len(self._episode["ticks"]) < _EPISODE_TICKS:
                self._episode["ticks"].append((late_ms, busy_ms, jobs))
            self._read_event_counters()
        else:
            self._read_event_counters()
            if self._episode is not None:
                self._finish_episode(now)
            self._beat(self.phase, late_ms, busy_ms, jobs, modal=False)
        self._due = self._clock() + HEARTBEAT_MS / 1000
        try:
            self._timer = self.root.after(HEARTBEAT_MS, self._tick)
        except Exception:  # noqa: BLE001 - the window is being destroyed
            self._timer = None

    def _size(self) -> tuple[int, int]:
        try:
            return self.root.winfo_width(), self.root.winfo_height()
        except Exception:  # noqa: BLE001
            return 0, 0

    def _finish_episode(self, now: float) -> None:
        episode, self._episode = self._episode, None
        phase = "native_resize" if episode["resized"] else "native_move"
        for late_ms, busy_ms, jobs in episode["ticks"]:
            self._beat(phase, late_ms, busy_ms, jobs, modal=True)
        lateness = [tick[0] for tick in episode["ticks"]]
        self.episodes.append({"kind": phase, "duration_ms": round((now - episode["started"]) * 1000, 1),
                              "ticks": len(lateness), "max_late_ms": round(max(lateness, default=0.0), 3),
                              "late_ticks": sum(value >= LATE_MS for value in lateness),
                              "configure_events": episode["configure_events"],
                              # How often the window itself was re-laid out: few per second means slow redraws
                              # even when no single tick is late (Tk serves timers between the pieces).
                              "root_configure_events": episode["root_configure_events"]})

    def _beat(self, phase: str, late_ms: float, busy_ms: float, jobs: int, *, modal: bool) -> None:
        self._record(phase, "heartbeat_late", late_ms)
        if jobs:
            self._record(phase, "heartbeat_late.jobs_active", late_ms)
        counts = self._counts.setdefault(phase, Counter())
        counts["ticks"] += 1
        counts["max_active_jobs"] = max(counts["max_active_jobs"], jobs)
        if late_ms < LATE_MS:
            return
        if busy_ms >= late_ms / 2:
            cause = "app"
        elif modal:
            cause = "native_modal"
        elif jobs:
            cause = "worker_contention"
        else:
            cause = "unattributed"
        counts["late_ticks"] += 1
        counts[f"late.{cause}"] += 1
        self.slow.append({"at_s": round(self._clock() - self._started, 3), "phase": phase, "name": "heartbeat_late",
                          "ms": round(late_ms, 3), "cause": cause, "measured_app_ms": round(busy_ms, 3),
                          "active_jobs": jobs})

    # -- reporting --------------------------------------------------------------------

    def environment(self) -> dict:
        info = {"python": platform.python_version(), "os": platform.platform()}
        try:
            info["tk"] = str(self.root.tk.call("info", "patchlevel"))
            info["tk_scaling"] = float(self.root.tk.call("tk", "scaling"))
        except Exception:  # noqa: BLE001
            pass
        try:
            import customtkinter
            info["customtkinter"] = customtkinter.__version__
        except Exception:  # noqa: BLE001
            pass
        return info

    def report(self) -> dict:
        with self._lock:
            phases: dict[str, dict] = {}
            # Summarizing allocates objects and can trigger GC on this thread.
            # Its callback re-enters the RLock and may add a new timing key.
            # Iterate a snapshot so recording that collection remains safe.
            for (phase, name), stat in self._stats.copy().items():
                phases.setdefault(phase, {"counts": {}, "timings": {}})["timings"][name] = stat.summary()
            for phase, counts in self._counts.items():
                phases.setdefault(phase, {"counts": {}, "timings": {}})["counts"] = dict(counts)
            return {"environment": self.environment(), "heartbeat_ms": HEARTBEAT_MS, "late_threshold_ms": LATE_MS,
                    "slow_threshold_ms": SLOW_MS, "duration_s": round(self._clock() - self._started, 3),
                    "phases": phases, "native_episodes": list(self.episodes), "slow_events": list(self.slow)}

    def close(self) -> dict | None:
        """Stop measuring and write the report (once); the Tcl counters end with the interpreter."""
        global _active
        if self.closed:
            return None
        if self._episode is not None:
            self._finish_episode(self._clock())
        report = self.report()
        self.closed = True
        if _active is self:
            _active = None
        try:
            gc.callbacks.remove(self._on_gc)
        except ValueError:
            pass
        try:
            if self._timer is not None:
                self.root.after_cancel(self._timer)
        except Exception:  # noqa: BLE001
            pass
        if self.output is not None:
            try:
                self.output.parent.mkdir(parents=True, exist_ok=True)
                self.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"UI diagnostics written to {self.output}", file=sys.stderr)
            except OSError as error:
                print(f"UI diagnostics could not be written: {error}", file=sys.stderr)
        return report
