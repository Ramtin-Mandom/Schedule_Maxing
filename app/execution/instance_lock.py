"""
app/execution/instance_lock.py

One application process per database file (Milestone 4 desktop/web
boundary, docs/desktop-web-boundaries.md).

SQLite itself keeps concurrent processes' transactions safe, but the
desktop app and the local web service (python -m app.web) each also own a
synchronization session on the database: an in-memory access token, the
selected account, the account marked active on the device (the owner
stamped on new records), the outbox and a background sync loop. Two such
processes on one file could push the same outbox twice or switch the
active account under each other. So each of them takes this lock before it
opens the database, and a second one is refused with DatabaseInUseError
instead of silently sharing the session state.

The lock is an OS advisory lock on "<database>.lock" (msvcrt on Windows,
flock elsewhere), held for the process's lifetime and released on close or
when the process ends -- a crash never leaves a stale lock behind. The file
also records who holds the lock, for the error message. Command-line tools
that only read or import (python -m app.main) do not take it. An in-memory
database needs no lock.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.execution.db import StorageError

#: The locked byte lies beyond the holder description, so other processes can still read who holds it (Windows
#: locks are mandatory for the locked range).
_LOCK_OFFSET = 1 << 20


class DatabaseInUseError(StorageError):
    """Another Schedule Maxing process (the desktop app or the local web service) has this database open."""

    def __init__(self, db_path: Path, holder: str | None) -> None:
        self.db_path = db_path
        self.holder = holder
        who = f" ({holder})" if holder else ""
        super().__init__(
            f"The database {db_path} is already open in another Schedule Maxing process{who}. "
            "The desktop app and the local web service (python -m app.web) cannot use the same database at the "
            "same time; close the other one first."
        )


def lock_path_for(db_path: str | Path) -> Path:
    path = Path(db_path)
    return path.with_name(path.name + ".lock")


class InstanceLock:
    """An exclusive, non-blocking, process-lifetime lock on one database file."""

    def __init__(self, db_path: str | Path, holder: str) -> None:
        self.db_path = Path(db_path)
        self.path = lock_path_for(db_path)
        self.holder = holder
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "InstanceLock":
        """Take the lock, or raise DatabaseInUseError naming its current holder."""
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if not _try_lock(fd):
                raise DatabaseInUseError(self.db_path, _read_holder(fd))
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{self.holder}, process {os.getpid()}".encode("utf-8"))
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)


def acquire_instance_lock(db_path: str | Path, holder: str) -> InstanceLock | None:
    """The lock for a database file (None for an in-memory database, which no other process can open)."""
    if str(db_path) == ":memory:":
        return None
    return InstanceLock(db_path, holder).acquire()


def _read_holder(fd: int) -> str | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 400).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


if sys.platform.startswith("win"):
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
