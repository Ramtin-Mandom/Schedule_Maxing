"""Schema v9 in app/execution/db.py (docs/recurrence.md): a populated v8 database -- a legacy recurring
template, its placements on two dates (one date ambiguous: two live placements), a completed execution
recorded against one of them, an ordinary task and synchronization state -- upgrades with every existing
value intact, the new columns NULL (the template needs configuration; nothing is guessed), nothing marked
for synchronization, and working constraints. Expanding afterwards maps the unambiguous legacy slot to an
occurrence without rewriting the placement or the execution's historical ids; the ambiguous date is
reported. A failing v9 leaves the database at v8."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date
from pathlib import Path

import pytest

from app.execution import db as db_module
from app.execution.db import LATEST_SCHEMA_VERSION, MigrationError, get_connection, initialize_schema
from app.planning import series as series_ops
from app.planning.application import PlanningService
from app.planning.recurrence import occurrence_task_id
from app.planning.repository import PlanningRepository

TEMPLATE = "11111111-1111-4111-8111-111111111111"
ORDINARY = "22222222-2222-4222-8222-222222222222"
PLACED = "33333333-3333-4333-8333-333333333333"
TWIN_A = "44444444-4444-4444-8444-444444444444"
TWIN_B = "55555555-5555-4555-8555-555555555555"
EXECUTION = "66666666-6666-4666-8666-666666666666"
MON, TUE = date(2026, 3, 2), date(2026, 3, 3)
STAMP = "2026-02-01T10:00:00+00:00"
TABLES = ("tasks", "task_recurrence_weekdays", "scheduled_tasks", "executions", "work_sessions", "sync_dirty")


def _rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


def _build_v8_database(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=8)
    task_sql = ("INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, "
                "recurrence_frequency, recurrence_interval, created_at, updated_at, version) "
                "VALUES (?, ?, 'work', 15, 5, 0, ?, ?, ?, ?, 3)")
    conn.execute(task_sql, (TEMPLATE, "Standup", "weekly", 1, STAMP, STAMP))
    conn.execute(task_sql, (ORDINARY, "Report", None, None, STAMP, STAMP))
    conn.execute("INSERT INTO task_recurrence_weekdays (task_id, weekday) VALUES (?, 0), (?, 1)", (TEMPLATE, TEMPLATE))
    placement_sql = ("INSERT INTO scheduled_tasks (id, task_id, planned_date, timezone, planned_start, planned_end, "
                     "planned_start_utc, planned_end_utc, score, created_at, updated_at, version) "
                     "VALUES (?, ?, ?, 'UTC', ?, ?, ?, ?, 1.5, ?, ?, 1)")
    for placement_id, day, hour in ((PLACED, MON, 9), (TWIN_A, TUE, 9), (TWIN_B, TUE, 11)):
        start, end = f"{day}T{hour:02d}:00:00+00:00", f"{day}T{hour:02d}:15:00+00:00"
        conn.execute(placement_sql, (placement_id, TEMPLATE, day.isoformat(), start, end,
                                     f"{day}T{hour:02d}:00:00.000000Z", f"{day}T{hour:02d}:15:00.000000Z", STAMP, STAMP))
    conn.execute(
        "INSERT INTO executions (id, task_name, category, tag, planned_duration, priority, status, created_at, "
        "updated_at, task_id, scheduled_task_id, canonical_planned_date, canonical_timezone, actual_final_end_at, "
        "version) VALUES (?, 'Standup', 'work', 'work', 15, 5, 'completed', ?, ?, ?, ?, ?, 'UTC', ?, 4)",
        (EXECUTION, STAMP, STAMP, TEMPLATE, PLACED, MON.isoformat(), "2026-03-02T09:20:00+00:00"),
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    conn.close()


def test_a_populated_v8_database_upgrades_without_touching_existing_values(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v8_database(db_path)
    raw = sqlite3.connect(str(db_path))
    before = {table: _rows(raw, table) for table in TABLES}
    raw.close()

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 9
        after = {table: _rows(conn, table) for table in TABLES}
        for table in TABLES:
            old_columns = before[table][0].keys() if before[table] else ()
            assert [{column: row[column] for column in old_columns} for row in after[table]] == before[table], table
        new_columns = ("recurrence_start_date", "recurrence_timezone", "series_id", "occurrence_slot",
                       "occurrence_state", "series_version", "series_predecessor_id")
        assert all(row[column] is None for row in after["tasks"] for column in new_columns)  # nothing guessed
        assert after["sync_dirty"] == before["sync_dirty"]  # the migration itself captured nothing

        planning = PlanningService(PlanningRepository(conn))
        template = planning.get_task(uuid.UUID(TEMPLATE))
        assert template.needs_configuration and template.recurrence.weekdays == [0, 1]

        with pytest.raises(sqlite3.IntegrityError):  # an anchor needs its time zone
            conn.execute("UPDATE tasks SET recurrence_start_date = '2026-03-02' WHERE id = ?", (TEMPLATE,))
        with pytest.raises(sqlite3.IntegrityError):  # an ordinary task cannot claim an exception state
            conn.execute("UPDATE tasks SET occurrence_state = 'skipped' WHERE id = ?", (ORDINARY,))

        result = series_ops.expand_occurrences(planning, MON, TUE)
        assert result.needs_configuration == [uuid.UUID(TEMPLATE)]
        [mapped] = result.created
        assert mapped.id == occurrence_task_id(uuid.UUID(TEMPLATE), MON) and mapped.required_date == MON
        assert [collision.slot for collision in result.legacy_collisions] == [TUE]
        # The legacy placement and the execution recorded against it keep their ids, task ids and snapshot.
        assert _rows(conn, "scheduled_tasks") == after["scheduled_tasks"]
        assert _rows(conn, "executions") == after["executions"]
    finally:
        conn.close()


def test_a_failing_v9_leaves_a_working_v8(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "app.db"
    _build_v8_database(db_path)
    failing = (*db_module._V9_STATEMENTS[:-1], "CREATE INDEX broken ON no_such_table(x)")
    monkeypatch.setattr(db_module, "MIGRATIONS", (*db_module.MIGRATIONS[:8], (9, failing)))
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    with pytest.raises(MigrationError):
        initialize_schema(raw)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 8
    columns = {row[1] for row in raw.execute("PRAGMA table_info(tasks)")}
    assert "series_id" not in columns and "recurrence_start_date" not in columns
    raw.close()
    monkeypatch.undo()
    conn = get_connection(db_path)  # the real v9 then applies cleanly
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        conn.close()
