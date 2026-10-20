"""Schema v17 in app/execution/db.py (app/budget): a populated v16 database upgrades with every existing row
intact and nothing marked for synchronization; the new budget tables enforce their invariants, writes to them
are captured for synchronization, and a record created while an account is active belongs to it."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.budget.repository import BudgetRepository
from app.budget.service import BudgetService
from app.execution.db import LATEST_SCHEMA_VERSION, get_connection, initialize_schema

PROJECT = "11111111-1111-4111-8111-111111111111"
TASK = "22222222-2222-4222-8222-222222222222"
ALICE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
STAMP = "2026-02-01T10:00:00+00:00"


def _snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = ("projects", "tasks", "sync_dirty", "sync_accounts", "executions")
    return {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")] for table in tables}


def _build_v16_database(db_path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    initialize_schema(conn, target_version=16)
    conn.execute("INSERT INTO projects (id, name, created_at, updated_at, version) VALUES (?, 'Thesis', ?, ?, 2)",
                 (PROJECT, STAMP, STAMP))
    conn.execute(
        "INSERT INTO tasks (id, project_id, name, category, estimated_duration_minutes, priority, required, "
        "created_at, updated_at, version) VALUES (?, ?, 'Write', 'study', 30, 5, 0, ?, ?, 1)",
        (TASK, PROJECT, STAMP, STAMP))
    snapshot = _snapshot(conn)
    conn.close()
    return snapshot


def test_a_populated_v16_database_upgrades_without_touching_existing_data(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    before = _build_v16_database(db_path)
    assert LATEST_SCHEMA_VERSION >= 17
    conn = get_connection(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert _snapshot(conn) == before  # existing rows unchanged, nothing marked for synchronization
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"budget_balances", "budget_entries", "budget_overrides"} <= tables
    finally:
        conn.close()
    reopened = get_connection(db_path)  # an up-to-date database opens as a no-op
    try:
        assert _snapshot(reopened) == before
    finally:
        reopened.close()


def test_budget_tables_enforce_their_invariants(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "app.db")
    insert = ("INSERT INTO budget_entries (id, kind, name, amount_minor, category, start_date, recurrence, end_date, "
              "created_at, updated_at, version) VALUES (?, ?, 'X', ?, 'Other', '2026-01-01', ?, ?, ?, ?, 1)")
    try:
        for kind, amount, recurrence, end in (("gift", 1, "none", None), ("income", 0, "none", None),
                                              ("income", 1, "yearly", None), ("income", 1, "none", "2026-02-01"),
                                              ("income", 1, "daily", "2025-12-31")):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, (f"e-{kind}-{amount}-{recurrence}-{end}", kind, amount, recurrence, end,
                                      STAMP, STAMP))
        conn.execute(insert, ("entry", "income", 1, "monthly", None, STAMP, STAMP))
        override = ("INSERT INTO budget_overrides (id, entry_id, occurrence_date, state, amount_minor, created_at, "
                    "updated_at, version, deleted_at) VALUES (?, 'entry', '2026-02-01', ?, ?, ?, ?, 1, ?)")
        with pytest.raises(sqlite3.IntegrityError):  # a skipped occurrence has no replacement values
            conn.execute(override, ("o1", "skipped", 5, STAMP, STAMP, None))
        conn.execute(override, ("o1", "modified", 5, STAMP, STAMP, None))
        with pytest.raises(sqlite3.IntegrityError):  # one live edit per occurrence
            conn.execute(override, ("o2", "skipped", None, STAMP, STAMP, None))
        conn.execute(override, ("o3", "skipped", None, STAMP, STAMP, STAMP))  # tombstones do not count
    finally:
        conn.close()


def test_budget_writes_are_captured_and_owned_by_the_active_account(tmp_path: Path) -> None:
    conn = get_connection(tmp_path / "app.db")
    try:
        service = BudgetService(BudgetRepository(conn))  # the device-wide repository: owner from the trigger
        entry = service.create_entry(kind="expense", name="Rent", amount="900", category="Housing",
                                     start_date=date(2026, 1, 1), recurrence="monthly")
        service.set_balance("10", date(2026, 1, 1))
        dirty = {tuple(row) for row in conn.execute("SELECT entity_type, entity_id FROM sync_dirty")}
        assert ("budget_entry", str(entry.id)) in dirty and len(dirty) == 2
        assert conn.execute("SELECT user_id FROM budget_entries").fetchone()[0] is None
        conn.execute("INSERT INTO sync_accounts (account_key, backend_url, user_id, active, created_at) "
                     "VALUES ('k', 'https://example.test', ?, 1, ?)", (ALICE, STAMP))
        later = service.create_entry(kind="income", name="Pay", amount="5", category="Work 3",
                                     start_date=date(2026, 1, 1))
        assert conn.execute("SELECT user_id FROM budget_entries WHERE id = ?", (str(later.id),)).fetchone()[0] == ALICE
    finally:
        conn.close()
