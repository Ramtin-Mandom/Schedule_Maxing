"""Schema v7 (Milestone 5, placement provenance) in app/execution/db.py: a
populated v6 database -- legacy and canonical executions with work
sessions, planning rows, a tombstone, and synchronization state -- upgrades
with every existing value intact, the new columns NULL (unknown, never
back-filled), no change-capture marks, and working constraints; old
placements can then be rescheduled without inventing their past."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.execution.db import LATEST_SCHEMA_VERSION, get_connection, initialize_schema
from app.execution.repository import ExecutionRepository
from app.execution.service import ExecutionService
from app.planning import workflow
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository
from tests.execution.test_migration_v4 import (
    V3_PLACEMENT_ID,
    V3_TASK_ID,
    _build_v3_database_with_planning_rows,
    without_block_completions,
)

TOMBSTONE_ID = "66666666-6666-4666-8666-666666666666"
ACCOUNT = "http://backend.test#77777777-7777-4777-8777-777777777777"
TABLES = ("executions", "work_sessions", "tasks", "scheduled_tasks", "sync_accounts", "sync_shadows", "sync_outbox",
          "sync_conflicts", "sync_dirty", "execution_wire_ids")


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return without_block_completions(
        conn, table, [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")])


def _build_v6_database(db_path: Path) -> None:
    _build_v3_database_with_planning_rows(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=6)
    conn.execute(
        "INSERT INTO scheduled_tasks (id, task_id, planned_date, timezone, planned_start, planned_end, "
        "planned_start_utc, planned_end_utc, score, created_at, updated_at, version, deleted_at) VALUES (?, ?, "
        "'2024-06-04', 'UTC', '2024-06-04T09:00:00+00:00', '2024-06-04T10:00:00+00:00', '2024-06-04T09:00:00.000000Z', "
        "'2024-06-04T10:00:00.000000Z', 1.0, '2024-06-02T11:00:00+00:00', '2024-06-05T11:00:00+00:00', 3, "
        "'2024-06-05T11:00:00+00:00')",
        (TOMBSTONE_ID, V3_TASK_ID),
    )
    conn.execute("INSERT INTO sync_accounts (account_key, backend_url, user_id, email, pull_cursor, active, created_at, "
                  "last_synced_at) VALUES (?, 'http://backend.test', '77777777-7777-4777-8777-777777777777', "
                  "'a@example.com', 12, 0, '2024-06-01T00:00:00+00:00', '2024-06-05T00:00:00+00:00')", (ACCOUNT,))
    conn.execute("INSERT INTO sync_shadows VALUES (?, 'placement', ?, 4, 0, '{\"id\": \"x\"}')", (ACCOUNT, V3_PLACEMENT_ID))
    conn.execute("INSERT INTO sync_outbox (op_id, account_key, entity_type, entity_id, local_id, kind, base_version, "
                 "payload, local_rev, state, created_at) VALUES ('op-1', ?, 'placement', ?, ?, 'delete', 4, NULL, 2, "
                 "'pending', '2024-06-05T00:00:00+00:00')", (ACCOUNT, TOMBSTONE_ID, TOMBSTONE_ID))
    conn.execute("INSERT INTO sync_conflicts (id, account_key, entity_type, entity_id, local_id, kind, status, "
                 "created_at) VALUES ('c-1', ?, 'task', ?, ?, 'pull_conflict', 'open', '2024-06-05T00:00:00+00:00')",
                 (ACCOUNT, V3_TASK_ID, V3_TASK_ID))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    conn.close()


def test_a_populated_v6_database_upgrades_without_touching_existing_values(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v6_database(db_path)
    raw = sqlite3.connect(str(db_path))
    before = {table: _rows(raw, table) for table in TABLES}
    raw.close()
    assert before["work_sessions"] and before["executions"] and before["sync_dirty"]

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 7
        after = {table: _rows(conn, table) for table in TABLES}
        if "sync_dirty" in after:
            # Later schemas (v12/v13) derive a type for each task and queue those new records for upload; the
            # marks that existed before the upgrade are compared unchanged.
            after["sync_dirty"] = [row for row in after["sync_dirty"] if row["entity_type"] != "task_type"]
        for table in TABLES:
            old_columns = before[table][0].keys() if before[table] else ()
            assert [{column: row[column] for column in old_columns} for row in after[table]] == before[table], table
        assert all((row["task_category"], row["removal_reason"], row["superseded_by_id"]) == (None, None, None)
                   for row in after["scheduled_tasks"])  # unknown, never back-filled from the current task
        assert after["sync_dirty"] == before["sync_dirty"]  # the migration itself captured nothing

        with pytest.raises(sqlite3.IntegrityError):  # removal provenance only on a tombstone
            conn.execute("UPDATE scheduled_tasks SET removal_reason = 'deleted' WHERE id = ?", (V3_PLACEMENT_ID,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE scheduled_tasks SET removal_reason = 'vanished' WHERE id = ?", (TOMBSTONE_ID,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE scheduled_tasks SET superseded_by_id = id WHERE id = ?", (TOMBSTONE_ID,))

        initialize_schema(conn)  # a repeated initialization is a no-op
        again = {table: _rows(conn, table) for table in TABLES}
        again["sync_dirty"] = [row for row in again["sync_dirty"] if row["entity_type"] != "task_type"]
        assert again == after
    finally:
        conn.close()


def test_an_old_placement_can_be_moved_and_keeps_its_unknown_history(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v6_database(db_path)
    conn = get_connection(db_path)
    try:
        planning = PlanningService(PlanningRepository(conn))
        executions = ExecutionService(ExecutionRepository(conn))
        old = planning.get_placement(V3_PLACEMENT_ID)
        execution = executions.get_or_create_canonical_execution(planning.get_task(old.task_id), old)
        start = datetime(2024, 6, 3, 14, tzinfo=timezone.utc)

        moved = workflow.reschedule_placement(
            planning, old.id, expected_version=old.version, planned_date=date(2024, 6, 3), timezone_name="UTC",
            planned_start=start, planned_end=start.replace(hour=15))

        assert moved.previous.task_category is None  # its category when planned was never recorded
        assert moved.replacement.task_category == "study"  # the new placement's snapshot is taken now
        assert moved.cancelled_execution_id == execution.id
        assert planning.get_placement(TOMBSTONE_ID, include_deleted=True).removal_reason is None  # still unknown
    finally:
        conn.close()


def test_a_failing_v7_leaves_a_usable_v6(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v6_database(db_path)
    raw = sqlite3.connect(str(db_path))
    raw.execute("ALTER TABLE scheduled_tasks ADD COLUMN superseded_by_id TEXT")  # v7's third ADD COLUMN now fails
    raw.commit()
    before = {table: _rows(raw, table) for table in TABLES}
    raw.close()

    from app.execution.db import MigrationError

    with pytest.raises(MigrationError, match="schema v7"):
        get_connection(db_path)
    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {row[1] for row in raw.execute("PRAGMA table_info(scheduled_tasks)")}
        assert "task_category" not in columns and "removal_reason" not in columns  # the first two rolled back too
        assert {table: _rows(raw, table) for table in TABLES} == before
    finally:
        raw.close()
