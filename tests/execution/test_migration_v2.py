"""Tests for app/execution/db.py's version 1 -> 2 migration (Task 2 / Schedule
Maxing v2 identity migration): rebuilding `executions` to add `cancelled` to
the status CHECK constraint, relax planned_date/planned_start/planned_end to
nullable, and add the canonical identity/timestamp columns + partial unique
index -- without losing any existing row, session, or foreign key.

Every database here is built directly against a raw sqlite3 connection
(bypassing app.execution.db entirely) to construct a genuine pre-migration
v1 database, then handed to app.execution.db.get_connection/initialize_schema
to exercise the real migration path. Tests about v2's own semantics stop the
migration at v2 (initialize_schema(target_version=2)); v3's link triggers
and the v2 -> v3 upgrade are covered in test_migration_v3.py.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.execution.db import LATEST_SCHEMA_VERSION, get_connection, initialize_schema
from app.execution.repository import ExecutionRepository

_V1_EXECUTIONS_SQL = """
CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    category TEXT NOT NULL,
    tag TEXT NOT NULL,
    planned_date INTEGER NOT NULL,
    planned_start INTEGER NOT NULL,
    planned_end INTEGER NOT NULL,
    planned_duration INTEGER NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
    status TEXT NOT NULL CHECK (
        status IN ('scheduled', 'in_progress', 'paused', 'completed', 'skipped')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actual_active_duration_minutes REAL,
    duration_variance_minutes REAL,
    start_delay_minutes REAL,
    focus_rating INTEGER CHECK (focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5),
    energy_rating INTEGER CHECK (energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5),
    interruption_count INTEGER CHECK (interruption_count IS NULL OR interruption_count >= 0),
    note TEXT
)
"""

_V1_WORK_SESSIONS_SQL = """
CREATE TABLE work_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    ended_at TEXT
)
"""


def _build_v1_database(db_path: Path, *, extra_rows: list[dict] | None = None) -> None:
    """Create a standalone pre-migration v1 database with two executions
    (one completed with feedback + a session, one still scheduled with a
    non-UUID legacy id) and one work session."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_V1_EXECUTIONS_SQL)
    conn.execute(_V1_WORK_SESSIONS_SQL)
    conn.execute("CREATE INDEX idx_work_sessions_execution_id ON work_sessions(execution_id)")
    conn.execute("PRAGMA user_version = 1")

    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_date, planned_start, planned_end, "
        "planned_duration, priority, status, created_at, updated_at, focus_rating, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "legacy-1", "Old Task", "study", "math", 1, 540, 600, 60, 8, "completed",
            "2023-01-01T09:00:00+00:00", "2023-01-01T10:00:00+00:00", 4, "a legacy note",
        ),
    )
    conn.execute(
        "INSERT INTO work_sessions (execution_id, started_at, ended_at) VALUES (?,?,?)",
        ("legacy-1", "2023-01-01T09:00:00+00:00", "2023-01-01T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_date, planned_start, planned_end, "
        "planned_duration, priority, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "not-a-uuid-legacy-id", "Weird Legacy", "work", "", 1, 600, 660, 60, 5, "scheduled",
            "2023-01-01T00:00:00+00:00", "2023-01-01T00:00:00+00:00",
        ),
    )
    for row in extra_rows or []:
        conn.execute(
            "INSERT INTO executions (id, task_name, category, tag, planned_date, planned_start, planned_end, "
            "planned_duration, priority, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"], row["task_name"], row["category"], row["tag"], row["planned_date"],
                row["planned_start"], row["planned_end"], row["planned_duration"], row["priority"],
                row["status"], row["created_at"], row["updated_at"],
            ),
        )
    conn.commit()
    conn.close()


