"""Schema v8 in app/execution/db.py: task points and each execution's snapshot of them. A populated
v7 database upgrades with every existing value intact, tasks get the default points (1), existing
executions an unknown snapshot (NULL -- never back-filled), nothing is marked for synchronization,
and the new CHECKs hold."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.execution.db import LATEST_SCHEMA_VERSION, get_connection, initialize_schema
from tests.execution.test_migration_v7 import TABLES, _build_v6_database, _rows


def test_a_populated_v7_database_upgrades_without_touching_existing_values(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    _build_v6_database(db_path)
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    initialize_schema(raw, target_version=7)
    before = {table: _rows(raw, table) for table in TABLES}
    raw.close()
    assert before["tasks"] and before["executions"]

    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION == 8
        after = {table: _rows(conn, table) for table in TABLES}
        for table in TABLES:
            old_columns = before[table][0].keys() if before[table] else ()
            assert [{column: row[column] for column in old_columns} for row in after[table]] == before[table], table
        assert {row["points"] for row in after["tasks"]} == {1}  # the default every client used
        assert {row["points"] for row in after["executions"]} == {None}  # unknown, never back-filled
        assert after["sync_dirty"] == before["sync_dirty"]  # the migration itself captured nothing

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET points = -1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET points = 1001")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE executions SET points = -3")
    finally:
        conn.close()
