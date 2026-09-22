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
    - A migration is either a plain tuple of SQL statements (run inside one
      `with connection:` transaction, then PRAGMA user_version is bumped),
      or a callable taking the connection, for migrations that need finer
      control than "run these statements and commit" -- see
      _migrate_v1_to_v2 below for why version 2 needs one.

Version 2 (Task 2 / Schedule Maxing v2 identity migration): adds `cancelled`
to the status CHECK constraint, relaxes planned_date/planned_start/
planned_end to nullable (a canonical-only execution has no legacy day-index
snapshot), and adds the canonical identity/timestamp columns plus a partial
unique index on scheduled_task_id. SQLite cannot ALTER a CHECK constraint or
a column's NOT NULL-ness in place, so this rebuilds the `executions` table
using SQLite's own documented "other kinds of table schema changes"
procedure (https://www.sqlite.org/lang_altertable.html): disable foreign
keys, do the whole rebuild inside one explicit transaction, verify with
`PRAGMA foreign_key_check` before committing, then re-enable foreign keys.
`work_sessions` and its rows are never touched -- SQLite resolves a foreign
key's parent table by name at query time, so
`work_sessions.execution_id REFERENCES executions(id)` continues to point at
the rebuilt table transparently once it is renamed back to `executions`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from config import settings

# Each migration is (version, statements) for the simple case, or
# (version, callable) when the migration needs its own transaction/PRAGMA
# handling (see _migrate_v1_to_v2). Versions must be applied in ascending
# order starting from 1; PRAGMA user_version records how many have been
# applied so far.
Migration = tuple[int, "tuple[str, ...] | Callable[[sqlite3.Connection], None]"]

_EXECUTIONS_V2_COLUMNS = (
    "id", "task_name", "category", "tag",
    "planned_date", "planned_start", "planned_end", "planned_duration", "priority",
    "status", "created_at", "updated_at",
    "actual_active_duration_minutes", "duration_variance_minutes", "start_delay_minutes",
    "focus_rating", "energy_rating", "interruption_count", "note",
    "task_id", "scheduled_task_id", "user_id",
    "canonical_planned_date", "canonical_timezone", "canonical_planned_start", "canonical_planned_end",
    "actual_first_start_at", "actual_final_end_at",
    "version",
)

_LEGACY_EXECUTIONS_COLUMNS = (
    "id", "task_name", "category", "tag",
    "planned_date", "planned_start", "planned_end", "planned_duration", "priority",
    "status", "created_at", "updated_at",
    "actual_active_duration_minutes", "duration_variance_minutes", "start_delay_minutes",
    "focus_rating", "energy_rating", "interruption_count", "note",
)


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    fk_was_on = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        try:
            connection.execute(
                """
                CREATE TABLE executions_v2 (
                    id TEXT PRIMARY KEY,
                    task_name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    planned_date INTEGER,
                    planned_start INTEGER,
                    planned_end INTEGER,
                    planned_duration INTEGER NOT NULL,
                    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'scheduled', 'in_progress', 'paused', 'completed', 'skipped', 'cancelled'
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
                    note TEXT,
                    task_id TEXT,
                    scheduled_task_id TEXT,
                    user_id TEXT,
                    canonical_planned_date TEXT,
                    canonical_timezone TEXT,
                    canonical_planned_start TEXT,
                    canonical_planned_end TEXT,
                    actual_first_start_at TEXT,
                    actual_final_end_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            legacy_columns = ", ".join(_LEGACY_EXECUTIONS_COLUMNS)
            connection.execute(
                f"INSERT INTO executions_v2 ({legacy_columns}) SELECT {legacy_columns} FROM executions"
            )
            connection.execute("DROP TABLE executions")
            connection.execute("ALTER TABLE executions_v2 RENAME TO executions")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_scheduled_task_id "
                "ON executions(scheduled_task_id) WHERE scheduled_task_id IS NOT NULL"
            )

            problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise sqlite3.IntegrityError(
                    f"foreign_key_check failed migrating executions to schema v2: {problems}"
                )

            connection.execute("PRAGMA user_version = 2")
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
    finally:
        if fk_was_on:
            connection.execute("PRAGMA foreign_keys = ON")


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
    (2, _migrate_v1_to_v2),
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

    for version, migration in MIGRATIONS:
        if version <= current_version:
            continue

        if callable(migration):
            # This migration manages its own transaction/PRAGMA handling
            # (and its own PRAGMA user_version write) -- see e.g.
            # _migrate_v1_to_v2 above.
            migration(connection)
            continue

        with connection:
            for statement in migration:
                connection.execute(statement)
            # SQLite does not support bind parameters inside PRAGMA statements.
            # `version` is an int literal from the hardcoded MIGRATIONS tuple
            # above, never external input, so this is not a SQL-injection risk.
            connection.execute(f"PRAGMA user_version = {version}")
