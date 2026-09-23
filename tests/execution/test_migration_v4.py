"""Tests for schema v4 (Milestone 3, sync-ready local records) in app/execution/db.py:
upgrades from genuine v1/v2/v3 databases preserve every id, timestamp,
snapshot, and work session; new metadata columns get honest defaults;
non-UUID execution ids get one durable wire id; repeated initialization is a
no-op; a failed v4 leaves v3 fully usable; and the recreated link trigger
ignores tombstones.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import LATEST_SCHEMA_VERSION, MigrationError, get_connection, initialize_schema
from app.execution.errors import ExecutionLinkError
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.models import Task
from app.planning.repository import PlanningRepository
from tests.execution.test_migration_v2 import _build_v1_database
from tests.execution.test_migration_v3 import ORPHAN_TASK_ID, _build_v2_database_with_canonical_rows

V3_TASK_ID = "33333333-3333-4333-8333-333333333333"
V3_BLOCK_ID = "44444444-4444-4444-8444-444444444444"
V3_PLACEMENT_ID = "55555555-5555-4555-8555-555555555555"


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def _build_v3_database_with_planning_rows(db_path: Path) -> None:
    """A genuine v3 database: v1 legacy + v2 canonical executions, plus v3-era planning rows."""
    _build_v2_database_with_canonical_rows(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=3)
    conn.execute(
        "INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, "
        "created_at, updated_at, version) VALUES (?, 'Study', 'study', 60, 5, 0, ?, ?, 4)",
        (V3_TASK_ID, "2024-06-01T10:00:00+00:00", "2024-06-02T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO fixed_blocks (id, label, planned_date, timezone, planned_start, planned_end, "
        "planned_start_utc, planned_end_utc) VALUES (?, 'Sleep', '2024-06-03', 'UTC', "
        "'2024-06-03T00:00:00+00:00', '2024-06-03T08:00:00+00:00', "
        "'2024-06-03T00:00:00.000000Z', '2024-06-03T08:00:00.000000Z')",
        (V3_BLOCK_ID,),
    )
    conn.execute(
        "INSERT INTO scheduled_tasks (id, task_id, planned_date, timezone, planned_start, planned_end, "
        "planned_start_utc, planned_end_utc, score, created_at, updated_at, version) VALUES (?, ?, '2024-06-03', "
        "'UTC', '2024-06-03T09:00:00+00:00', '2024-06-03T10:00:00+00:00', '2024-06-03T09:00:00.000000Z', "
        "'2024-06-03T10:00:00.000000Z', 7.5, '2024-06-02T11:00:00+00:00', '2024-06-02T11:00:00+00:00', 2)",
        (V3_PLACEMENT_ID, V3_TASK_ID),
    )
    conn.close()


@pytest.mark.parametrize("builder", [_build_v1_database, _build_v2_database_with_canonical_rows,
                                     _build_v3_database_with_planning_rows])
def test_upgrade_preserves_every_existing_value_and_session(tmp_path: Path, builder) -> None:
    db_path = tmp_path / "executions.db"
    builder(db_path)
    raw = sqlite3.connect(str(db_path))
    before = {table: _rows(raw, table) for table in ("executions", "work_sessions")}
    raw.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 4
        after = _rows(conn, "executions")
        assert [{column: row[column] for column in before["executions"][0]} for row in after] == before["executions"]
        assert all(row["deleted_at"] is None for row in after)
        assert _rows(conn, "work_sessions") == before["work_sessions"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_v3_planning_rows_keep_their_values_and_get_explicit_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v3_database_with_planning_rows(db_path)

    conn = get_connection(db_path)
    try:
        planning = PlanningRepository(conn)
        task = planning.get_task(uuid.UUID(V3_TASK_ID))
        assert (task.version, task.deleted_at, task.user_id) == (4, None, None)
        assert task.created_at == datetime(2024, 6, 1, 10, tzinfo=timezone.utc)

        [block] = planning.list_fixed_blocks(datetime(2024, 6, 3).date(), datetime(2024, 6, 3).date())
        assert str(block.id) == V3_BLOCK_ID
        assert block.category == "fixed" and block.version == 1 and block.deleted_at is None
        assert block.created_at == block.updated_at  # the migration instant: first recorded, never guessed earlier

        [placement] = planning.list_placements(datetime(2024, 6, 3).date(), datetime(2024, 6, 3).date())
        assert (str(placement.id), placement.version, placement.score) == (V3_PLACEMENT_ID, 2, 7.5)
    finally:
        conn.close()


def test_non_uuid_execution_ids_get_one_durable_wire_id(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)

    conn = get_connection(db_path)
    repository = ExecutionRepository(conn)
    wire = repository.wire_id("legacy-1")
    assert repository.get_execution("legacy-1").id == "legacy-1"  # the local id is never reminted
    conn.close()

    conn = get_connection(db_path)
    try:
        repository = ExecutionRepository(conn)
        assert repository.wire_id("legacy-1") == wire  # stable across reopen
        assert repository.wire_id("canonical-orphan") != wire
        created = ExecutionService(repository).create_execution(
            task_name="New", category="c", tag="t", planned_date=1, planned_start=0, planned_end=30,
            planned_duration=30, priority=5,
        )
        assert repository.wire_id(created.id) == uuid.UUID(created.id)  # a UUID id is its own wire id
        mapped = {row[0] for row in conn.execute("SELECT execution_id FROM execution_wire_ids")}
        assert created.id not in mapped and "legacy-1" in mapped
    finally:
        conn.close()


def test_repeated_initialization_is_a_no_op(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v3_database_with_planning_rows(db_path)
    conn = get_connection(db_path)
    schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    wire_ids = conn.execute("SELECT * FROM execution_wire_ids ORDER BY 1").fetchall()
    initialize_schema(conn)
    initialize_schema(conn)
    conn.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
        assert conn.execute("SELECT * FROM execution_wire_ids ORDER BY 1").fetchall() == wire_ids
    finally:
        conn.close()


def test_failed_v4_rolls_back_to_a_usable_v3(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "executions.db"
    _build_v3_database_with_planning_rows(db_path)
    monkeypatch.setattr(db_module, "_V4_STATEMENTS", db_module._V4_STATEMENTS[:2] + ("NOT VALID SQL",))

    with pytest.raises(MigrationError, match="schema v4"):
        get_connection(db_path)

    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
        assert "deleted_at" not in {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}  # ALTERs rolled back
        assert raw.execute("SELECT name FROM sqlite_master WHERE name = 'preference_overrides'").fetchone() is None
    finally:
        raw.close()

    monkeypatch.undo()
    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert PlanningRepository(conn).get_task(uuid.UUID(V3_TASK_ID)).version == 4
    finally:
        conn.close()


def test_new_executions_cannot_link_to_a_tombstoned_task(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task = Task(name="Gone", category="study", estimated_duration_minutes=30, priority=5)
        planning = PlanningRepository(conn)
        planning.insert_task(task)
        planning.soft_delete_task(task.id, deleted_at=datetime.now(timezone.utc), expected_version=1)

        with pytest.raises(ExecutionLinkError, match="persisted task"):
            ExecutionService(ExecutionRepository(conn)).create_canonical_execution(task)
    finally:
        conn.close()


def test_orphan_history_from_v2_is_still_reported_unresolved(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)
    conn = get_connection(db_path)
    try:
        orphan = ExecutionRepository(conn).get_execution("canonical-orphan")
        assert str(orphan.task_id) == ORPHAN_TASK_ID
        assert {e.id for e in ExecutionRepository(conn).list_executions_with_unresolved_links()} == {
            "canonical-orphan", "task-only-orphan"
        }
    finally:
        conn.close()
