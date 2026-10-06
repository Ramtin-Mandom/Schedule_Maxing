"""app/logging_setup.py: the log file, credential masking and the last-resort error hooks."""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app import logging_setup
from app.logging_setup import LOG_FILENAME, configure_logging, install_exception_hooks, scrub, tk_error_handler


@pytest.fixture(autouse=True)
def _restore_logging_and_hooks(monkeypatch):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


def read(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_log_file_is_created_rotating_and_configured_once(tmp_path: Path) -> None:
    path = configure_logging(tmp_path / "logs")
    assert path == tmp_path / "logs" / LOG_FILENAME
    assert configure_logging(tmp_path / "logs") == path
    handlers = [h for h in logging.getLogger().handlers
                if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.resolve()]
    assert len(handlers) == 1 and handlers[0].maxBytes > 0 and handlers[0].backupCount > 0

    logging.getLogger("app.sample").warning("synchronization did not complete: offline")
    assert "synchronization did not complete: offline" in read(path)


def test_default_location_is_the_data_directory(tmp_path: Path, monkeypatch) -> None:
    from config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "data")
    assert configure_logging() == tmp_path / "data" / "logs" / LOG_FILENAME
    logging_setup.log_startup()
    assert "starting (source" in read(tmp_path / "data" / "logs" / LOG_FILENAME)


def test_an_unusable_log_folder_does_not_stop_the_application(tmp_path: Path) -> None:
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    assert configure_logging(blocker) is None


def test_credentials_never_reach_the_log(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    log = logging.getLogger("app.sample")
    log.error("request failed: Authorization: Bearer abc.DEF-123456789")
    log.error("bad url postgresql://owner:hunter2secret@db.example.com/app")
    log.error('payload {"password": "correct horse", "refresh_token": "rt_0123456789"}')
    text = read(path)
    for secret in ("abc.DEF-123456789", "hunter2secret", "correct", "rt_0123456789"):
        assert secret not in text
    assert "db.example.com" in text  # only the credential is masked


def test_scrub_leaves_ordinary_text_alone() -> None:
    assert scrub("Migrated the database to schema v12.") == "Migrated the database to schema v12."


def test_unhandled_errors_are_logged_on_the_main_thread_and_on_workers(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    shown: list[str] = []
    install_exception_hooks(shown.append)

    try:
        raise RuntimeError("main thread failure")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    def work():
        raise ValueError("worker failure")

    thread = threading.Thread(target=work, name="sample-worker")
    thread.start()
    thread.join()

    text = read(path)
    assert "RuntimeError: main thread failure" in text and "Traceback" in text
    assert "ValueError: worker failure" in text and "sample-worker" in text
    assert len(shown) == 1 and "main thread failure" in shown[0] and LOG_FILENAME in shown[0]


def test_a_repeating_interface_error_is_logged_each_time_but_shown_once(tmp_path: Path) -> None:
    path = configure_logging(tmp_path)
    shown: list[str] = []
    now = [0.0]
    handle = tk_error_handler(shown.append, repeat_after=30.0, clock=lambda: now[0])

    def fail(message: str) -> None:
        try:
            raise KeyError(message)
        except KeyError:
            handle(*sys.exc_info())

    fail("timer")
    fail("timer")
    assert len(shown) == 1 and read(path).count("Unexpected error in the user interface.") == 2
    fail("another")
    assert len(shown) == 2
    now[0] = 31.0
    fail("timer")
    assert len(shown) == 3
