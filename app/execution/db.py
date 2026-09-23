"""
db.py

SQLite connection management, transaction ownership, and ordered schema
migrations for the application's single local database: execution history
(since v1) and persisted canonical planning data (since v3).

This module owns everything that talks directly to sqlite3 for connection
setup: where the database file lives, how its schema is created/upgraded,
and how a unit of work is made atomic. Queries live in the repositories
(app/execution/repository.py, app/planning/repository.py); business rules
live in the services.

Design notes:
    - The database is a single local file (see config.settings.DATA_DIR),
      never a server or cloud database. By default it lives in the per-user
      application-data directory (config.settings.default_user_data_dir),
      independent of the current working directory and of where the
      checkout lives. SCHEDULE_MAXING_DATA_DIR still overrides the
      directory, and every opener accepts an explicit db_path.
    - Schema changes are expressed as an ordered list of migrations, tracked
      via SQLite's built-in `PRAGMA user_version`. Calling initialize_schema
      on an already-up-to-date connection is a guaranteed no-op: pending
      migrations are skipped based on the recorded version, and every DDL
      statement is additionally written with IF NOT EXISTS as a second,
      independent safety net. A database whose user_version is *newer*
      than this code knows about is refused (MigrationError) rather than
      opened and possibly mis-written.
    - A migration is either a plain tuple of SQL statements (run inside one
      explicit transaction together with its PRAGMA user_version bump, so a
      failure part-way leaves the previous version fully intact), or a
      callable taking the connection, for migrations that need finer
      control than "run these statements and commit" -- see
      _migrate_v1_to_v2 below for why version 2 needs one. After each
      migration, `PRAGMA foreign_key_check` must be clean before it commits;
      after all pending migrations, `PRAGMA quick_check` must report "ok".

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

Version 3 (Milestone 2 / planning persistence): adds relational tables for
the canonical planning entities (app/planning/models.py) -- projects,
tasks (+ task_tags, task_preferred_dates, task_dependencies,
task_recurrence_weekdays child tables; the single preferred time window
and the recurrence scalars are plain columns), fixed_blocks, and
scheduled_tasks (placements). Only ScheduledTask.optimization_metadata,
which is genuinely arbitrary, is stored as a small JSON text column; no
whole-model blobs. Existing `executions`/`work_sessions` rows, ids, and
timestamps are not rewritten.

Version 4 (Milestone 3 / sync-ready local records, see
docs/sync-contract.md and _migrate_v3_to_v4): every synchronizable table
(projects, tasks, fixed_blocks, scheduled_tasks, executions, and the new
preference_overrides and schedule_generations) carries an owner (user_id,
NULL for a local ownerless record), UTC audit timestamps, an integer local
revision (`version`), and a `deleted_at` tombstone; fixed_blocks gains
`category`; execution_wire_ids maps non-UUID execution ids to durable wire
ids. Deletes of synchronizable planning records are soft (tombstones) from
v4 on, so every normal read filters `deleted_at IS NULL`. The execution
link-insert trigger is recreated to reject links to tombstoned
tasks/placements. Additive only: released v1-v3 migrations are unchanged.

Version 5 (Milestone 3 / synchronization client, see app/sync and
docs/sync-protocol.md): change capture triggers that record every local
write to a synchronizable row in sync_dirty in the same transaction (and
stamp records created while an account is active with its owner);
sync_control (suppresses capture while pulled records are applied);
per-account sync_accounts (cursor), sync_shadows (last acknowledged server
state), sync_outbox (materialized operations with stable op ids) and
sync_conflicts. The execution link trigger is recreated once more so pulled
history is validated by the server, not re-checked locally.

Execution <-> planning links (the legacy compatibility strategy):
    executions.task_id / scheduled_task_id are *historical identity*: they
    record which task/placement an execution was created for, alongside the
    execution's own snapshot (name, category, planned date/instants). They
    are deliberately not SQL foreign keys, because
      (a) rows written before v3 may carry ids whose parent Task/placement
          was never persisted anywhere (the canonical API used to accept
          in-memory models) -- those ids are preserved exactly as they are;
          no placeholder task is fabricated and no date is guessed, and
      (b) deleting/replacing a task or placement must never cascade into,
          null out, or block on execution history.
    Relationships are instead enforced at *link time* by triggers created
    in v3 (the insert trigger recreated in v4 to ignore tombstones): a newly
    inserted execution that names a task_id must reference a live persisted
    task; one that names a scheduled_task_id must also name a
    task_id, reference a persisted placement, and that placement must
    belong to the same task. Once written, an execution's task_id/
    scheduled_task_id are immutable (a status/session/feedback update never
    changes them, so pre-v3 orphan rows keep working through the whole
    execution lifecycle). A placement id that execution history already
    references can only ever be (re)stored for that same task. Because the
    insert trigger only fires on INSERT, pre-existing rows are exempt by
    construction -- no per-row "legacy" flag is needed.

Transactions and threads:
    get_connection returns an AppConnection in autocommit mode
    (isolation_level=None), so sqlite3 never opens or commits a transaction
    implicitly -- `transaction()` below is the only thing that does. The
    outermost `transaction()` owns BEGIN IMMEDIATE/COMMIT/ROLLBACK; a nested
    `transaction()` (e.g. a repository method called from inside a service
    operation) becomes a SAVEPOINT, so it can never commit the enclosing
    unit of work early, and its own failure rolls back only its part if the
    caller chooses to handle the error. Each AppConnection carries one
    re-entrant lock shared by every repository using it; a transaction
    holds it from BEGIN to COMMIT and reads hold it per query, so work from
    a background thread can never interleave statements into another
    thread's open transaction.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Errors
# -----------------------------------------------------------------------------


class StorageError(Exception):
    """Base class for database infrastructure errors (not domain rule violations)."""


class MigrationError(StorageError):
    """A schema migration could not be applied; the database is left at its previous version."""


class IntegrityCheckError(StorageError):
    """PRAGMA foreign_key_check / quick_check reported a problem after migrating."""


class LegacyAdoptionError(StorageError):
    """A repository-local legacy database exists but could not be safely copied."""


# -----------------------------------------------------------------------------
# Connections and transactions
# -----------------------------------------------------------------------------


class TransactionState:
    """Per-connection lock plus SAVEPOINT nesting depth (see module docstring)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.savepoint_depth = 0


