"""
app/runtime.py

Where this process runs from and where its files live -- the one module that
knows the difference between a source checkout (`python -m app.desktop`) and
the packaged Windows application (PyInstaller, docs/windows-distribution.md).

    bundled, read-only resources   resource_root() / resource_path(...)
        source:   the repository root
        packaged: the bundle directory (sys._MEIPASS, "<install>\\_internal")
    writable per-user data         config.settings.DATA_DIR and, under it,
        logs_dir(), backups_dir(), updates_dir()

The install directory is never written to. Nothing here creates a directory
or reads a file on import; callers create what they need. Standard library
only, and free of Tk and of every optional package, so it is safe to import
first. Other modules ask these helpers instead of testing `sys.frozen`.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

LOGS_DIRNAME = "logs"
BACKUPS_DIRNAME = "backups"
UPDATES_DIRNAME = "updates"


def is_frozen(system=sys) -> bool:
    """True in the packaged application (a PyInstaller build), False from source."""
    return bool(getattr(system, "frozen", False)) and hasattr(system, "_MEIPASS")


def resource_root(system=sys) -> Path:
    """The directory holding the bundled read-only resources (config/, assets/)."""
    if is_frozen(system):
        return Path(system._MEIPASS)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str, system=sys) -> Path:
    """A bundled read-only resource, e.g. resource_path("config", "task_preference.yaml")."""
    return resource_root(system).joinpath(*parts)


def data_dir() -> Path:
    """The writable per-user data directory (read at call time, so overrides and tests apply)."""
    from config import settings

    return Path(settings.DATA_DIR)


def logs_dir() -> Path:
    return data_dir() / LOGS_DIRNAME


def backups_dir() -> Path:
    return data_dir() / BACKUPS_DIRNAME


def updates_dir() -> Path:
    return data_dir() / UPDATES_DIRNAME


def ensure_standard_streams(system=sys) -> None:
    """
    A windowed executable has no console: sys.stdout and sys.stderr are None,
    and anything that prints (argparse, a stray print) would raise. Give such
    a process writable streams that discard their output.
    """
    for name in ("stdout", "stderr"):
        if getattr(system, name, None) is None:
            setattr(system, name, io.TextIOWrapper(open(os.devnull, "wb"), encoding="utf-8", errors="replace"))
