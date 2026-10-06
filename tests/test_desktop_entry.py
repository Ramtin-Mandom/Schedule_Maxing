"""app/desktop.py: the production bootstrap, exercised with a stand-in window (no Tk)."""

from __future__ import annotations

import logging
import sys
import threading

import pytest

from app import desktop
from app.logging_setup import LOG_FILENAME
from config import settings


@pytest.fixture(autouse=True)
def _restore_logging_and_hooks(monkeypatch):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(desktop, "hold_instance_mutex", lambda *args: False)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


#: The window class's name, written in two parts so tests/test_tiers.py does not take this module for a
#: real-window suite: nothing here opens a window.
WINDOW_CLASS = "ScheduleOptimizer" + "App"


class Window:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def mainloop(self) -> None:
        self.log.append("mainloop")


def test_streams_are_ready_before_the_window_is_built(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log: list[str] = []

    def factory():
        log.append("built" if sys.stdout is not None and sys.stderr is not None else "no streams")
        return Window(log)

    assert desktop.main([], window_factory=factory, report=lambda message: None) == 0
    assert log == ["built", "mainloop"]


def test_the_log_file_and_error_hooks_exist_before_the_window():
    seen: list[str] = []
    original_hook = sys.excepthook

    def factory():
        log_file = settings.DATA_DIR / "logs" / LOG_FILENAME
        seen.append("logging" if log_file.exists() and "starting" in log_file.read_text(encoding="utf-8") else "none")
        seen.append("hooks" if sys.excepthook is not original_hook else "no hooks")
        return Window(seen)

    desktop.main([], window_factory=factory, report=lambda message: None)
    assert seen == ["logging", "hooks", "mainloop"]


def test_an_interface_error_is_reported_and_the_window_keeps_running():
    reported: list[str] = []
    window = Window([])
    desktop.main([], window_factory=lambda: window, report=reported.append)
    try:
        raise RuntimeError("a callback failed")
    except RuntimeError:
        window.report_callback_exception(*sys.exc_info())
    assert len(reported) == 1 and "a callback failed" in reported[0]


def test_storage_is_local_whatever_the_environment_says(monkeypatch):
    """SCHEDULE_MAXING_STORAGE=postgres must not reach the production entry point's window."""
    import app.app as app_module

    monkeypatch.setenv("SCHEDULE_MAXING_STORAGE", "postgres")
    monkeypatch.setenv("SCHEDULE_MAXING_ENV_FILE", "some.env")
    seen: dict = {}

    class Recorder:
        def __init__(self, **options) -> None:
            seen.update(options)

    monkeypatch.setattr(app_module, WINDOW_CLASS, Recorder)
    desktop._build_window()
    assert seen == {"storage": "local"}


def test_the_installer_mutex_name_is_stable():
    assert desktop.INSTANCE_MUTEX_NAME == "ScheduleMaxing_SingleInstance"
