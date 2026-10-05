"""Schema v12 in app/execution/db.py (docs/productivity-redesign-plan.md, contract A): a populated v11 database
-- two unrelated tasks with the same name, a recurring series, its continuing segment and an occurrence of each,
a deleted task, a placement and synchronization state -- upgrades with every existing value intact. Every task
gets its deterministic type (occurrences and continued segments share their series root's; same-name tasks stay
apart), existing placements keep unknown snapshots, nothing is marked for synchronization, and the result
survives a restart. A failing v12 leaves the database at v11."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import LATEST_SCHEMA_VERSION, MigrationError, get_connection, initialize_schema
from app.planning.models import derived_task_type_id
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository

READING_A = "11111111-1111-4111-8111-111111111111"
READING_B = "22222222-2222-4222-8222-222222222222"
SERIES = "33333333-3333-4333-8333-333333333333"
SEGMENT = "44444444-4444-4444-8444-444444444444"
DELETED = "55555555-5555-4555-8555-555555555555"
PLACEMENT = "66666666-6666-4666-8666-666666666666"
STAMP = "2026-02-01T10:00:00+00:00"
MON, NEXT_MON = date(2026, 3, 2), date(2026, 3, 9)
OCCURRENCE = str(occurrence_task_id(uuid.UUID(SERIES), MON))
LATER_OCCURRENCE = str(occurrence_task_id(uuid.UUID(SEGMENT), NEXT_MON))
SNAPSHOT_COLUMNS = ("task_name", "task_tags", "task_points", "task_estimate_minutes", "task_type_id", "task_type_label")


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


def _build_v11_database(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=11)
    task_sql = (
        "INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, required_date, "
        "recurrence_frequency, recurrence_interval, recurrence_start_date, recurrence_timezone, series_id, "
        "occurrence_slot, series_predecessor_id, created_at, updated_at, version, deleted_at) "
        "VALUES (?, ?, 'study', 30, 5, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3, ?)")
    series = ("weekly", 1)
    for row in (
        (READING_A, "Reading", None, None, None, None, None, None, None, None, None),
        (READING_B, "Reading", None, None, None, None, None, None, None, None, None),
        (SERIES, "Standup", None, *series, MON.isoformat(), "UTC", None, None, None, None),
        (SEGMENT, "Standup (new time)", None, *series, NEXT_MON.isoformat(), "UTC", None, None, SERIES, None),
        (OCCURRENCE, "Standup", MON.isoformat(), None, None, None, None, SERIES, MON.isoformat(), None, None),
        (LATER_OCCURRENCE, "Standup (new time)", NEXT_MON.isoformat(), None, None, None, None, SEGMENT,
         NEXT_MON.isoformat(), None, None),
        (DELETED, "Old chore", None, None, None, None, None, None, None, None, STAMP),
    ):
        conn.execute(task_sql, (*row[:10], STAMP, STAMP, row[10]))
    conn.execute(
        "INSERT INTO scheduled_tasks (id, task_id, planned_date, timezone, planned_start, planned_end, "
        "planned_start_utc, planned_end_utc, score, task_category, created_at, updated_at, version) "
        "VALUES (?, ?, ?, 'UTC', ?, ?, ?, ?, 1.5, 'study', ?, ?, 1)",
        (PLACEMENT, READING_A, MON.isoformat(), f"{MON}T09:00:00+00:00", f"{MON}T09:30:00+00:00",
         f"{MON}T09:00:00.000000Z", f"{MON}T09:30:00.000000Z", STAMP, STAMP))
    conn.execute("DELETE FROM sync_dirty")  # as after a completed synchronization
    conn.close()


def test_a_v11_database_upgrades_with_deterministic_types_and_unknown_snapshots(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v11_database(db_path)
    raw = sqlite3.connect(str(db_path))
    before = {table: _rows(raw, table) for table in ("tasks", "scheduled_tasks")}
    raw.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 12
        tasks = {row["id"]: row for row in _rows(conn, "tasks")}
        types = {task_id: row["task_type_id"] for task_id, row in tasks.items()}

        def derived(root: str) -> str:
            return str(derived_task_type_id(uuid.UUID(root)))

        # Unrelated tasks are never grouped by name; a deleted task keeps a type of its own.
        assert types[READING_A] == derived(READING_A) != types[READING_B] == derived(READING_B)
        assert types[DELETED] == derived(DELETED)
        # A series, the segment continuing it and all their occurrences share the type of the oldest root.
        assert {types[SERIES], types[SEGMENT], types[OCCURRENCE], types[LATER_OCCURRENCE]} == {derived(SERIES)}
        assert len({OCCURRENCE, LATER_OCCURRENCE}) == 2  # ... while staying distinct occurrences

        labels = {row["id"]: row["label"] for row in _rows(conn, "task_types")}
        assert labels == {derived(READING_A): "Reading", derived(READING_B): "Reading", derived(SERIES): "Standup",
                          derived(DELETED): "Old chore"}

        # Nothing else of a task changed (no version bump, no new timestamp), and nothing is queued for sync.
        for row in before["tasks"]:
            assert {key: tasks[row["id"]][key] for key in row} == row
        # Schema v13 then queues exactly the new type records for upload -- and nothing else.
        assert {(row["entity_type"], row["entity_id"]) for row in _rows(conn, "sync_dirty")} == {
            ("task_type", type_id) for type_id in labels}
        assert conn.execute("SELECT value FROM sync_control WHERE name = 'applying_remote'").fetchone()[0] == 0

        # The existing placement's planning snapshot is unknown: never back-filled from the current task.
        placement = _rows(conn, "scheduled_tasks")[0]
        assert {key: placement[key] for key in before["scheduled_tasks"][0]} == before["scheduled_tasks"][0]
        assert all(placement[column] is None for column in SNAPSHOT_COLUMNS)
        stored = PlanningRepository(conn).get_placements([uuid.UUID(PLACEMENT)])[uuid.UUID(PLACEMENT)]
        assert (stored.task_category, stored.task_name, stored.task_tags, stored.task_points) == ("study", None, None, None)
    finally:
        conn.close()

    reopened = get_connection(db_path)  # a restart changes nothing
    try:
        assert {row["id"]: row["task_type_id"] for row in _rows(reopened, "tasks")} == types
        assert {row["id"]: row["label"] for row in _rows(reopened, "task_types")} == labels
    finally:
        reopened.close()


def test_a_failing_v12_leaves_a_working_v11(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "app.db"
    _build_v11_database(db_path)
    monkeypatch.setattr(db_module, "_V12_STATEMENTS", (*db_module._V12_STATEMENTS, "CREATE INDEX broken ON no_such_table(x)"))
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    with pytest.raises(MigrationError):
        initialize_schema(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 11
    assert "task_type_id" not in {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'task_types'").fetchone()[0] == 0
    raw.close()
    monkeypatch.undo()
    conn = get_connection(db_path)  # the real v12 then applies cleanly
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        conn.close()
