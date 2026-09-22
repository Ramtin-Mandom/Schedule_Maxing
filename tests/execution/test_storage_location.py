"""Tests for where the application database lives (config.settings) and how
an existing repository-local executions.db from earlier milestones is
carried over (app.execution.db.adopt_legacy_database / get_connection).

Every path here is under pytest's tmp_path; config.settings attributes are
only ever monkeypatched, never pointed at the real checkout or user profile.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import (
    LATEST_SCHEMA_VERSION,
    LegacyAdoptionError,
    LegacyAdoptionStatus,
    adopt_legacy_database,
    get_connection,
    resolve_db_path,
)
from app.execution.repository import ExecutionRepository
from config import settings
from config.settings import default_user_data_dir, resolve_data_dir
from tests.execution.test_migration_v2 import _build_v1_database


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -----------------------------------------------------------------------------
# Default per-user location
# -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "environ", "expected_parts"),
    [
        ("win32", {"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, ("C:/Users/me/AppData/Local", "ScheduleMaxing")),
        ("win32", {}, ("HOME", "AppData", "Local", "ScheduleMaxing")),
        ("darwin", {}, ("HOME", "Library", "Application Support", "ScheduleMaxing")),
        ("linux", {"XDG_DATA_HOME": "/xdg/data"}, ("/xdg/data", "ScheduleMaxing")),
        ("linux", {}, ("HOME", ".local", "share", "ScheduleMaxing")),
    ],
)
def test_default_user_data_dir_per_platform(tmp_path: Path, platform, environ, expected_parts) -> None:
    home = tmp_path / "home"
    parts = [str(home) if part == "HOME" else part for part in expected_parts]

    assert default_user_data_dir(platform=platform, environ=environ, home=home) == Path(*parts)


def test_default_location_is_independent_of_cwd_and_checkout(tmp_path: Path, monkeypatch) -> None:
    repository_root = Path(settings.__file__).resolve().parent.parent
    environ = {"LOCALAPPDATA": str(tmp_path / "local"), "XDG_DATA_HOME": str(tmp_path / "xdg")}

    first = default_user_data_dir(environ=environ, home=tmp_path)
    monkeypatch.chdir(tmp_path)
    second = default_user_data_dir(environ=environ, home=tmp_path)

    assert first == second
    assert first.is_absolute()
    assert repository_root not in first.parents


def test_env_override_is_retained(tmp_path: Path) -> None:
    override = tmp_path / "custom"
    assert resolve_data_dir({settings.DATA_DIR_ENV_VAR: str(override)}) == (override, True)
    data_dir, overridden = resolve_data_dir({"XDG_DATA_HOME": str(tmp_path / "xdg"), "LOCALAPPDATA": str(tmp_path)})
    assert overridden is False
    assert data_dir.name == settings.APP_DATA_DIRNAME


def test_explicit_path_wins_over_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path / "configured")
    explicit = tmp_path / "explicit.db"

    assert resolve_db_path(explicit) == explicit
    assert resolve_db_path() == tmp_path / "configured" / settings.EXECUTION_DB_FILENAME


# -----------------------------------------------------------------------------
# Legacy adoption (copy, never move/merge/overwrite)
# -----------------------------------------------------------------------------


def test_adopt_copies_legacy_database_and_leaves_source_untouched(tmp_path: Path) -> None:
    source = tmp_path / "repo" / "data" / "executions.db"
    source.parent.mkdir(parents=True)
    _build_v1_database(source)
    digest_before = _digest(source)
    target = tmp_path / "user" / "ScheduleMaxing" / "executions.db"

    result = adopt_legacy_database(source, target)

    assert result.status == LegacyAdoptionStatus.COPIED
    assert _digest(source) == digest_before  # never written to
    assert target.exists()
    assert not list(target.parent.glob(".*adopting*"))  # no temp file left behind

    # The copy is then migrated like any other database; history is intact.
    connection = get_connection(target)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        ids = {row["id"] for row in connection.execute("SELECT id FROM executions")}
        assert ids == {"legacy-1", "not-a-uuid-legacy-id"}
        assert connection.execute("SELECT COUNT(*) FROM work_sessions").fetchone()[0] == 1
    finally:
        connection.close()
    assert _digest(source) == digest_before


def test_adopt_never_overwrites_or_merges_into_an_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _build_v1_database(source)
    target = tmp_path / "target.db"
    get_connection(target).close()
    source_digest, target_digest = _digest(source), _digest(target)

    result = adopt_legacy_database(source, target)

    assert result.status == LegacyAdoptionStatus.TARGET_EXISTS
    assert _digest(source) == source_digest
    assert _digest(target) == target_digest


def test_adopt_without_legacy_database_does_nothing(tmp_path: Path) -> None:
    target = tmp_path / "target.db"

    result = adopt_legacy_database(tmp_path / "missing.db", target)

    assert result.status == LegacyAdoptionStatus.NO_LEGACY_DATABASE
    assert not target.exists()


def test_adopt_same_file_is_retained_in_place(tmp_path: Path) -> None:
    path = tmp_path / "executions.db"
    _build_v1_database(path)

    assert adopt_legacy_database(path, path).status == LegacyAdoptionStatus.SAME_FILE


def test_adopt_unreadable_legacy_file_fails_without_creating_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    source.write_bytes(b"this is not a sqlite database" * 100)
    target = tmp_path / "user" / "executions.db"

    with pytest.raises(LegacyAdoptionError):
        adopt_legacy_database(source, target)

    assert not target.exists()
    assert source.read_bytes() == b"this is not a sqlite database" * 100


def _configure_locations(monkeypatch, tmp_path: Path, *, overridden: bool) -> tuple[Path, Path]:
    legacy_dir = tmp_path / "checkout" / "data"
    user_dir = tmp_path / "user" / "ScheduleMaxing"
    legacy_dir.mkdir(parents=True)
    _build_v1_database(legacy_dir / settings.EXECUTION_DB_FILENAME)
    monkeypatch.setattr(settings, "LEGACY_DATA_DIR", legacy_dir)
    monkeypatch.setattr(settings, "DATA_DIR", user_dir)
    monkeypatch.setattr(settings, "DATA_DIR_OVERRIDDEN", overridden)
    return legacy_dir / settings.EXECUTION_DB_FILENAME, user_dir / settings.EXECUTION_DB_FILENAME


def test_default_get_connection_adopts_legacy_database_once(tmp_path: Path, monkeypatch, caplog) -> None:
    legacy, target = _configure_locations(monkeypatch, tmp_path, overridden=False)
    legacy_digest = _digest(legacy)

    with caplog.at_level("WARNING", logger=db_module.__name__):
        connection = get_connection()
    try:
        assert ExecutionRepository(connection).get_execution("legacy-1").task_name == "Old Task"
        connection.execute(
            "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, "
            "created_at, updated_at) VALUES ('new-1', 'New', 'study', '', 30, 5, 'scheduled', "
            "'2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')"
        )
    finally:
        connection.close()
    assert "Copied the repository-local database" in caplog.text

    # A second open uses the adopted copy (with its new row) and never
    # re-copies or merges the legacy file, which stays byte-identical.
    connection = get_connection()
    try:
        ids = {row["id"] for row in connection.execute("SELECT id FROM executions")}
        assert ids == {"legacy-1", "not-a-uuid-legacy-id", "new-1"}
    finally:
        connection.close()
    assert _digest(legacy) == legacy_digest
    legacy_connection = sqlite3.connect(str(legacy))
    try:
        assert legacy_connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        legacy_connection.close()


def test_env_override_or_explicit_path_never_adopts(tmp_path: Path, monkeypatch) -> None:
    _, target = _configure_locations(monkeypatch, tmp_path, overridden=True)

    get_connection().close()
    connection = get_connection(tmp_path / "explicit.db")
    try:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    finally:
        connection.close()

    fresh = get_connection(target)
    try:
        assert fresh.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
    finally:
        fresh.close()