class AppConnection(sqlite3.Connection):
    """sqlite3.Connection carrying the TransactionState shared by every repository that uses it."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.transaction_state = TransactionState()


def transaction_state_for(connection: sqlite3.Connection) -> TransactionState | None:
    """The connection's shared TransactionState, or None for a plain sqlite3.Connection."""
    return getattr(connection, "transaction_state", None)


def _resolve_state(connection: sqlite3.Connection, state: TransactionState | None) -> TransactionState:
    resolved = state or transaction_state_for(connection)
    if resolved is None:
        raise TypeError(
            "transaction()/locked() need an AppConnection (from get_connection) or an explicit TransactionState."
        )
    return resolved


@contextmanager
def locked(connection: sqlite3.Connection, state: TransactionState | None = None) -> Iterator[sqlite3.Connection]:
    """Hold the connection's lock for a read (re-entrant inside an open transaction on the same thread)."""
    with _resolve_state(connection, state).lock:
        yield connection


@contextmanager
def transaction(connection: sqlite3.Connection, state: TransactionState | None = None) -> Iterator[sqlite3.Connection]:
    """
    Run the enclosed block as one atomic unit of work.

    Outermost call: BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block
    (or the COMMIT itself, e.g. a deferred foreign-key violation) raises.
    Nested call: SAVEPOINT ... RELEASE, or ROLLBACK TO that savepoint on
    error -- never a COMMIT, so only the outermost owner ends the
    transaction.
    """
    state = _resolve_state(connection, state)
    with state.lock:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return

        state.savepoint_depth += 1
        # The name is built from an internal counter, never from caller input.
        name = f"app_savepoint_{state.savepoint_depth}"
        try:
            connection.execute(f"SAVEPOINT {name}")
            try:
                yield connection
            except BaseException:
                connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
                connection.execute(f"RELEASE SAVEPOINT {name}")
                raise
            connection.execute(f"RELEASE SAVEPOINT {name}")
        finally:
            state.savepoint_depth -= 1


# -----------------------------------------------------------------------------
# Migrations
# -----------------------------------------------------------------------------

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


#: Prefix of every RAISE(ABORT, ...) message from the v3 link triggers, so
#: repositories can translate them into domain errors.
EXECUTION_LINK_VIOLATION = "execution link violation"

