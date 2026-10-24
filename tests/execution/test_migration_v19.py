"""Schema v19 in app/execution/db.py (tasks have no priority and no dependencies): a v18 database upgrades
with every task and execution row intact apart from the dropped priority column, its dependency edges
discarded, and nothing marked for synchronization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.execution.db import get_connection, initialize_schema

STAMP = "2026-02-01T10:00:00+00:00"


def _rows(conn, table: str) -> list[dict]:
    cursor = conn.execute(f"SELECT * FROM {table} ORDER BY 1")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_v18_database_drops_priority_and_dependencies_and_keeps_every_row(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    initialize_schema(conn, target_version=18)
    task = ("INSERT INTO tasks (id, name, category, estimated_duration_minutes, priority, required, points, "
            "created_at, updated_at, version) VALUES (?, ?, 'study', 30, ?, 0, ?, ?, ?, 1)")
    conn.execute(task, ("a", "Draft", 9, 4, STAMP, STAMP))
    conn.execute(task, ("b", "Edit", 2, 7, STAMP, STAMP))
    conn.execute("INSERT INTO task_dependencies (task_id, position, depends_on_task_id) VALUES ('b', 0, 'a')")
    conn.execute("INSERT INTO executions (id, task_id, task_name, category, tag, planned_duration, priority, status, "
                 "created_at, updated_at) VALUES ('x', 'a', 'Draft', 'study', '', 30, 9, 'completed', ?, ?)",
                 (STAMP, STAMP))
    conn.execute("DELETE FROM sync_dirty")
    before = {table: [{k: v for k, v in row.items() if k != "priority"} for row in _rows(conn, table)]
              for table in ("tasks", "executions")}
    conn.close()

    upgraded = get_connection(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] >= 19
        assert "priority" not in _columns(upgraded, "tasks") | _columns(upgraded, "executions")
        assert not upgraded.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_dependencies'").fetchone()
        assert {table: _rows(upgraded, table) for table in ("tasks", "executions")} == before
        assert [row["points"] for row in _rows(upgraded, "tasks")] == [4, 7]
        assert upgraded.execute("SELECT COUNT(*) FROM sync_dirty").fetchone()[0] == 0
    finally:
        upgraded.close()
