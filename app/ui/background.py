"""
background.py

Runs work that touches storage or computes (ExecutionService,
ProductivityService, PlanningService, sync) off the Tk main thread, so a slow
or merely non-trivial call never blocks the UI. Results are handed back to
the Tk main thread by a main-thread `after` poll; workers never call Tk.

ControllerResult is the structured-error-handling mechanism used throughout
app/ui/: every controller method that can fail returns one of these instead
of raising, so a Tk callback can always branch on `.ok` and show `.error`
instead of letting an exception reach -- and potentially crash -- the UI.

Execution (WorkerRegistry)
--------------------------
- At most MAX_WORKERS (4) threads run jobs; further jobs wait in a FIFO
  queue. Four is deliberate: local storage is one SQLite connection behind
  one lock and the planning controller has its own lock, so more threads
  would only wait on those locks while competing with the Tk thread for the
  interpreter. Four leaves room for the serialized write lane, one long
  job (schedule generation, a sync round trip) and two short reads.
  Threads are started on demand and end when nothing is runnable.
- `serial=key`: jobs with the same key run one at a time, in submission
  order. Every storage mutation uses WRITE_LANE, so two changes never
  interleave their read-check-write steps (each is still protected by its
  expected_version).
- `supersede=key`: a read whose result only matters if it is the newest. A
  newer submission with the same key removes a queued older one (it is
  never started) and discards the result of a running one. Only reads are
  superseded; a write is never dropped.
- The queue holds at most MAX_QUEUED (64) jobs that cannot be superseded. A
  page runs one blocking action at a time, so this is a backstop, far above
  normal use. Beyond it a job is not run: its on_done receives a failed
  ControllerResult saying so (the form keeps what was typed) -- submission
  never blocks the Tk thread and never silently drops a write.
- If a worker thread cannot be started, the waiting jobs fail the same
  visible way rather than leaving a page busy.

Delivery
--------
One `after` poll per Tk root (not one per job) hands finished results to
their on_done callbacks, for at most ~8 ms per turn before returning to the
event loop, so a burst of completions cannot monopolize it. The next poll
is scheduled before each callback runs: a callback that raises, or opens a
dialog with its own event loop, cannot stall later results. A result is
never delivered to a widget that no longer exists, nor after shutdown has
begun, nor after its `still_current` guard says it is stale, nor once it
was superseded.

Shutdown
--------
WorkerRegistry.shutdown() refuses new work, discards queued superseding
reads (their results could never be shown), lets running jobs and queued
writes finish, and reports whether everything finished in time -- so the
application closes its database only once nothing is still using it (see
app/ui/app_services.py).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.ui import diagnostics

T = TypeVar("T")

#: Windows serves Tk timers on a ~15.6 ms tick, so 10 ms means "the next tick".
_POLL_INTERVAL_MS = 10
#: See the module docstring for why these limits were chosen.
MAX_WORKERS = 4
MAX_QUEUED = 64
#: The longest one poll keeps delivering results before it returns to the event loop.
_DELIVERY_BUDGET_SECONDS = 0.008
#: The `serial` key of every storage mutation.
WRITE_LANE = "storage-write"

SATURATED_MESSAGE = ("The app is still working on earlier requests, so this one was not started and nothing was "
                     "changed. Try again in a moment.")


@dataclass(frozen=True)
class ControllerResult(Generic[T]):
    """The outcome of one controller operation: either a value, or an error message."""

    ok: bool
    value: T | None = None
    error: str | None = None
    #: The structured error behind a failure, when there is one (e.g. RegenerationRequiredError's problems).
    cause: BaseException | None = field(default=None, compare=False, repr=False)

    @classmethod
    def success(cls, value: T) -> ControllerResult[T]:
        return cls(ok=True, value=value, error=None)

    @classmethod
    def failure(cls, error: str, cause: BaseException | None = None) -> ControllerResult[T]:
        return cls(ok=False, value=None, error=error, cause=cause)


class _Job:
    __slots__ = ("widget", "root", "key", "work", "on_done", "still_current", "serial", "supersede", "outcome",
                 "superseded")

    def __init__(self, widget, work, on_done, still_current, serial, supersede) -> None:
        self.widget, self.work, self.on_done, self.still_current = widget, work, on_done, still_current
        self.serial, self.supersede = serial, supersede
        self.root = _root_of(widget)
        self.key = id(self.root)
        self.outcome = None
        self.superseded = False


def _root_of(widget):
    """The Tk root whose event loop delivers the widget's results (the widget itself for a stand-in)."""
    try:
        return widget._root()
    except Exception:  # noqa: BLE001 - a test stand-in with only after()/winfo_exists()
        return widget


def _exists(widget) -> bool:
    try:
        return bool(widget.winfo_exists())
    except Exception:  # noqa: BLE001 - a destroyed Tk application raises TclError here
        return False


