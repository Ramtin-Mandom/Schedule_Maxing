"""Schema v10 in app/execution/db.py (docs/execution-rescheduling.md, "Manual placements"): a populated v9
database -- a generated placement, a placement moved by the user before origins existed (its predecessor is a
'rescheduled' tombstone) and a cancelled execution -- upgrades with every existing value intact, origins and
cancel reasons unknown (NULL), no stored intent and nothing marked for synchronization. The proven move is
recognized as manual intent from its recorded lineage when read (never by rewriting rows, never from
coordinates); the new constraints hold; a failing v10 leaves a working v9."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import LATEST_SCHEMA_VERSION, MigrationError, get_connection, initialize_schema
from app.planning.application import PlanningService
from app.planning.repository import PlanningRepository

TASK = "11111111-1111-4111-8111-111111111111"
ORIGINAL = "22222222-2222-4222-8222-222222222222"
MOVED = "33333333-3333-4333-8333-333333333333"
GENERATED = "44444444-4444-4444-8444-444444444444"
EXECUTION = "55555555-5555-4555-8555-555555555555"
MON = date(2026, 3, 2)
STAMP = "2026-02-01T10:00:00+00:00"
TABLES = ("scheduled_tasks", "executions", "sync_dirty")


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


def _build_v9_database(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=9)
    conn.execute("INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, created_at, "
                 "updated_at, version) VALUES (?, 'Essay', 'study', 60, 5, 0, ?, ?, 2)", (TASK, STAMP, STAMP))
    placement_sql = ("INSERT INTO scheduled_tasks (id, task_id, planned_date, timezone, planned_start, planned_end, "
                     "planned_start_utc, planned_end_utc, score, removal_reason, superseded_by_id, deleted_at, "
                     "created_at, updated_at, version) VALUES (?, ?, ?, 'UTC', ?, ?, ?, ?, 1.5, ?, ?, ?, ?, ?, 1)")
    for placement_id, hour, reason, successor, deleted in (
        (MOVED, 15, None, None, None),
        (ORIGINAL, 9, "rescheduled", MOVED, STAMP),
        (GENERATED, 11, None, None, None),
    ):
        conn.execute(placement_sql, (placement_id, TASK, MON.isoformat(), f"{MON}T{hour:02d}:00:00+00:00",
                                     f"{MON}T{hour:02d}:30:00+00:00", f"{MON}T{hour:02d}:00:00.000000Z",
                                     f"{MON}T{hour:02d}:30:00.000000Z", reason, successor, deleted, STAMP, STAMP))
    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, created_at, "
        "updated_at, task_id, scheduled_task_id, actual_final_end_at, version) "
        "VALUES (?, 'Essay', 'study', 'study', 60, 5, 'cancelled', ?, ?, ?, ?, ?, 2)",
        (EXECUTION, STAMP, STAMP, TASK, GENERATED, "2026-03-02T08:00:00+00:00"),
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    conn.close()


def test_a_populated_v9_database_upgrades_without_guessing(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v9_database(db_path)
    raw = sqlite3.connect(str(db_path))
    before = {table: _rows(raw, table) for table in TABLES}
    raw.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 10
        after = {table: _rows(conn, table) for table in TABLES}
        for table in TABLES:
            old_columns = before[table][0].keys() if before[table] else ()
            assert [{column: row[column] for column in old_columns} for row in after[table]] == before[table], table
        assert {(row["origin"], row["preserved"]) for row in after["scheduled_tasks"]} == {(None, 0)}
        assert [row["cancel_reason"] for row in after["executions"]] == [None]  # unknown, not guessed as "user"
        assert after["sync_dirty"] == before["sync_dirty"]

        planning = PlanningService(PlanningRepository(conn))
        live = planning.placements_for_date(MON)
        assert planning.preserved_placement_ids(live) == {uuid.UUID(MOVED)}  # proven by its recorded lineage
        assert _rows(conn, "scheduled_tasks") == after["scheduled_tasks"]  # recognized when read, not rewritten

        with pytest.raises(sqlite3.IntegrityError):  # only a manual placement can be preserved
            conn.execute("UPDATE scheduled_tasks SET preserved = 1 WHERE id = ?", (GENERATED,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE scheduled_tasks SET origin = 'guessed' WHERE id = ?", (GENERATED,))
        with pytest.raises(sqlite3.IntegrityError):  # only a cancelled execution has a cancel reason
            conn.execute("UPDATE executions SET status = 'scheduled', cancel_reason = 'user' WHERE id = ?",
                         (EXECUTION,))
    finally:
        conn.close()


def test_a_failing_v10_leaves_a_working_v9(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "app.db"
    _build_v9_database(db_path)
    failing = (*db_module._V10_STATEMENTS[:-1], "CREATE INDEX broken ON no_such_table(x)")
    monkeypatch.setattr(db_module, "MIGRATIONS", (*db_module.MIGRATIONS[:9], (10, failing)))
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    with pytest.raises(MigrationError):
        initialize_schema(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 9
    assert "origin" not in {row[1] for row in raw.execute("PRAGMA table_info(scheduled_tasks)")}
    raw.close()
    monkeypatch.undo()
    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        conn.close()