_V3_PLANNING_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        name TEXT NOT NULL CHECK (length(name) > 0),
        description TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0)
    )
    """,
    # Timestamps/dates are ISO 8601 text. Instants keep their original UTC
    # offset (exact round trip) and, where range/order queries need it, a
    # normalized fixed-width UTC twin column (*_utc) that sorts correctly as
    # text. Foreign keys between planning entities are DEFERRABLE INITIALLY
    # DEFERRED so a bulk write may insert rows in any order within one
    # transaction; they are verified at COMMIT.
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        project_id TEXT REFERENCES projects(id) DEFERRABLE INITIALLY DEFERRED,
        name TEXT NOT NULL CHECK (length(name) > 0),
        category TEXT NOT NULL CHECK (length(category) > 0),
        estimated_duration_minutes INTEGER NOT NULL CHECK (estimated_duration_minutes > 0),
        priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 10),
        required INTEGER NOT NULL CHECK (required IN (0, 1)),
        required_date TEXT,
        preferred_window_start_minute INTEGER CHECK (
            preferred_window_start_minute IS NULL OR preferred_window_start_minute BETWEEN 0 AND 1439
        ),
        preferred_window_end_minute INTEGER CHECK (
            preferred_window_end_minute IS NULL OR preferred_window_end_minute BETWEEN 1 AND 1440
        ),
        deadline TEXT,
        deadline_utc TEXT,
        recurrence_frequency TEXT CHECK (
            recurrence_frequency IS NULL OR recurrence_frequency IN ('daily', 'weekly', 'monthly')
        ),
        recurrence_interval INTEGER CHECK (recurrence_interval IS NULL OR recurrence_interval > 0),
        recurrence_day_of_month INTEGER CHECK (
            recurrence_day_of_month IS NULL OR recurrence_day_of_month BETWEEN 1 AND 31
        ),
        recurrence_end_date TEXT,
        recurrence_count INTEGER CHECK (recurrence_count IS NULL OR recurrence_count > 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        CHECK ((preferred_window_start_minute IS NULL) = (preferred_window_end_minute IS NULL)),
        CHECK (
            preferred_window_start_minute IS NULL
            OR preferred_window_end_minute > preferred_window_start_minute
        ),
        CHECK ((deadline IS NULL) = (deadline_utc IS NULL)),
        CHECK ((recurrence_frequency IS NULL) = (recurrence_interval IS NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_required_date ON tasks(required_date)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_deadline_utc ON tasks(deadline_utc)",
    """
    CREATE TABLE IF NOT EXISTS task_tags (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position >= 0),
        tag TEXT NOT NULL,
        PRIMARY KEY (task_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag)",
    """
    CREATE TABLE IF NOT EXISTS task_preferred_dates (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position >= 0),
        preferred_date TEXT NOT NULL,
        PRIMARY KEY (task_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_preferred_dates_date ON task_preferred_dates(preferred_date)",
    # depends_on_task_id deliberately has no ON DELETE action: deleting a
    # task that other tasks still depend on fails (at COMMIT at the latest)
    # instead of silently dropping the edge and unblocking its dependents.
    # The planning service checks first and raises a domain error.
    """
    CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK (position >= 0),
        depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) DEFERRABLE INITIALLY DEFERRED,
        PRIMARY KEY (task_id, position),
        CHECK (task_id <> depends_on_task_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_task_dependencies_depends_on ON task_dependencies(depends_on_task_id)",
    """
    CREATE TABLE IF NOT EXISTS task_recurrence_weekdays (
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
        weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
        PRIMARY KEY (task_id, weekday)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fixed_blocks (
        id TEXT PRIMARY KEY,
        label TEXT NOT NULL CHECK (length(label) > 0),
        planned_date TEXT NOT NULL,
        timezone TEXT NOT NULL,
        planned_start TEXT NOT NULL,
        planned_end TEXT NOT NULL,
        planned_start_utc TEXT NOT NULL,
        planned_end_utc TEXT NOT NULL,
        CHECK (planned_end_utc > planned_start_utc)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fixed_blocks_planned_date ON fixed_blocks(planned_date, planned_start_utc)",
    # Placements are derived planning output: deleting their task deletes
    # them. Execution history that referenced a deleted placement is
    # untouched (see "Execution <-> planning links" in the module docstring).
    """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
        planned_date TEXT NOT NULL,
        timezone TEXT NOT NULL,
        planned_start TEXT NOT NULL,
        planned_end TEXT NOT NULL,
        planned_start_utc TEXT NOT NULL,
        planned_end_utc TEXT NOT NULL,
        score REAL NOT NULL,
        optimization_metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        CHECK (planned_end_utc > planned_start_utc)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_planned_date ON scheduled_tasks(planned_date, planned_start_utc)",
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_task_id ON scheduled_tasks(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_executions_task_id ON executions(task_id) WHERE task_id IS NOT NULL",
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_executions_link_insert
    BEFORE INSERT ON executions
    FOR EACH ROW
    WHEN NEW.task_id IS NOT NULL OR NEW.scheduled_task_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id requires task_id')
        WHERE NEW.task_id IS NULL;
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: task_id does not reference a persisted task')
        WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id does not reference a persisted placement')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: placement belongs to a different task')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id AND task_id <> NEW.task_id
          );
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_executions_link_immutable
    BEFORE UPDATE OF task_id, scheduled_task_id ON executions
    FOR EACH ROW
    WHEN NEW.task_id IS NOT OLD.task_id OR NEW.scheduled_task_id IS NOT OLD.scheduled_task_id
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: task_id/scheduled_task_id are immutable history');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_scheduled_tasks_history_insert
    BEFORE INSERT ON scheduled_tasks
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM executions WHERE scheduled_task_id = NEW.id AND task_id IS NOT NEW.task_id
    )
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: placement id is recorded in history for a different task');
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS trg_scheduled_tasks_history_update
    BEFORE UPDATE OF task_id ON scheduled_tasks
    FOR EACH ROW
    WHEN NEW.task_id IS NOT OLD.task_id
      AND EXISTS (SELECT 1 FROM executions WHERE scheduled_task_id = NEW.id)
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: a history-linked placement cannot change task');
    END
    """,
)


#: Columns added to existing tables by v4, as (table, column, definition).
#: Every definition either allows NULL or has a constant default, as SQLite's
#: ALTER TABLE ADD COLUMN requires; the fixed-block audit timestamps are then
#: filled in by _migrate_v3_to_v4 itself.
_V4_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("projects", "deleted_at", "TEXT"),
    ("tasks", "deleted_at", "TEXT"),
    ("fixed_blocks", "user_id", "TEXT"),
    ("fixed_blocks", "category", "TEXT NOT NULL DEFAULT 'fixed' CHECK (length(category) > 0)"),
    ("fixed_blocks", "created_at", "TEXT"),
    ("fixed_blocks", "updated_at", "TEXT"),
    ("fixed_blocks", "version", "INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)"),
    ("fixed_blocks", "deleted_at", "TEXT"),
    ("scheduled_tasks", "user_id", "TEXT"),
    ("scheduled_tasks", "deleted_at", "TEXT"),
    ("executions", "deleted_at", "TEXT"),
)

_V4_STATEMENTS: tuple[str, ...] = (
    # Persisted preference layers (the "user" layer and per-date layers of
    # app.planning.preferences.resolve_day_preferences). optimizer_mode is a
    # real column; the remaining PreferenceOverrides fields are one
    # pydantic-validated JSON document, because their absent/value/None
    # three-state semantics are exactly what that document encodes.
    """
    CREATE TABLE IF NOT EXISTS preference_overrides (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        scope TEXT NOT NULL CHECK (scope IN ('user', 'date')),
        scope_date TEXT,
        optimizer_mode TEXT CHECK (optimizer_mode IS NULL OR optimizer_mode IN ('precise_greedy', 'adhd_friendly')),
        overrides TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        deleted_at TEXT,
        CHECK ((scope = 'date') = (scope_date IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_overrides_live_scope "
    "ON preference_overrides(COALESCE(user_id, ''), scope, COALESCE(scope_date, '')) WHERE deleted_at IS NULL",
    # Provenance of the saved schedule of one date: which inputs (by
    # fingerprint, see app/planning/provenance.py) and which placements (by
    # digest) it was generated from. One live row per (owner, date).
    """
    CREATE TABLE IF NOT EXISTS schedule_generations (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        planned_date TEXT NOT NULL,
        timezone TEXT NOT NULL,
        engine_mode TEXT NOT NULL,
        range_start TEXT NOT NULL,
        range_end TEXT NOT NULL,
        range_scope TEXT NOT NULL,
        allocation_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        fingerprint_version INTEGER NOT NULL,
        placements_digest TEXT NOT NULL,
        placement_count INTEGER NOT NULL CHECK (placement_count >= 0),
        unscheduled_count INTEGER NOT NULL CHECK (unscheduled_count >= 0),
        total_score REAL NOT NULL,
        generated_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        deleted_at TEXT,
        CHECK (range_start <= planned_date AND planned_date <= range_end)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_schedule_generations_live_date "
    "ON schedule_generations(COALESCE(user_id, ''), planned_date) WHERE deleted_at IS NULL",
    # A stable wire identity for executions whose local id is not a UUID
    # (legacy/fixture ids are preserved exactly, never reminted -- see
    # docs/sync-contract.md). UUID-shaped ids are their own wire id.
    """
    CREATE TABLE IF NOT EXISTS execution_wire_ids (
        execution_id TEXT PRIMARY KEY REFERENCES executions(id) ON DELETE CASCADE,
        wire_id TEXT NOT NULL UNIQUE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_task_live ON scheduled_tasks(task_id) WHERE deleted_at IS NULL",
    # Soft deletion: a new execution may only link to a *live* task and
    # placement. The v3 trigger is replaced (by name) rather than edited in
    # the released v3 migration.
    "DROP TRIGGER IF EXISTS trg_executions_link_insert",
    f"""
    CREATE TRIGGER trg_executions_link_insert
    BEFORE INSERT ON executions
    FOR EACH ROW
    WHEN NEW.task_id IS NOT NULL OR NEW.scheduled_task_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id requires task_id')
        WHERE NEW.task_id IS NULL;
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: task_id does not reference a persisted task')
        WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id AND deleted_at IS NULL);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id does not reference a persisted placement')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id AND deleted_at IS NULL);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: placement belongs to a different task')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id AND task_id <> NEW.task_id
          );
    END
    """,
)


def _is_uuid_text(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
    """
    Version 4 (Milestone 3 / sync-ready local records): uniform ownership,
    audit timestamps, record versions and soft-deletion (deleted_at)
    metadata on every synchronizable table; FixedBlock.category; persisted
    preference layers; persisted schedule provenance; a durable wire id for
    non-UUID execution ids; and a link trigger that ignores tombstones.

    Purely additive: no existing id, timestamp, snapshot, work session, or
    version is rewritten. Existing fixed blocks receive the migration instant
    as their (previously unrecorded) created_at/updated_at and version 1.

    A callable (not a statement tuple) because ALTER TABLE ADD COLUMN has no
    IF NOT EXISTS (so columns are added only when missing, keeping repeated
    runs safe) and because wire ids are minted with Python's uuid4. Like the
    statement migrations it is one transaction together with its
    user_version bump and foreign_key_check: any failure leaves v3 intact.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table, column, definition in _V4_ADDED_COLUMNS:
            if column not in _column_names(connection, table):
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE fixed_blocks SET created_at = COALESCE(created_at, ?), updated_at = COALESCE(updated_at, ?) "
            "WHERE created_at IS NULL OR updated_at IS NULL",
            (now, now),
        )

        for statement in _V4_STATEMENTS:
            connection.execute(statement)

        mapped = {row[0] for row in connection.execute("SELECT execution_id FROM execution_wire_ids").fetchall()}
        for (execution_id,) in [tuple(row) for row in connection.execute("SELECT id FROM executions").fetchall()]:
            if not _is_uuid_text(execution_id) and execution_id not in mapped:
                connection.execute(
                    "INSERT INTO execution_wire_ids (execution_id, wire_id) VALUES (?, ?)",
                    (execution_id, str(uuid.uuid4())),
                )

        _check_foreign_keys(connection, 4)
        connection.execute("PRAGMA user_version = 4")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


#: (entity type on the wire, local table) for every synchronizable table.
SYNC_TABLES: tuple[tuple[str, str], ...] = (
    ("project", "projects"),
    ("task", "tasks"),
    ("fixed_block", "fixed_blocks"),
    ("placement", "scheduled_tasks"),
    ("preference", "preference_overrides"),
    ("schedule_generation", "schedule_generations"),
    ("execution", "executions"),
)

#: True while synchronization applies pulled records (app/sync): change
#: capture is suppressed (no outbound echo) and link checks are left to the
#: server, which already validated the records within the user's scope.
_APPLYING_REMOTE = "(SELECT value FROM sync_control WHERE name = 'applying_remote') = 1"


def _v5_capture_triggers() -> tuple[str, ...]:
    """
    Change capture (app/sync, docs/sync-protocol.md). Every local write to a
    synchronizable row -- whatever code path makes it: a service method, CSV
    import, rescheduling cleanup, a work session, a history reset -- bumps
    that record's local revision in sync_dirty inside the same transaction,
    so a rolled-back mutation leaves no trace and a committed one can never
    be missed. Work-session writes mark their execution (the aggregate).
    """
    mark = (
        "INSERT INTO sync_dirty (entity_type, entity_id, local_rev, changed_at) "
        "VALUES ('{type}', {ref}.{column}, 1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) "
        "ON CONFLICT (entity_type, entity_id) DO UPDATE SET local_rev = local_rev + 1, changed_at = excluded.changed_at"
    )
    statements = []
    targets = [(entity, table, "id") for entity, table in SYNC_TABLES] + [("execution", "work_sessions", "execution_id")]
    for entity, table, column in targets:
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            name = f"trg_sync_{table}_{event.lower()}"
            statements.append(f"DROP TRIGGER IF EXISTS {name}")
            statements.append(
                f"CREATE TRIGGER {name} AFTER {event} ON {table} FOR EACH ROW WHEN NOT {_APPLYING_REMOTE} "
                f"BEGIN {mark.format(type=entity, ref=ref, column=column)}; END"
            )
    for _, table in SYNC_TABLES:
        # Records created while an account is *active* on this device belong to it; nothing
        # existing is reassigned (that is the explicit association step in app/sync).
        name = f"trg_sync_{table}_owner"
        statements.append(f"DROP TRIGGER IF EXISTS {name}")
        statements.append(
            f"CREATE TRIGGER {name} AFTER INSERT ON {table} FOR EACH ROW "
            f"WHEN NEW.user_id IS NULL AND NOT {_APPLYING_REMOTE} AND EXISTS (SELECT 1 FROM sync_accounts WHERE active = 1) "
            f"BEGIN UPDATE {table} SET user_id = (SELECT user_id FROM sync_accounts WHERE active = 1) "
            f"WHERE id = NEW.id; END"
        )
    return tuple(statements)


_V5_STATEMENTS: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS sync_control (name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
    "INSERT OR IGNORE INTO sync_control (name, value) VALUES ('applying_remote', 0)",
    # One row per record changed locally and not yet acknowledged by the server; local_rev
    # counts local changes (it is not a server version).
    """
    CREATE TABLE IF NOT EXISTS sync_dirty (
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        local_rev INTEGER NOT NULL CHECK (local_rev > 0),
        changed_at TEXT NOT NULL,
        PRIMARY KEY (entity_type, entity_id)
    )
    """,
    # One row per (backend, server account) this device has signed in to. Cursors, shadows,
    # outbox operations and conflicts all hang off it, so accounts never mix.
    """
    CREATE TABLE IF NOT EXISTS sync_accounts (
        account_key TEXT PRIMARY KEY,
        backend_url TEXT NOT NULL,
        user_id TEXT NOT NULL,
        email TEXT,
        pull_cursor INTEGER NOT NULL DEFAULT 0 CHECK (pull_cursor >= 0),
        active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
        associated_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (backend_url, user_id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_accounts_one_active ON sync_accounts(active) WHERE active = 1",
    # The last server state acknowledged for a record (its server version is the precondition of
    # the next local change).
    """
    CREATE TABLE IF NOT EXISTS sync_shadows (
        account_key TEXT NOT NULL REFERENCES sync_accounts(account_key) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        server_version INTEGER NOT NULL CHECK (server_version > 0),
        deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
        record TEXT NOT NULL,
        PRIMARY KEY (account_key, entity_type, entity_id)
    )
    """,
    # Materialized operations awaiting a server answer. An operation keeps its op_id until it is
    # answered, so a resend after a lost response is recognized by the server.
    """
    CREATE TABLE IF NOT EXISTS sync_outbox (
        op_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id TEXT NOT NULL UNIQUE,
        account_key TEXT NOT NULL REFERENCES sync_accounts(account_key) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        local_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('create', 'update', 'delete', 'action', 'feedback')),
        action TEXT,
        base_version INTEGER,
        payload TEXT,
        group_id TEXT,
        local_rev INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'blocked')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_outbox_entity ON sync_outbox(account_key, entity_type, entity_id)",
    """
    CREATE TABLE IF NOT EXISTS sync_conflicts (
        id TEXT PRIMARY KEY,
        account_key TEXT NOT NULL REFERENCES sync_accounts(account_key) ON DELETE CASCADE,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        local_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('push_conflict', 'push_rejected', 'pull_conflict')),
        op_id TEXT,
        base_version INTEGER,
        local_record TEXT,
        remote_record TEXT,
        error TEXT,
        status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
        resolution TEXT,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sync_conflicts_open ON sync_conflicts(account_key, status)",
    # The link trigger again (see v4), now leaving pulled history to the server's validation.
    "DROP TRIGGER IF EXISTS trg_executions_link_insert",
    f"""
    CREATE TRIGGER trg_executions_link_insert
    BEFORE INSERT ON executions
    FOR EACH ROW
    WHEN (NEW.task_id IS NOT NULL OR NEW.scheduled_task_id IS NOT NULL) AND NOT {_APPLYING_REMOTE}
    BEGIN
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id requires task_id')
        WHERE NEW.task_id IS NULL;
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: task_id does not reference a persisted task')
        WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.task_id AND deleted_at IS NULL);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: scheduled_task_id does not reference a persisted placement')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id AND deleted_at IS NULL);
        SELECT RAISE(ABORT, '{EXECUTION_LINK_VIOLATION}: placement belongs to a different task')
        WHERE NEW.scheduled_task_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM scheduled_tasks WHERE id = NEW.scheduled_task_id AND task_id <> NEW.task_id
          );
    END
    """,
    *_v5_capture_triggers(),
)


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
    (3, _V3_PLANNING_STATEMENTS),
    (4, _migrate_v3_to_v4),
    (5, _V5_STATEMENTS),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def _validate_migration_order(migrations: tuple[Migration, ...]) -> None:
    versions = [version for version, _ in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(f"migrations must be numbered 1..N without gaps, got {versions}")


def _check_foreign_keys(connection: sqlite3.Connection, version: int) -> None:
    problems = connection.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise IntegrityCheckError(
            f"foreign_key_check failed after migrating to schema v{version}: {[tuple(row) for row in problems]}"
        )


def initialize_schema(connection: sqlite3.Connection, *, target_version: int | None = None) -> None:
    """
    Apply, in order, every migration not yet reflected in `PRAGMA user_version`
    (up to `target_version`, default: the latest -- tests use a lower target
    to build genuine older-version databases).

    Idempotent: running this repeatedly against the same connection/file
    applies nothing further once the schema is current. Each migration is
    atomic: if it fails, it is rolled back, user_version stays at the last
    successfully applied version, and MigrationError is raised (chained to
    the original error). Raises MigrationError without touching anything if
    the database is newer than this code supports.
    """
    migrations = MIGRATIONS
    _validate_migration_order(migrations)
    latest = migrations[-1][0]
    target = latest if target_version is None else target_version
    if not 0 <= target <= latest:
        raise MigrationError(f"target_version must be between 0 and {latest}, got {target}")

    state = transaction_state_for(connection) or TransactionState()
    with state.lock:
        if connection.in_transaction:
            raise MigrationError("initialize_schema must not run inside an open transaction")

        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version > latest:
            raise MigrationError(
                f"database schema version {current_version} is newer than this application supports ({latest}); "
                "refusing to open it"
            )

        applied_any = False
        for version, migration in migrations:
            if version <= current_version or version > target:
                continue

            try:
                if callable(migration):
                    # This migration manages its own transaction/PRAGMA
                    # handling (and its own PRAGMA user_version write) --
                    # see e.g. _migrate_v1_to_v2 above.
                    migration(connection)
                else:
                    with transaction(connection, state):
                        for statement in migration:
                            connection.execute(statement)
                        _check_foreign_keys(connection, version)
                        # SQLite does not support bind parameters inside
                        # PRAGMA statements. `version` is an int literal from
                        # the hardcoded MIGRATIONS tuple above, never external
                        # input, so this is not a SQL-injection risk. The
                        # user_version write is part of the same transaction.
                        connection.execute(f"PRAGMA user_version = {int(version)}")
            except IntegrityCheckError:
                raise
            except Exception as error:
                raise MigrationError(f"failed to migrate database to schema v{version}: {error}") from error
            applied_any = True

        if applied_any:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise IntegrityCheckError(f"quick_check failed after migrating: {result}")


# -----------------------------------------------------------------------------
# Location and legacy (repository-local) database adoption
# -----------------------------------------------------------------------------


class LegacyAdoptionStatus(str, Enum):
    #: No repository-local database exists; nothing to do.
    NO_LEGACY_DATABASE = "no_legacy_database"
    #: The legacy database was copied to the (previously absent) target. The
    #: legacy file itself is left in place, unmodified, as a backup.
    COPIED = "copied"
    #: Both files exist. Nothing is copied or merged; the target is used and
    #: the legacy file is retained untouched.
    TARGET_EXISTS = "target_exists"
    #: Source and target are the same file (e.g. SCHEDULE_MAXING_DATA_DIR
    #: points at the checkout's data/ directory, retaining it in place).
    SAME_FILE = "same_file"


@dataclass(frozen=True)
class LegacyAdoptionResult:
    status: LegacyAdoptionStatus
    source: Path
    target: Path


def adopt_legacy_database(source: str | Path, target: str | Path) -> LegacyAdoptionResult:
    """
    Carry a repository-local executions.db (earlier milestones' default
    location) over to the per-user location, safely:

    - never deletes, moves, or writes to the source (it is opened read-only
      and stays behind as a backup);
    - never overwrites an existing target and never merges two histories --
      if the target already exists, nothing is done (TARGET_EXISTS);
    - copies through SQLite's online backup API (consistent even if the
      source has a WAL/journal), into a temporary file in the target
      directory that is only then linked into place, so a crash or a
      concurrent opener can never observe a half-written target.

    Schema migrations are *not* run here; get_connection runs them on the
    copy afterwards, exactly as for any other database. Raises
    LegacyAdoptionError (and leaves the target absent) if the source cannot
    be read as a SQLite database.
    """
    source_path = Path(source)
    target_path = Path(target)

    if not source_path.exists():
        return LegacyAdoptionResult(LegacyAdoptionStatus.NO_LEGACY_DATABASE, source_path, target_path)
    if target_path.exists():
        same = os.path.samefile(source_path, target_path)
        status = LegacyAdoptionStatus.SAME_FILE if same else LegacyAdoptionStatus.TARGET_EXISTS
        return LegacyAdoptionResult(status, source_path, target_path)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.adopting-{os.getpid()}-{threading.get_ident()}")

    try:
        source_connection = sqlite3.connect(f"{source_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            destination = sqlite3.connect(str(temp_path))
            try:
                source_connection.backup(destination)
            finally:
                destination.close()
        finally:
            source_connection.close()

        try:
            # os.link fails if the target appeared in the meantime -- it
            # never replaces an existing file (unlike os.replace on POSIX).
            os.link(temp_path, target_path)
        except FileExistsError:
            return LegacyAdoptionResult(LegacyAdoptionStatus.TARGET_EXISTS, source_path, target_path)
        except OSError:
            # Filesystems without hard links: rename, which on Windows also
            # refuses to replace an existing file; re-check first elsewhere.
            if target_path.exists():
                return LegacyAdoptionResult(LegacyAdoptionStatus.TARGET_EXISTS, source_path, target_path)
            os.rename(temp_path, target_path)
    except sqlite3.DatabaseError as error:
        raise LegacyAdoptionError(f"could not copy legacy database {source_path}: {error}") from error
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return LegacyAdoptionResult(LegacyAdoptionStatus.COPIED, source_path, target_path)


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Return the database file path to use: the explicit path if given, else the configured default."""
    if db_path is not None:
        return Path(db_path)
    return Path(settings.DATA_DIR) / settings.EXECUTION_DB_FILENAME


def _adopt_legacy_for_default_location(target: Path) -> None:
    """Only the implicit default location (no explicit path, no env override) adopts the legacy file."""
    result = adopt_legacy_database(Path(settings.LEGACY_DATA_DIR) / settings.EXECUTION_DB_FILENAME, target)
    if result.status == LegacyAdoptionStatus.COPIED:
        logger.warning(
            "Copied the repository-local database %s to the per-user data location %s. "
            "The original was left in place as a backup and is no longer used.",
            result.source, result.target,
        )
    elif result.status == LegacyAdoptionStatus.TARGET_EXISTS:
        logger.info(
            "A repository-local database still exists at %s; it was not merged into %s and is left untouched.",
            result.source, result.target,
        )


def get_connection(db_path: str | Path | None = None) -> AppConnection:
    """
    Open (creating if necessary) the application database.

    Ensures the parent directory exists, enables row access by column name,
    turns on foreign-key enforcement (off by default in SQLite), puts the
    connection in autocommit mode so transaction() owns every transaction,
    and applies any pending schema migrations before returning.

    When no db_path is given and SCHEDULE_MAXING_DATA_DIR is not set, an
    existing repository-local database from earlier milestones is first
    adopted into the per-user location (see adopt_legacy_database). An
    explicit db_path or data-directory override never triggers adoption.
    """
    resolved_path = resolve_db_path(db_path)
    in_memory = str(resolved_path) == ":memory:"
    if not in_memory:
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path is None and not settings.DATA_DIR_OVERRIDDEN:
            _adopt_legacy_for_default_location(resolved_path)

    # The desktop UI opens this connection once on the Tk main thread but
    # reads/writes it from background worker threads (see
    # app.ui.background.run_in_background), so the default same-thread
    # affinity check must be disabled. The AppConnection's shared lock (see
    # transaction()/locked()) serializes actual access so this stays safe.
    connection = sqlite3.connect(
        str(resolved_path), check_same_thread=False, isolation_level=None, factory=AppConnection
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_schema(connection)
    except BaseException:
        connection.close()
        raise
    return connection
