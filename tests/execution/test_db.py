"""Tests for app/execution/db.py: schema creation, idempotent migrations,
foreign-key enforcement, and persistence across independent connections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.execution.db import get_connection, initialize_schema


def _table_names(conn: sqlite3.Connection) -> set[str]:
    # sqlite_sequence is an internal bookkeeping table SQLite creates itself
    # for AUTOINCREMENT columns; it is not part of our schema.
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    ).fetchall()
    return {row[0] for row in rows}


def test_get_connection_creates_expected_tables(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        tables = _table_names(conn)
        assert "executions" in tables
        assert "work_sessions" in tables
    finally:
        conn.close()


def test_get_connection_creates_parent_directory(tmp_path: Path) -> None:
    nested_path = tmp_path / "nested" / "dir" / "executions.db"
    assert not nested_path.parent.exists()

    conn = get_connection(nested_path)
    try:
        assert nested_path.parent.exists()
        assert nested_path.exists()
    finally:
        conn.close()


def test_initialize_schema_is_idempotent(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        version_before = conn.execute("PRAGMA user_version").fetchone()[0]

        # Calling this again must not raise and must not change the schema version.
        initialize_schema(conn)
        initialize_schema(conn)

        version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version_before == version_after
        assert version_after >= 1
        assert _table_names(conn) == {"executions", "work_sessions"}
    finally:
        conn.close()


def test_initialize_schema_idempotent_across_reopen(db_path: Path) -> None:
    conn1 = get_connection(db_path)
    conn1.close()

    # Reopening an already-migrated database must not fail or duplicate anything.
    conn2 = get_connection(db_path)
    try:
        assert _table_names(conn2) == {"executions", "work_sessions"}
    finally:
        conn2.close()


def test_foreign_keys_are_enforced(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_sessions (execution_id, started_at) VALUES (?, ?)",
                ("does-not-exist", "2024-01-01T00:00:00+00:00"),
            )
    finally:
        conn.close()


def test_data_persists_across_independent_connections(db_path: Path) -> None:
    conn_a = get_connection(db_path)
    conn_a.execute(
        "INSERT INTO executions ("
        "id, task_name, category, tag, planned_date, planned_start, planned_end, "
        "planned_duration, priority, status, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "exec-1",
            "Study Math",
            "study",
            "math",
            1,
            540,
            660,
            120,
            8,
            "scheduled",
            "2024-01-01T09:00:00+00:00",
            "2024-01-01T09:00:00+00:00",
        ),
    )
    conn_a.commit()
    conn_a.close()

    conn_b = get_connection(db_path)
    try:
        row = conn_b.execute("SELECT * FROM executions WHERE id = ?", ("exec-1",)).fetchone()
        assert row is not None
        assert row["task_name"] == "Study Math"
        assert row["status"] == "scheduled"
    finally:
        conn_b.close()