class WorkerRegistry:
    """Runs background jobs on a bounded set of threads, delivers their results, and lets shutdown wait."""

    def __init__(self, *, max_workers: int = MAX_WORKERS, max_queued: int = MAX_QUEUED,
                 thread_factory: Callable[..., threading.Thread] = threading.Thread) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False
        #: Makes the default still_current guard of each new job (AppServices installs workspace_guard):
        #: a result started before an account/workspace switch is then never delivered after it.
        self.result_guard: Callable[[], Callable[[], bool]] | None = None
        self.max_workers, self.max_queued = max_workers, max_queued
        self._thread_factory = thread_factory
        self._queue: deque[_Job] = deque()
        self._running: set[_Job] = set()
        self._lanes: set[Hashable] = set()
        self._threads = 0
        # Per Tk root (by id): its finished-but-undelivered jobs, how many jobs still owe it a delivery, and
        # whether its poll is scheduled.
        self._roots: dict[int, object] = {}
        self._finished: dict[int, deque[_Job]] = {}
        self._undelivered: dict[int, int] = {}
        self._polling: set[int] = set()

    @property
    def closing(self) -> bool:
        with self._condition:
            return self._closing

    @property
    def active(self) -> int:
        """Jobs waiting or running (what shutdown waits for)."""
        with self._condition:
            return self._active

    @property
    def outstanding(self) -> int:
        """Jobs waiting, running, or finished with a result not yet handed to the Tk thread."""
        with self._condition:
            return self._active + sum(len(finished) for finished in self._finished.values())

    def begin(self) -> bool:
        """Register work running outside the pool; False (start nothing) once shutdown has begun."""
        with self._condition:
            if self._closing:
                return False
            self._active += 1
            return True

    def end(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def shutdown(self, timeout: float | None = None) -> bool:
        """Refuse new work, then wait for running jobs and queued writes. True if all finished in time."""
        with self._condition:
            self._closing = True
            for job in [queued for queued in self._queue if queued.supersede is not None]:
                self._queue.remove(job)  # a read nobody will see: not started
                self._active -= 1
            self._finished.clear()  # nothing is delivered once shutdown has begun
            self._undelivered.clear()
            self._roots.clear()
            return self._condition.wait_for(lambda: self._active == 0, timeout=timeout)

    # -- submission (Tk thread) -----------------------------------------------------------

    def submit(self, job: _Job) -> bool:
        with self._condition:
            if self._closing:
                return False
            self._forget_dead_roots()
            saturated = False
            if job.supersede is not None:
                for queued in [queued for queued in self._queue if queued.supersede == job.supersede]:
                    self._queue.remove(queued)
                    self._active -= 1
                    self._owed(queued.key, -1)
                for running in self._running:
                    if running.supersede == job.supersede:
                        running.superseded = True
            elif sum(queued.supersede is None for queued in self._queue) >= self.max_queued:
                saturated = True
            self._roots[job.key] = job.root
            self._active += 1
            self._owed(job.key, +1)
            if saturated:
                self._finish(job, ControllerResult.failure(SATURATED_MESSAGE))
            else:
                self._queue.append(job)
                self._start_threads()
        self._ensure_poll(job.key)
        return True

    def _owed(self, key: int, change: int) -> None:
        self._undelivered[key] = self._undelivered.get(key, 0) + change

    def _forget_dead_roots(self) -> None:
        for key, root in list(self._roots.items()):
            if not _exists(root):  # its timers will never fire: nothing can be delivered there
                self._roots.pop(key, None)
                self._finished.pop(key, None)
                self._undelivered.pop(key, None)
                self._polling.discard(key)

    # -- execution ------------------------------------------------------------------------

    def _take_runnable(self) -> _Job | None:
        for job in self._queue:
            if job.serial is None or job.serial not in self._lanes:
                self._queue.remove(job)
                self._running.add(job)
                if job.serial is not None:
                    self._lanes.add(job.serial)
                return job
        return None

    def _start_threads(self) -> None:
        while self._threads < self.max_workers:
            job = self._take_runnable()
            if job is None:
                return
            self._threads += 1
            try:
                self._thread_factory(target=self._work, args=(job,), daemon=True).start()
            except Exception as error:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
                self._threads -= 1
                self._running.discard(job)
                self._lanes.discard(job.serial)
                if self._threads:  # a running worker takes it when it is free
                    self._queue.appendleft(job)
                    return
                failed, self._queue = [job, *self._queue], deque()
                for waiting in failed:  # nothing can run them: fail visibly instead of leaving pages busy
                    self._finish(waiting, ControllerResult.failure(
                        f"The operation could not be started: {error}", error))
                return

    def _work(self, job: _Job | None) -> None:
        while job is not None:
            try:
                with diagnostics.span("worker", job.work):  # off the Tk thread: reported, never counted as Tk work
                    outcome = job.work()
            except BaseException as error:  # noqa: BLE001 - last-resort delivery: never leave a page stuck busy
                outcome = ControllerResult.failure(f"The operation could not finish: {error}", error)
            with self._condition:
                self._running.discard(job)
                self._lanes.discard(job.serial)
                self._finish(job, outcome)
                job = self._take_runnable()
                if job is None:
                    self._threads -= 1
                else:
                    self._start_threads()

    def _finish(self, job: _Job, outcome) -> None:
        job.outcome = outcome
        self._active -= 1
        if not self._closing and job.key in self._roots:
            self._finished.setdefault(job.key, deque()).append(job)
        self._condition.notify_all()

    # -- delivery (Tk thread) -------------------------------------------------------------

    def _ensure_poll(self, key: int) -> None:
        with self._condition:
            root = self._roots.get(key)
            if root is None or key in self._polling or self._closing:
                return
            self._polling.add(key)
        try:
            root.after(_POLL_INTERVAL_MS, lambda: self._poll(key))
        except Exception:  # noqa: BLE001 - the window is gone
            with self._condition:
                self._polling.discard(key)

    def _poll(self, key: int) -> None:
        with self._condition:
            self._polling.discard(key)
        deadline = time.perf_counter() + _DELIVERY_BUDGET_SECONDS
        while True:
            with self._condition:
                if self._closing:
                    return
                finished = self._finished.get(key)
                job = finished.popleft() if finished else None
                if job is not None:
                    self._owed(key, -1)
                owed = self._undelivered.get(key, 0)
                if owed <= 0:
                    self._undelivered.pop(key, None)
                    self._finished.pop(key, None)
                    self._roots.pop(key, None)
            if owed > 0:
                # Before the callback: one that raises or opens a dialog must not stall the results after it.
                self._ensure_poll(key)
            if job is None:
                return
            self._deliver(job)
            if time.perf_counter() >= deadline:
                return  # the rest waits for the next poll, after the event loop had its turn

    @staticmethod
    def _deliver(job: _Job) -> None:
        if job.superseded or not _exists(job.widget):
            return
        if job.still_current is not None and not job.still_current():
            return  # e.g. the workspace changed while the work ran: its result belongs to another view
        with diagnostics.span("background.deliver", job.on_done):
            job.on_done(job.outcome)


default_registry = WorkerRegistry()
_current_registry = default_registry


def install_registry(registry: WorkerRegistry) -> None:
    """Make `registry` the default for run_in_background (each app instance installs its own)."""
    global _current_registry
    _current_registry = registry


def current_registry() -> WorkerRegistry:
    return _current_registry


def run_io(
    widget: tk.Misc,
    work: Callable[[], T],
    on_done: Callable[[T], None],
    *,
    background: bool,
    still_current: Callable[[], bool] | None = None,
    serial: Hashable | None = None,
    supersede: Hashable | None = None,
) -> bool:
    """
    A page's storage call. The desktop app passes background=True for local
    and direct storage alike: the call runs in a worker and on_done runs on
    the Tk thread (run_in_background). background=False -- a page built on
    its own, without the app -- runs both at once on the calling thread.
    Returns False if nothing ran (shutting down).
    """
    if not background:
        with diagnostics.span("io.work", work):
            value = work()
        with diagnostics.span("io.deliver", on_done):
            on_done(value)
        return True
    return run_in_background(widget, work, on_done, still_current=still_current, serial=serial, supersede=supersede)


def run_in_background(
    widget: tk.Misc,
    work: Callable[[], T],
    on_done: Callable[[T], None],
    *,
    registry: WorkerRegistry | None = None,
    still_current: Callable[[], bool] | None = None,
    serial: Hashable | None = None,
    supersede: Hashable | None = None,
) -> bool:
    """
    Run `work` on a worker thread; deliver its return value to `on_done`
    back on the Tk main thread (see the module docstring for the limits).

    `work` is expected to return a ControllerResult (every controller method
    does), so failures are just a value flowing through `on_done` -- there is
    no separate error callback here. `work` must not touch Tk: capture what
    it needs (variable values, dates, controllers) before calling this.

    `still_current` (checked on the Tk main thread just before delivery)
    drops a result that no longer applies -- e.g. AppServices.workspace_guard()
    after the account/workspace changed while the work ran. Without one, the
    registry's result_guard (installed by the desktop app) supplies it.

    `serial` orders the job behind earlier jobs with the same key (use
    WRITE_LANE for a mutation); `supersede` marks a read that a newer one
    with the same key replaces.

    Returns False (and runs nothing) if the registry is shutting down.
    """
    registry = registry or _current_registry
    if still_current is None and registry.result_guard is not None:
        still_current = registry.result_guard()
    return registry.submit(_Job(widget, work, on_done, still_current, serial, supersede))
