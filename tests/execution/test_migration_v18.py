"""Schema v18 in app/execution/db.py (Budgeting > Future): a v17 database with budget records upgrades with
every row intact and nothing marked for synchronization; income ideas and wishlist items are stored, enforce
their invariants, and are captured for synchronization like every budget record."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.execution.db import get_connection, initialize_schema

STAMP = "2026-02-01T10:00:00+00:00"
TABLES = ("budget_balances", "budget_entries", "budget_overrides", "sync_dirty")


def _rows(conn, table: str) -> list[tuple]:
    return [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]


def test_a_v17_database_with_budget_records_upgrades_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    initialize_schema(conn, target_version=17)
    conn.execute("INSERT INTO budget_balances (id, amount_minor, as_of, created_at, updated_at, version) "
                 "VALUES ('b', -250, '2026-01-31', ?, ?, 1)", (STAMP, STAMP))
    conn.execute("INSERT INTO budget_entries (id, kind, name, amount_minor, category, start_date, recurrence, "
                 "created_at, updated_at, version) VALUES ('e', 'expense', 'Rent', 120000, 'Housing', '2026-01-01', "
                 "'monthly', ?, ?, 1)", (STAMP, STAMP))
    conn.execute("INSERT INTO budget_overrides (id, entry_id, occurrence_date, state, created_at, updated_at, "
                 "version) VALUES ('o', 'e', '2026-02-01', 'skipped', ?, ?, 1)", (STAMP, STAMP))
    conn.execute("DELETE FROM sync_dirty")
    before = {table: _rows(conn, table) for table in TABLES}
    conn.close()

    upgraded = get_connection(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] >= 18
        assert {table: _rows(upgraded, table) for table in TABLES} == before
        upgraded.execute("INSERT INTO budget_ideas (id, name, expected_amount_minor, created_at, updated_at, version) "
                         "VALUES ('i', 'Side project', 50000, ?, ?, 1)", (STAMP, STAMP))
        assert _rows(upgraded, "budget_ideas")[0][4] == 0  # nothing secured yet
        wish = ("INSERT INTO budget_wishes (id, name, amount_minor, category, converted_entry_id, converted_on, "
                "created_at, updated_at, version) VALUES (?, 'Desk', 30000, 'Shopping', ?, ?, ?, ?, 1)")
        with pytest.raises(sqlite3.IntegrityError):  # a conversion records the expense and its date together
            upgraded.execute(wish, ("w1", "e", None, STAMP, STAMP))
        upgraded.execute(wish, ("w1", None, None, STAMP, STAMP))
        dirty = {tuple(row) for row in upgraded.execute("SELECT entity_type, entity_id FROM sync_dirty")}
        assert dirty == {("budget_idea", "i"), ("budget_wish", "w1")}
    finally:
        upgraded.close()
