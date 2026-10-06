"""
app/execution/backup.py

A copy of the application database taken just before its schema is migrated
(app.execution.db.get_connection), so an application update can never be the
reason data is lost: every migration is already atomic, and this is the
recovery copy for the case where one succeeds and is later found to be wrong.

    <data directory>/backups/executions-v<old schema>-<UTC timestamp>.db

- Copied with SQLite's online backup API into a temporary name, then renamed:
  a file with the final name is always a complete database.
- The newest KEEP_BACKUPS are kept; older ones are removed. Nothing else in
  the data directory is ever touched.
- A brand-new, in-memory or already-current database needs no backup.

Failures propagate (sqlite3.Error, OSError): the caller refuses to migrate a
database it could not back up. To restore, close the app and copy a backup
over executions.db (docs/windows-distribution.md).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.runtime import BACKUPS_DIRNAME

logger = logging.getLogger(__name__)

KEEP_BACKUPS = 5


def backups_dir_for(db_path: str | Path) -> Path:
    """The backups of a database: a folder beside it (so every data directory keeps its own)."""
    return Path(db_path).resolve().parent / BACKUPS_DIRNAME


def backup_before_migration(connection: sqlite3.Connection, db_path: str | Path, from_version: int,
                            *, now: datetime | None = None, keep: int = KEEP_BACKUPS) -> Path:
    """Copy the open database (at schema `from_version`) to its backups folder; returns the copy's path."""
    db_path = Path(db_path)
    directory = backups_dir_for(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{db_path.stem}-v{int(from_version)}-{stamp}{db_path.suffix or '.db'}"
    temporary = target.with_name(target.name + ".partial")
    try:
        destination = sqlite3.connect(str(temporary))
        try:
            connection.backup(destination)
        finally:
            destination.close()
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    logger.info("Backed up the database (schema v%d) to %s before migrating it.", from_version, target)
    prune_backups(directory, db_path, keep=keep)
    return target


def list_backups(directory: str | Path, db_path: str | Path) -> list[Path]:
    """This database's complete backups, newest first."""
    db_path = Path(db_path)
    pattern = f"{db_path.stem}-v*-*{db_path.suffix or '.db'}"
    return sorted(Path(directory).glob(pattern), key=lambda path: path.name.rsplit("-", 1)[-1], reverse=True)


def prune_backups(directory: str | Path, db_path: str | Path, *, keep: int = KEEP_BACKUPS) -> None:
    for old in list_backups(directory, db_path)[max(keep, 1):]:
        try:
            old.unlink()
        except OSError as error:  # an old copy that cannot be removed is not a reason to stop
            logger.warning("Could not remove the old database backup %s: %s", old, error)
