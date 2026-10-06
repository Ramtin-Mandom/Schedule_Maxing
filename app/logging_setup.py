"""
app/logging_setup.py

Log files and last-resort error handling for the desktop application, which
in its packaged form has no console to print to (app/desktop.py installs
both; `python -m app.app` keeps plain console behavior for development).

    <data directory>/logs/schedule-maxing.log      (rotated: 1 MB, 5 older files)

What is logged: one line per start (version, packaged or source, Python and
Windows versions, the data directory), database backups and migrations,
synchronization and update failures, and the traceback of any unexpected
error. What is never logged: passwords, access or refresh tokens,
Authorization headers or database URLs -- the application does not pass
them to a logger, and scrub() masks anything of that shape in case a library
error message quotes one.

Unexpected errors: install_exception_hooks() logs an exception that reaches
the top of the main thread or of a worker thread; tk_error_handler() does the
same for an exception inside a Tk callback and shows one short dialog naming
the log file, instead of the application silently doing nothing.

Logging is never a reason not to start: if the log file cannot be created,
the application runs without it.
"""

from __future__ import annotations

import logging
import platform
import re
import sys
import threading
import time
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import runtime
from app.version import APP_NAME, __version__

LOG_FILENAME = "schedule-maxing.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 5
FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"

logger = logging.getLogger(__name__)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(password|passwd|refresh_token|access_token|token|secret|authorization)\b(\"?\s*[:=]\s*\"?)[^\s\"',;&]+"),
    re.compile(r"(?i)\b(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+):[^@\s]+@"),
)


def scrub(text: str) -> str:
    """`text` with anything shaped like a credential masked."""
    text = _SECRET_PATTERNS[0].sub(r"\1 ***", text)
    text = _SECRET_PATTERNS[1].sub(r"\1\2***", text)
    return _SECRET_PATTERNS[2].sub(r"\1:***@", text)


class ScrubbingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def log_path(log_dir: str | Path | None = None) -> Path:
    return Path(log_dir) / LOG_FILENAME if log_dir is not None else runtime.logs_dir() / LOG_FILENAME


def configure_logging(log_dir: str | Path | None = None, *, level: int = logging.INFO) -> Path | None:
    """
    Send the application's log records to the rotating log file. Returns its
    path, or None when it could not be opened (the application still runs).
    Calling it again for the same file adds nothing.
    """
    path = log_path(log_dir)
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path.resolve():
            return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=False)
    except OSError:
        return None
    handler.setFormatter(ScrubbingFormatter(FORMAT))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


def log_startup() -> None:
    logger.info(
        "%s %s starting (%s; Python %s; %s; data directory %s)",
        APP_NAME, __version__, "packaged" if runtime.is_frozen() else "source", platform.python_version(),
        platform.platform(), runtime.data_dir(),
    )


def install_exception_hooks(show_error: Callable[[str], None] | None = None) -> None:
    """
    Log an exception that nothing handled, on the main thread (and then call
    `show_error(message)`, e.g. a dialog) or on any other thread (logged only).
    """

    def main_thread_hook(exc_type, exc, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, traceback)
            return
        logger.critical("Unexpected error; the application cannot continue.", exc_info=(exc_type, exc, traceback))
        if show_error is not None:
            try:
                show_error(describe_unexpected_error(exc))
            except Exception:  # noqa: BLE001 - reporting must never raise from the hook
                pass

    def thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread is not None else "?"
        logger.error("Unexpected error in background thread %s.", name,
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = main_thread_hook
    threading.excepthook = thread_hook


def describe_unexpected_error(error: BaseException, log_dir: str | Path | None = None) -> str:
    return (
        f"{APP_NAME} ran into an unexpected problem.\n\n"
        f"{type(error).__name__}: {scrub(str(error))}\n\n"
        f"Your saved data is not affected. Details were written to:\n{log_path(log_dir)}"
    )


def tk_error_handler(show_error: Callable[[str], None], *, repeat_after: float = 30.0,
                     clock: Callable[[], float] = time.monotonic) -> Callable:
    """
    A Tk `report_callback_exception`: log the traceback of an exception raised
    inside a callback and show one dialog. The same error repeating (a timer
    that keeps failing) is logged each time but shown again only after
    `repeat_after` seconds, so the window stays usable.
    """
    shown: dict[tuple[str, str], float] = {}

    def handle(exc_type, exc, traceback) -> None:
        logger.error("Unexpected error in the user interface.", exc_info=(exc_type, exc, traceback))
        key, now = (exc_type.__name__, str(exc)), clock()
        last = shown.get(key)
        if last is not None and now - last < repeat_after:
            return
        shown[key] = now
        try:
            show_error(describe_unexpected_error(exc))
        except Exception:  # noqa: BLE001 - the window may be closing
            pass

    return handle
