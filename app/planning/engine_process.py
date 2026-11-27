"""
app/planning/engine_process.py

Runs the day engine -- the pure, CPU-bound part of a schedule generation --
in one separate process, so its work never competes with the desktop window
for Python's GIL.

Only the engine call moves (app/planning/workflow.py `_generate_date` calls
`run_engine(generate_selected_day, ...)`): reading the inputs, the kept-work
reservations, the stale-input checks and the save stay in the background
thread that runs the generation, on the one database connection, exactly as
before. The engine is deterministic (ADR-007) and its arguments and result
are plain pydantic models, so the separate process gives the same result.

EngineProcess: one worker process (spawn), started on first use or by
warm_up() in idle time, used by one generation at a time; the calling thread
waits for the result, so cancellation (the deadline between dates) and the
stale-result rules are unchanged. If the process cannot be started or its
answer cannot be delivered (a broken pool, a pickling or OS error), the call
runs in the calling thread instead -- the same function, so the same result
or the same domain error -- and the process is not tried again this session.
shutdown() is called when the desktop's storage closes; terminate() when
closing gave up waiting for background work (app/app.py CLOSE_DEADLINE_S).

The worker never outlives the application. On Windows it joins a job object
the application holds open with "kill on close": however the application
ends -- closed, crashed or ended in Task Manager -- Windows ends the worker
with it, so no ScheduleMaxing.exe stays behind holding the program's files
(which would make the next installer fail to replace them).

Packaged builds: packaging/windows/launcher.py calls multiprocessing.freeze_support()
first, so a spawned worker runs the engine, never a second window.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_ASSIGN_PROCESS = 0x0001


def _job_api():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.OpenJobObjectW.restype = wintypes.HANDLE
    kernel32.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return ctypes, kernel32


def _create_kill_on_close_job(name: str):
    """A named job object whose processes Windows ends when its last handle closes (None if unavailable)."""
    if not sys.platform.startswith("win"):
        return None
    try:
        ctypes, kernel32 = _job_api()

        class _BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = kernel32.CreateJobObjectW(None, name)
        if not job:
            return None
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                                ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001 - the worker still works; it is only not tied to this process
        logger.warning("The engine process could not be tied to the application.", exc_info=True)
        return None


def _close_job(job) -> None:
    if job:
        try:
            _job_api()[1].CloseHandle(job)
        except Exception:  # noqa: BLE001 - process exit closes it anyway
            pass


def _join_job(name: str | None) -> None:
    """Worker initializer: enter the application's job, then let go of the handle so only the application holds it."""
    if not name or not sys.platform.startswith("win"):
        return
    try:
        _, kernel32 = _job_api()
        job = kernel32.OpenJobObjectW(_JOB_OBJECT_ASSIGN_PROCESS, False, name)
        if job:
            kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())
            kernel32.CloseHandle(job)
    except Exception:  # noqa: BLE001 - the worker runs untied rather than not at all
        pass

_runner: Callable[..., Any] | None = None


def run_engine(function: Callable[..., Any], *args, **kwargs) -> Any:
    """Call `function` through the installed runner (a separate process), else directly."""
    runner = _runner
    return runner(function, *args, **kwargs) if runner is not None else function(*args, **kwargs)


def install(runner: Callable[..., Any] | None) -> None:
    """Make `runner` the way run_engine() calls the engine (None: call it directly)."""
    global _runner
    _runner = runner


def _ready() -> bool:
    return True


class EngineProcess:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: ProcessPoolExecutor | None = None
        self._job = None
        self._failed = False
        self._closed = False
        #: Calls answered by the worker process / run in the calling thread instead (diagnostics, tests).
        self.remote_calls = 0
        self.local_calls = 0

    def _pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._executor is None:
                name = f"Local\\ScheduleMaxing_Engine_{os.getpid()}_{id(self)}"
                self._job = _create_kill_on_close_job(name)
                self._executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"),
                                                     initializer=_join_job,
                                                     initargs=(name if self._job else None,))
            return self._executor

    def _take(self):
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
            job, self._job = self._job, None
        return executor, job

    def __call__(self, function: Callable[..., Any], *args, **kwargs) -> Any:
        if not (self._failed or self._closed):
            try:
                result = self._pool().submit(function, *args, **kwargs).result()
            except Exception as error:  # noqa: BLE001 - see below: the call is made here instead
                # A domain error raised by the engine, or a failure of the process itself: running the same
                # deterministic call here returns its result or raises the same error, typed as usual.
                if not _is_engine_error(error):
                    logger.warning("The schedule engine process is unavailable; generating in-process.",
                                   exc_info=error)
                    self._failed = True
            else:
                self.remote_calls += 1
                return result
        self.local_calls += 1
        return function(*args, **kwargs)

    def warm_up(self) -> None:
        """Start the worker process now (it imports the engine once), without waiting for it."""
        if self._failed or self._closed:
            return
        try:
            self._pool().submit(_ready)
        except Exception as error:  # noqa: BLE001 - a generation will run in-process instead
            logger.warning("The schedule engine process could not be started.", exc_info=error)
            self._failed = True

    def shutdown(self) -> None:
        """
        Stop the worker process and wait until it and the pool's manager thread have ended (it finishes a
        call already running, such as warm_up()'s), so closing the storage leaves nothing running; later
        calls run in-process.
        """
        executor, job = self._take()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        _close_job(job)

    def terminate(self) -> None:
        """
        End the worker process now, even in the middle of a generation (closing gave up waiting). A caller
        still waiting for that call gets a broken pool and runs it in its own thread; later calls run in-process.
        """
        executor, job = self._take()
        if executor is None:
            return
        for process in list((getattr(executor, "_processes", None) or {}).values()):
            try:
                process.terminate()
            except Exception:  # noqa: BLE001 - already gone; closing the job below ends it otherwise
                pass
        _close_job(job)  # kill on close: ends anything left in the job
        executor.shutdown(wait=False, cancel_futures=True)


def _is_engine_error(error: BaseException) -> bool:
    """An error the engine itself raised (delivered intact), as opposed to the process failing."""
    from concurrent.futures.process import BrokenProcessPool
    from pickle import PicklingError

    return not isinstance(error, (BrokenProcessPool, PicklingError, OSError, EOFError, AttributeError,
                                  ImportError, TypeError, RuntimeError))
