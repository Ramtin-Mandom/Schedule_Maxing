"""The safety copy taken before a schema migration (app/execution/backup.py, db.get_connection)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.backup import KEEP_BACKUPS, backup_before_migration, backups_dir_for, list_backups
from app.execution.db import (
    LATEST_SCHEMA_VERSION,
    BackupError,
    MigrationError,
    NewerSchemaError,
    get_connection,
    initialize_schema,
)

OLD_VERSION = LATEST_SCHEMA_VERSION - 1


def build_database(path: Path, version: int) -> None:
    """A genuine database at an older schema version, holding one recognizable row."""
    connection = sqlite3.connect(str(path), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_schema(connection, target_version=version)
        connection.execute("CREATE TABLE keepsake (note TEXT)")
        connection.execute("INSERT INTO keepsake VALUES ('before the update')")
    finally:
        connection.close()


def version_of(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def note_in(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return connection.execute("SELECT note FROM keepsake").fetchone()[0]
    finally:
        connection.close()


def test_an_older_database_is_copied_then_migrated_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "executions.db"
    build_database(path, OLD_VERSION)

    get_connection(path).close()

    assert version_of(path) == LATEST_SCHEMA_VERSION and note_in(path) == "before the update"
    (backup,) = list_backups(backups_dir_for(path), path)
    assert backup.name.startswith(f"executions-v{OLD_VERSION}-") and backup.suffix == ".db"
    assert version_of(backup) == OLD_VERSION and note_in(backup) == "before the update"  # a complete, readable copy
    assert not list(backups_dir_for(path).glob("*.partial"))


def test_new_current_and_in_memory_databases_need_no_copy(tmp_path: Path) -> None:
    path = tmp_path / "executions.db"
    get_connection(path).close()  # created
    get_connection(path).close()  # already current
    get_connection(":memory:").close()
    assert not backups_dir_for(path).exists()


def test_only_the_newest_copies_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "executions.db"
    build_database(path, OLD_VERSION)
    (tmp_path / "other.txt").write_text("untouched", encoding="utf-8")
    connection = sqlite3.connect(str(path))
    try:
        for minute in range(KEEP_BACKUPS + 3):
            backup_before_migration(connection, path, OLD_VERSION, now=datetime(2026, 1, 1, 0, minute, tzinfo=timezone.utc))
    finally:
        connection.close()
    kept = list_backups(backups_dir_for(path), path)
    assert len(kept) == KEEP_BACKUPS
    assert kept[0].name.endswith("20260101T000700000000Z.db")  # the newest first; the oldest three are gone
    assert (tmp_path / "other.txt").read_text(encoding="utf-8") == "untouched"  # nothing else is ever removed


def test_a_database_that_cannot_be_copied_is_not_migrated(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "executions.db"
    build_database(path, OLD_VERSION)

    def full_disk(*args, **kwargs):
        raise OSError("no space left on device (injected)")

    monkeypatch.setattr(db_module, "backup_before_migration", full_disk)
    with pytest.raises(BackupError, match="could not back up"):
        get_connection(path)
    assert version_of(path) == OLD_VERSION and note_in(path) == "before the update"


def test_a_failed_migration_leaves_the_old_version_and_its_copy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "executions.db"
    build_database(path, OLD_VERSION)
    broken = db_module.MIGRATIONS[:-1] + ((LATEST_SCHEMA_VERSION, ("CREATE TABLE half_done (x)", "THIS IS NOT SQL")),)
    monkeypatch.setattr(db_module, "MIGRATIONS", broken)

    with pytest.raises(MigrationError):
        get_connection(path)

    assert version_of(path) == OLD_VERSION and note_in(path) == "before the update"
    connection = sqlite3.connect(str(path))
    try:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    finally:
        connection.close()
    (backup,) = list_backups(backups_dir_for(path), path)
    assert version_of(backup) == OLD_VERSION


def test_a_newer_database_is_refused_untouched_without_a_copy(tmp_path: Path) -> None:
    path = tmp_path / "executions.db"
    connection = sqlite3.connect(str(path))
    connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
    connection.close()
    before = path.read_bytes()

    with pytest.raises(NewerSchemaError):
        get_connection(path)
    assert path.read_bytes() == before and not backups_dir_for(path).exists()
