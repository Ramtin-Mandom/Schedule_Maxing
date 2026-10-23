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
shutdown() is called when the desktop's storage closes.

Packaged builds: packaging/windows/launcher.py calls multiprocessing.freeze_support()
first, so a spawned worker runs the engine, never a second window.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

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
        self._failed = False
        self._closed = False
        #: Calls answered by the worker process / run in the calling thread instead (diagnostics, tests).
        self.remote_calls = 0
        self.local_calls = 0

    def _pool(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            return self._executor

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
        with self._lock:
            self._closed = True
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


def _is_engine_error(error: BaseException) -> bool:
    """An error the engine itself raised (delivered intact), as opposed to the process failing."""
    from concurrent.futures.process import BrokenProcessPool
    from pickle import PicklingError

    return not isinstance(error, (BrokenProcessPool, PicklingError, OSError, EOFError, AttributeError,
                                  ImportError, TypeError, RuntimeError))
