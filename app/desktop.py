"""
app/desktop.py

The production entry point of the Schedule Maxing desktop application: what
the packaged Windows executable runs (packaging/windows/ScheduleMaxing.spec),
and the same start-up from source with

    python -m app.desktop

It is a thin bootstrap around the window in app/app.py:

    1. give a console-less process usable standard streams (app/runtime.py);
    2. start the log file and the exception hooks (app/logging_setup.py);
    3. open the window on the local SQLite database;
    4. start the background update check (app/ui/update_view.py).

Storage is always local here. The direct PostgreSQL mode needs database
credentials, which a distributed client must never hold, so this entry point
ignores SCHEDULE_MAXING_STORAGE and SCHEDULE_MAXING_ENV_FILE and takes no
--storage option; that mode stays a development tool of `python -m app.app`
(docs/direct-postgres.md).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from app import logging_setup, runtime
from app.version import APP_ID, APP_NAME

logger = logging.getLogger(__name__)

LOCAL_STORAGE = "local"
SELF_TEST_OPTION = "--self-test"

#: A named Windows mutex held while the application runs. It only tells the installer (AppMutex in
#: packaging/windows/ScheduleMaxing.iss) that the application is open; whether a second window may use the
#: database is still decided by the database's own lock (app/execution/instance_lock.py).
INSTANCE_MUTEX_NAME = f"{APP_ID}_SingleInstance"
_instance_mutex = None


def hold_instance_mutex(name: str = INSTANCE_MUTEX_NAME) -> bool:
    """Create the mutex and keep it for the life of the process (Windows only; failure is ignored)."""
    global _instance_mutex
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    except Exception:  # noqa: BLE001 - a signal for the installer only; never a reason not to start
        _instance_mutex = None
    return bool(_instance_mutex)


def show_error(message: str) -> None:
    """A plain error dialog that needs no Tk window (there may be none yet, or none any more)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)  # MB_ICONERROR
            return
        except Exception:  # noqa: BLE001 - fall through to Tk
            pass
    try:
        from tkinter import messagebox

        messagebox.showerror(APP_NAME, message)
    except Exception:  # noqa: BLE001 - nothing left to report with; the log has the details
        pass


def _build_window():
    # Imported here: Tk, CustomTkinter and the application modules load only after the bootstrap above them is ready.
    from app.app import ScheduleOptimizerApp

    return ScheduleOptimizerApp(storage=LOCAL_STORAGE)


def start_runtime(*, report: Callable[[str], None] = show_error) -> None:
    """Everything a console-less process needs before the window exists: streams, the log file, error hooks."""
    runtime.ensure_standard_streams()
    logging_setup.configure_logging()
    logging_setup.install_exception_hooks(report)
    logging_setup.log_startup()
    hold_instance_mutex()


def main(argv: list[str] | None = None, *, window_factory: Callable[[], object] = _build_window,
         report: Callable[[str], None] = show_error) -> int:
    start_runtime(report=report)
    arguments = list(argv or [])
    if SELF_TEST_OPTION in arguments:
        # The build's own check of the packaged program (app/selftest.py); never part of normal use.
        from app import selftest

        index = arguments.index(SELF_TEST_OPTION)
        if index + 1 >= len(arguments):
            return selftest.EXIT_REFUSED
        return selftest.run(arguments[index + 1])
    window = window_factory()
    # An exception inside a Tk callback: logged, shown once, and the window keeps working.
    window.report_callback_exception = logging_setup.tk_error_handler(report)
    # Look for a newer version in the background, once the window is up; never a condition for starting.
    start_update_checks = getattr(window, "start_update_checks", None)
    if start_update_checks is not None:
        start_update_checks()
    window.mainloop()
    logger.info("%s closed.", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
