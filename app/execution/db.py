"""
db.py

SQLite connection management and idempotent schema migrations for the
execution-tracking subsystem.

This module owns the only two things that talk directly to sqlite3 for
connection setup: where the database file lives, and how its schema is
created/upgraded. Everything else (queries, business logic) lives in
repository.py and service.py.

Design notes:
    - The database is a single local file (see config.settings.DATA_DIR),
      never a server or cloud database.
    - Schema changes are expressed as an ordered list of migrations, tracked
      via SQLite's built-in `PRAGMA user_version`. Calling initialize_schema
      on an already-up-to-date connection is a guaranteed no-op: pending
      migrations are skipped based on the recorded version, and every DDL
      statement is additionally written with IF NOT EXISTS as a second,
      independent safety net.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from config import settings

# Each migration is (version, statements). Versions must be applied in
# ascending order starting from 1; PRAGMA user_version records how many have
# been applied so far.
Migration = tuple[int, tuple[str, ...]]

MIGRATIONS: tuple[Migration, ...] = (
    (
        1,
        (
            """
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                category TEXT NOT NULL,
                tag TEXT NOT NULL,
                planned_date INTEGER NOT NULL,
                planned_start INTEGER NOT NULL,
                planned_end INTEGER NOT NULL,
                planned_duration INTEGER NOT NULL,
                priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
                status TEXT NOT NULL CHECK (
                    status IN (
                        'scheduled', 'in_progress', 'paused', 'completed', 'skipped'
                    )
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                actual_active_duration_minutes REAL,
                duration_variance_minutes REAL,
                start_delay_minutes REAL,
                focus_rating INTEGER CHECK (
                    focus_rating IS NULL OR focus_rating BETWEEN 1 AND 5
                ),
                energy_rating INTEGER CHECK (
                    energy_rating IS NULL OR energy_rating BETWEEN 1 AND 5
                ),
                interruption_count INTEGER CHECK (
                    interruption_count IS NULL OR interruption_count >= 0
                ),
                note TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS work_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_work_sessions_execution_id
                ON work_sessions(execution_id)
            """,
        ),
    ),
)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Return the database file path to use: the explicit path if given, else the configured default."""
    if db_path is not None:
        return Path(db_path)
    return Path(settings.DATA_DIR) / settings.EXECUTION_DB_FILENAME


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """
    Open (creating if necessary) the execution-tracking SQLite database.

    Ensures the parent directory exists, enables row access by column name,
    turns on foreign-key enforcement (off by default in SQLite), and applies
    any pending schema migrations before returning.
    """
    resolved_path = resolve_db_path(db_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    # The desktop UI opens this connection once on the Tk main thread but
    # reads/writes it from background worker threads (see
    # app.ui.background.run_in_background), so the default same-thread
    # affinity check must be disabled. ExecutionRepository is responsible for
    # serializing actual access so this stays safe.
    connection = sqlite3.connect(str(resolved_path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """
    Apply any migrations not yet reflected in `PRAGMA user_version`.

    Idempotent: running this repeatedly against the same connection/file
    applies nothing further once the schema is current.
    """
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]

    for version, statements in MIGRATIONS:
        if version <= current_version:
            continue

        with connection:
            for statement in statements:
                connection.execute(statement)
            # SQLite does not support bind parameters inside PRAGMA statements.
            # `version` is an int literal from the hardcoded MIGRATIONS tuple
            # above, never external input, so this is not a SQL-injection risk.
            connection.execute(f"PRAGMA user_version = {version}")
