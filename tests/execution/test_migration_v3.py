"""Tests for schema v3 (Milestone 2 planning persistence) in app/execution/db.py:
the v2 -> v3 upgrade, preservation of pre-existing execution history
(including canonical ids whose parent task/placement was never persisted),
link-time relationship enforcement, ordered/transactional migrations with
rollback, and post-migration integrity checks.

Older-version databases are built for real: a raw v1 database (see
test_migration_v2._build_v1_database), migrated to exactly v2 with
initialize_schema(target_version=2), then populated with v2-era rows.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import (
    LATEST_SCHEMA_VERSION,
    IntegrityCheckError,
    MigrationError,
    get_connection,
    initialize_schema,
)
from app.execution.errors import ExecutionLinkError
from app.execution.models import ExecutionStatus
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning.errors import InvalidEntityError
from app.planning.models import ScheduledTask, Task
from app.planning.repository import PlanningRepository
from tests.execution.test_migration_v2 import _build_v1_database

ORPHAN_TASK_ID = "11111111-1111-4111-8111-111111111111"
ORPHAN_PLACEMENT_ID = "22222222-2222-4222-8222-222222222222"


def _build_v2_database_with_canonical_rows(db_path: Path) -> None:
    """A genuine v2 database: the v1 legacy rows, plus a v2-era canonical
    execution whose task/placement ids were never persisted anywhere (the
    canonical API accepted in-memory models before v3), with a session."""
    _build_v1_database(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=2)
    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, "
        "created_at, updated_at, task_id, scheduled_task_id, user_id, canonical_planned_date, canonical_timezone, "
        "canonical_planned_start, canonical_planned_end, actual_first_start_at, version) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "canonical-orphan", "Orphan Study", "study", "math", 60, 7, "in_progress",
            "2024-06-03T08:00:00+00:00", "2024-06-03T09:00:00+00:00",
            ORPHAN_TASK_ID, ORPHAN_PLACEMENT_ID, None, "2024-06-03", "UTC",
            "2024-06-03T09:00:00+00:00", "2024-06-03T10:00:00+00:00", "2024-06-03T09:05:00+00:00", 3,
        ),
    )
    conn.execute(
        "INSERT INTO work_sessions (execution_id, started_at) VALUES (?, ?)",
        ("canonical-orphan", "2024-06-03T09:05:00+00:00"),
    )
    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, "
        "created_at, updated_at, task_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("task-only-orphan", "Orphan Task-only", "work", "", 30, 5, "scheduled",
         "2024-06-03T07:00:00+00:00", "2024-06-03T07:00:00+00:00", ORPHAN_TASK_ID),
    )
    conn.close()


def _snapshot(conn: sqlite3.Connection, table: str) -> list[dict]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def _raw_rows(db_path: Path, table: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return _snapshot(conn, table)
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _persist(connection, *, task: Task | None = None, placement: ScheduledTask | None = None) -> None:
    repository = PlanningRepository(connection)
    if task is not None:
        repository.upsert_task(task)
    if placement is not None:
        repository.upsert_placement(placement)


def _task(**overrides) -> Task:
    defaults = dict(name="Study", category="study", estimated_duration_minutes=60, priority=5)
    defaults.update(overrides)
    return Task(**defaults)


def _placement(task: Task, hour: int = 9) -> ScheduledTask:
    return ScheduledTask(
        task_id=task.id, planned_date=datetime(2024, 6, 3).date(), timezone="UTC",
        planned_start=datetime(2024, 6, 3, hour, tzinfo=timezone.utc),
        planned_end=datetime(2024, 6, 3, hour + 1, tzinfo=timezone.utc),
    )


# -----------------------------------------------------------------------------
# Fresh schema / reopen / upgrades
# -----------------------------------------------------------------------------


def test_fresh_database_is_latest_with_foreign_keys_on_and_clean_integrity(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "fresh.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION == 3
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_repeated_initialization_and_reopen_change_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)

    conn = get_connection(db_path)
    schema_before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows_before = _snapshot(conn, "executions")
    initialize_schema(conn)
    initialize_schema(conn)
    conn.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == schema_before
        assert _snapshot(conn, "executions") == rows_before
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        conn.close()


def test_upgrade_from_v1_preserves_rows_sessions_ids_and_timestamps(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)
    legacy_rows = _raw_rows(db_path, "executions")
    legacy_sessions = _raw_rows(db_path, "work_sessions")

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        migrated = {row["id"]: row for row in _snapshot(conn, "executions")}
        for legacy in legacy_rows:
            for column, value in legacy.items():
                assert migrated[legacy["id"]][column] == value
        assert _snapshot(conn, "work_sessions") == legacy_sessions
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_upgrade_from_v2_preserves_every_column_of_every_row(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)
    before = _raw_rows(db_path, "executions")
    sessions_before = _raw_rows(db_path, "work_sessions")

    conn = get_connection(db_path)
    try:
        assert _snapshot(conn, "executions") == before
        assert _snapshot(conn, "work_sessions") == sessions_before
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Legacy compatibility: orphan canonical ids are preserved, never fabricated
# -----------------------------------------------------------------------------


def test_orphan_ids_are_preserved_without_fabricating_parents_or_dates(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)

    conn = get_connection(db_path)
    try:
        repository = ExecutionRepository(conn)
        orphan = repository.get_execution("canonical-orphan")
        assert str(orphan.task_id) == ORPHAN_TASK_ID
        assert str(orphan.scheduled_task_id) == ORPHAN_PLACEMENT_ID
        assert orphan.canonical_planned_start == datetime(2024, 6, 3, 9, tzinfo=timezone.utc)
        assert orphan.version == 3

        # No placeholder task/placement was invented for the orphan ids...
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheduled_tasks").fetchone()[0] == 0
        # ...and legacy rows still have no guessed canonical date.
        assert repository.get_execution("legacy-1").canonical_planned_date is None

        unresolved = {execution.id for execution in repository.list_executions_with_unresolved_links()}
        assert unresolved == {"canonical-orphan", "task-only-orphan"}
    finally:
        conn.close()


def test_orphan_rows_keep_working_through_the_execution_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)

    conn = get_connection(db_path)
    try:
        service = ExecutionService(ExecutionRepository(conn))
        service.pause("canonical-orphan")
        service.resume("canonical-orphan")
        completed = service.complete("canonical-orphan")
        service.record_feedback("canonical-orphan", focus_rating=4)
        skipped = service.skip("task-only-orphan")

        assert completed.status == ExecutionStatus.COMPLETED
        assert str(completed.task_id) == ORPHAN_TASK_ID
        assert str(completed.scheduled_task_id) == ORPHAN_PLACEMENT_ID
        assert skipped.status == ExecutionStatus.SKIPPED
        assert len(service.list_sessions("canonical-orphan")) == 2
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Link-time enforcement for newly linked records
# -----------------------------------------------------------------------------


def test_new_execution_for_unpersisted_task_is_rejected(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        service = ExecutionService(ExecutionRepository(conn))
        with pytest.raises(ExecutionLinkError, match="persisted task"):
            service.create_canonical_execution(_task())
        assert service.list_executions() == []
    finally:
        conn.close()


def test_new_execution_for_unpersisted_placement_is_rejected(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task = _task()
        _persist(conn, task=task)
        service = ExecutionService(ExecutionRepository(conn))
        with pytest.raises(ExecutionLinkError, match="persisted placement"):
            service.get_or_create_canonical_execution(task, _placement(task))
    finally:
        conn.close()


def test_execution_task_must_match_its_placement_task(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task_a, task_b = _task(name="A"), _task(name="B")
        placement_a = _placement(task_a)
        _persist(conn, task=task_a)
        _persist(conn, task=task_b, placement=placement_a)
        service = ExecutionService(ExecutionRepository(conn))

        with pytest.raises(ExecutionLinkError, match="different task"):
            service.create_canonical_execution(task_b, placement_a)
        assert service.create_canonical_execution(task_a, placement_a).scheduled_task_id == placement_a.id
    finally:
        conn.close()


def test_scheduled_task_id_without_task_id_is_rejected(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="requires task_id"):
            conn.execute(
                "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, "
                "created_at, updated_at, scheduled_task_id) VALUES ('x', 'X', 'c', '', 5, 5, 'scheduled', "
                "'2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00', ?)",
                (str(uuid.uuid4()),),
            )
    finally:
        conn.close()


def test_execution_identity_is_immutable_after_creation(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task_a, task_b = _task(name="A"), _task(name="B")
        _persist(conn, task=task_a)
        _persist(conn, task=task_b)
        repository = ExecutionRepository(conn)
        execution = ExecutionService(repository).create_canonical_execution(task_a)

        with pytest.raises(ExecutionLinkError, match="immutable"):
            repository.update_execution(execution.model_copy(update={"task_id": task_b.id}))
        assert repository.get_execution(execution.id).task_id == task_a.id
    finally:
        conn.close()


def test_history_referenced_placement_id_cannot_be_restored_for_another_task(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task_a, task_b = _task(name="A"), _task(name="B")
        placement = _placement(task_a)
        _persist(conn, task=task_a, placement=placement)
        _persist(conn, task=task_b)
        ExecutionService(ExecutionRepository(conn)).get_or_create_canonical_execution(task_a, placement)
        planning = PlanningRepository(conn)

        with pytest.raises(InvalidEntityError, match="different task"):
            planning.upsert_placement(placement.model_copy(update={"task_id": task_b.id}))
        planning.delete_placements([placement.id])
        with pytest.raises(InvalidEntityError, match="different task"):
            planning.upsert_placement(placement.model_copy(update={"task_id": task_b.id}))
        planning.upsert_placement(placement)  # the same task may re-store it
    finally:
        conn.close()


def test_scheduled_task_uniqueness_still_holds_at_v3(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "db.sqlite3")
    try:
        task = _task()
        placement = _placement(task)
        _persist(conn, task=task, placement=placement)
        service = ExecutionService(ExecutionRepository(conn))
        service.create_canonical_execution(task, placement)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            service.create_canonical_execution(task, placement)
        assert service.get_or_create_canonical_execution(task, placement).scheduled_task_id == placement.id
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Ordered, transactional migrations and integrity checks
# -----------------------------------------------------------------------------


def test_failed_v3_migration_rolls_back_completely_and_can_be_retried(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "executions.db"
    _build_v1_database(db_path)
    rows_before = _raw_rows(db_path, "executions")

    broken_v3 = (3, db_module._V3_PLANNING_STATEMENTS[:5] + ("THIS IS NOT VALID SQL",))
    monkeypatch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:2] + (broken_v3,))

    with pytest.raises(MigrationError, match="schema v3"):
        get_connection(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        # v2 (applied and committed first, in order) is kept; none of v3's
        # earlier statements (which created tables) survived the rollback.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not _table_exists(conn, "projects")
        assert not _table_exists(conn, "tasks")
        assert len(conn.execute("SELECT * FROM executions").fetchall()) == len(rows_before)
    finally:
        conn.close()

    monkeypatch.undo()
    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert {row["id"] for row in _snapshot(conn, "executions")} == {row["id"] for row in rows_before}
    finally:
        conn.close()


def test_failed_later_migration_rolls_back_only_itself(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "executions.db"
    get_connection(db_path).close()

    failing_v4 = (4, ("CREATE TABLE partial_v4 (x INTEGER)", "INSERT INTO no_such_table VALUES (1)"))
    monkeypatch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS + (failing_v4,))

    with pytest.raises(MigrationError):
        get_connection(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not _table_exists(conn, "partial_v4")
    finally:
        conn.close()


def test_migrations_must_be_contiguous(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db_module, "MIGRATIONS", db_module.MIGRATIONS[:1] + db_module.MIGRATIONS[2:])
    with pytest.raises(MigrationError, match="without gaps"):
        get_connection(tmp_path / "db.sqlite3")


def test_database_newer_than_code_is_refused_untouched(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE something_new (x INTEGER)")
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()

    with pytest.raises(MigrationError, match="newer"):
        get_connection(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 99
        assert not _table_exists(conn, "tasks")
    finally:
        conn.close()


def test_pre_existing_foreign_key_damage_fails_the_integrity_check(tmp_path: Path) -> None:
    db_path = tmp_path / "executions.db"
    _build_v2_database_with_canonical_rows(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO work_sessions (execution_id, started_at) VALUES ('ghost', '2024-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    with pytest.raises(IntegrityCheckError, match="foreign_key_check"):
        get_connection(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2  # v3 rolled back, nothing hidden
    finally:
        conn.close()