def _open_at_v2(db_path: Path) -> sqlite3.Connection:
    """Migrate a v1 database to exactly v2 (not further) and return the connection."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=2)
    return conn


def test_upgrade_v1_database_reaches_version_2(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = _open_at_v2(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        conn.close()


def test_upgrade_v1_database_through_get_connection_reaches_latest(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        conn.close()


def test_upgrade_preserves_row_count(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_upgrade_preserves_ids_including_non_uuid_legacy_id(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        ids = {row[0] for row in conn.execute("SELECT id FROM executions").fetchall()}
        assert ids == {"legacy-1", "not-a-uuid-legacy-id"}
    finally:
        conn.close()


def test_upgrade_preserves_snapshot_fields_and_feedback(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM executions WHERE id = ?", ("legacy-1",)).fetchone()
        assert dict(row)["task_name"] == "Old Task"
        assert dict(row)["category"] == "study"
        assert dict(row)["planned_duration"] == 60
        assert dict(row)["priority"] == 8
        assert dict(row)["focus_rating"] == 4
        assert dict(row)["note"] == "a legacy note"
    finally:
        conn.close()


def test_upgrade_preserves_work_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        sessions = conn.execute("SELECT * FROM work_sessions").fetchall()
        assert len(sessions) == 1
        assert dict(sessions[0])["execution_id"] == "legacy-1"
        assert dict(sessions[0])["ended_at"] == "2023-01-01T10:00:00+00:00"
    finally:
        conn.close()


def test_upgrade_leaves_foreign_keys_consistent(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert problems == []
        # The rebuilt table's FK must still be enforced going forward.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_sessions (execution_id, started_at) VALUES (?, ?)",
                ("does-not-exist", "2024-01-01T00:00:00+00:00"),
            )
    finally:
        conn.close()


def test_upgrade_leaves_unknown_canonical_and_legacy_date_fields_unset(tmp_path: Path) -> None:
    """Old integer planned_date has no recoverable calendar anchor -- migrated
    rows must never have a canonical date/task/placement guessed for them."""
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        repo = ExecutionRepository(conn)
        execution = repo.get_execution("legacy-1")
        assert execution.task_id is None
        assert execution.scheduled_task_id is None
        assert execution.canonical_planned_date is None
        assert execution.canonical_timezone is None
        assert execution.canonical_planned_start is None
        # The legacy day-index snapshot itself IS preserved (it was never unknown).
        assert execution.planned_date == 1
        assert execution.planned_start == 540
    finally:
        conn.close()


def test_status_check_constraint_now_accepts_cancelled(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = get_connection(db_path)
    try:
        # Would raise sqlite3.IntegrityError against the old (pre-migration)
        # CHECK constraint, which only listed the original five statuses.
        conn.execute(
            "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("cancelled-1", "Cancelled Task", "study", "", 30, 5, "cancelled",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00"),
        )
        conn.commit()
        row = conn.execute("SELECT status FROM executions WHERE id = ?", ("cancelled-1",)).fetchone()
        assert row["status"] == "cancelled"
    finally:
        conn.close()


def test_planned_date_start_end_are_now_nullable(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    # At v2 a task_id is plain historical identity (v3 additionally requires
    # a newly inserted link to reference a persisted task).
    conn = _open_at_v2(db_path)
    try:
        # Would raise against the old NOT NULL constraints.
        conn.execute(
            "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, "
            "status, created_at, updated_at, task_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("canonical-1", "Canonical Task", "study", "", 45, 5, "scheduled",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "11111111-1111-1111-1111-111111111111"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT planned_date, planned_start, planned_end FROM executions WHERE id = ?", ("canonical-1",)
        ).fetchone()
        assert dict(row) == {"planned_date": None, "planned_start": None, "planned_end": None}
    finally:
        conn.close()


def test_scheduled_task_id_uniqueness_enforced_by_partial_index(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn = _open_at_v2(db_path)
    try:
        insert_sql = (
            "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, "
            "status, created_at, updated_at, scheduled_task_id) VALUES (?,?,?,?,?,?,?,?,?,?)"
        )
        conn.execute(
            insert_sql,
            ("c1", "Task A", "study", "", 30, 5, "scheduled",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "placement-1"),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert_sql,
                ("c2", "Task B", "study", "", 30, 5, "scheduled",
                 "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", "placement-1"),
            )

        # NULL scheduled_task_id is exempt from the partial unique index --
        # any number of task-only executions may coexist.
        conn.execute(
            insert_sql,
            ("c3", "Task C", "study", "", 30, 5, "scheduled",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", None),
        )
        conn.execute(
            insert_sql,
            ("c4", "Task D", "study", "", 30, 5, "scheduled",
             "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00", None),
        )
        conn.commit()
    finally:
        conn.close()


def test_reopening_and_upgrading_twice_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)

    conn1 = get_connection(db_path)
    initialize_schema(conn1)
    initialize_schema(conn1)
    version_after_double_init = conn1.execute("PRAGMA user_version").fetchone()[0]
    count_after_double_init = conn1.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    conn1.close()

    conn2 = get_connection(db_path)
    try:
        assert conn2.execute("PRAGMA user_version").fetchone()[0] == version_after_double_init == LATEST_SCHEMA_VERSION
        assert conn2.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == count_after_double_init == 2
    finally:
        conn2.close()


def test_upgrading_an_already_current_database_is_a_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    # get_connection on a brand-new path creates a fresh, already-current database.
    conn = get_connection(db_path)
    try:
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        initialize_schema(conn)
        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version_before == version_after == LATEST_SCHEMA_VERSION
    finally:
        conn.close()
