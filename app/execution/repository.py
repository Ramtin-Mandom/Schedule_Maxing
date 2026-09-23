"""
repository.py

The only module in this subsystem that writes raw SQL. All statements use
parameterized `?` placeholders; nothing here builds a SQL string out of
caller-supplied values.

ExecutionRepository is a thin, business-rule-free mapping between
TaskExecution/WorkSession and the `executions`/`work_sessions` tables. It
does not decide whether an operation makes sense (e.g. whether a status
transition is legal) — that belongs to app/execution/service.py. It does
enforce that the row it was asked to affect actually exists, by raising
ExecutionNotFoundError rather than silently doing nothing.

Transactions: every write goes through app.execution.db.transaction(), and
every read holds the connection's shared lock (app.execution.db.locked()).
Called on its own, a write method is its own atomic transaction; called
from inside ExecutionRepository.transaction() (as ExecutionService does for
each logical mutation), it joins the caller's transaction as a SAVEPOINT
and never commits it early. The lock is the connection's, not this
repository's, so every repository sharing one connection (including
app.planning.repository.PlanningRepository) is serialized together.

Link violations raised by the database's v3 triggers (see app/execution/
db.py, "Execution <-> planning links") are translated into
ExecutionLinkError.

Milestone 3 (schema v4): update_execution is an atomic compare-and-update
(`WHERE id = ? AND version = ? AND deleted_at IS NULL`) -- a stale write
raises ExecutionVersionConflictError and changes nothing. Deletion of one
execution is a tombstone (soft_delete_execution); every normal read skips
tombstones. An execution whose id is not a UUID has a durable wire id in
execution_wire_ids (see wire_id), assigned once and never reminted.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from app.execution.db import EXECUTION_LINK_VIOLATION, TransactionState, locked, transaction, transaction_state_for
from app.execution.errors import (
    ExecutionDeletedError,
    ExecutionError,
    ExecutionLinkError,
    ExecutionNotFoundError,
    ExecutionVersionConflictError,
)
from app.execution.models import ExecutionStatus, TaskExecution, WorkSession

_EXECUTION_COLUMNS = (
    "id",
    "task_name",
    "category",
    "tag",
    "planned_date",
    "planned_start",
    "planned_end",
    "planned_duration",
    "priority",
    "status",
    "created_at",
    "updated_at",
    "actual_active_duration_minutes",
    "duration_variance_minutes",
    "start_delay_minutes",
    "focus_rating",
    "energy_rating",
    "interruption_count",
    "note",
    "task_id",
    "scheduled_task_id",
    "user_id",
    "canonical_planned_date",
    "canonical_timezone",
    "canonical_planned_start",
    "canonical_planned_end",
    "actual_first_start_at",
    "actual_final_end_at",
    "version",
    "deleted_at",
)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class ExecutionRepository:
    """CRUD access to executions and their work sessions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        # The desktop UI shares one connection between the Tk main thread and
        # background worker threads (app.ui.background.run_in_background).
        # sqlite3 connections aren't safe under unsynchronized concurrent
        # access, so every method below holds the connection's shared lock
        # (see app.execution.db) while touching self._connection. A plain
        # sqlite3.Connection (not from get_connection) gets a private state.
        self._state = transaction_state_for(connection) or TransactionState()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic unit of work spanning any number of this repository's calls."""
        with transaction(self._connection, self._state):
            yield

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with locked(self._connection, self._state):
            yield self._connection

    # ------------------------------------------------------------------
    # Executions
    # ------------------------------------------------------------------

    def create_execution(self, execution: TaskExecution) -> TaskExecution:
        """Insert a brand-new execution row. Raises sqlite3.IntegrityError on a duplicate id."""
        values = _execution_to_row(execution)
        placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
        columns = ", ".join(_EXECUTION_COLUMNS)

        with self.transaction(), _translate_link_errors():
            self._connection.execute(
                f"INSERT INTO executions ({columns}) VALUES ({placeholders})",
                values,
            )
            self._assign_wire_id(execution.id)
        return execution

    def _assign_wire_id(self, execution_id: str) -> None:
        """A non-UUID id gets its durable wire id when its row is created (a UUID id is its own)."""
        if not _is_uuid(execution_id):
            self._connection.execute(
                "INSERT OR IGNORE INTO execution_wire_ids (execution_id, wire_id) VALUES (?, ?)",
                (execution_id, str(uuid.uuid4())),
            )

    def wire_id(self, execution_id: str) -> uuid.UUID:
        """
        The stable identity this execution has outside this device (see
        docs/sync-contract.md): the id itself when it is a UUID, otherwise
        the mapping assigned once (by migration v4 or at creation). The
        local id is never rewritten.
        """
        if _is_uuid(execution_id):
            return uuid.UUID(execution_id)
        with self._read():
            row = self._connection.execute(
                "SELECT wire_id FROM execution_wire_ids WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise ExecutionNotFoundError(execution_id)
        return uuid.UUID(row["wire_id"])

    def get_or_create_by_scheduled_task_id(self, execution: TaskExecution) -> TaskExecution:
        """
        Atomic identity-aware get-or-create for a canonical, placement-based
        execution: if a row already exists with this
        execution.scheduled_task_id, return it unchanged (its original
        planned snapshot is never overwritten); otherwise insert `execution`
        as a new row and return it. If the placement's execution exists only
        as a tombstone, ExecutionDeletedError is raised: it is neither
        revived nor silently duplicated.

        Requires execution.scheduled_task_id to be set -- callers with a
        task-only (no placement) execution should use create_execution
        instead, which never deduplicates.

        Atomicity: the existence check and the insert happen while holding
        this repository's lock for the whole operation (not as two separate
        locked calls), so two get-or-create calls racing on the same
        scheduled_task_id within this process cannot both insert. The
        partial unique index on executions(scheduled_task_id) (see
        app/execution/db.py's v2 migration) is the ultimate guarantee at the
        database level; the IntegrityError fallback below exists in case
        that index is ever hit despite the lock (e.g. a second writer on the
        same file from another process).
        """
        if execution.scheduled_task_id is None:
            raise ValueError("get_or_create_by_scheduled_task_id requires execution.scheduled_task_id to be set")

        scheduled_task_id = str(execution.scheduled_task_id)

        with self.transaction():
            existing = self._connection.execute(
                "SELECT * FROM executions WHERE scheduled_task_id = ?",
                (scheduled_task_id,),
            ).fetchone()
            if existing is not None:
                if existing["deleted_at"] is not None:
                    raise ExecutionDeletedError(existing["id"])
                return _row_to_execution(existing)

            values = _execution_to_row(execution)
            placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
            columns = ", ".join(_EXECUTION_COLUMNS)

            try:
                with _translate_link_errors():
                    self._connection.execute(
                        f"INSERT INTO executions ({columns}) VALUES ({placeholders})",
                        values,
                    )
                    self._assign_wire_id(execution.id)
            except sqlite3.IntegrityError:
                row = self._connection.execute(
                    "SELECT * FROM executions WHERE scheduled_task_id = ? AND deleted_at IS NULL",
                    (scheduled_task_id,),
                ).fetchone()
                if row is not None:
                    return _row_to_execution(row)
                raise

        return execution

    def update_execution(self, execution: TaskExecution, *, expected_version: int) -> TaskExecution:
        """
        Atomically overwrite a live execution row with the given state, only
        if it is still at `expected_version` (compare-and-update).

        Raises ExecutionNotFoundError if no row with this id exists (callers
        must create before updating), and ExecutionVersionConflictError --
        leaving the row unchanged -- if it is at another version or has been
        deleted. The caller sets the new version (see ExecutionService's
        logical-mutation rule); this method stores it verbatim.
        """
        assignments = ", ".join(f"{column} = ?" for column in _EXECUTION_COLUMNS if column != "id")
        values = [
            value
            for column, value in zip(_EXECUTION_COLUMNS, _execution_to_row(execution))
            if column != "id"
        ]
        values.extend([execution.id, expected_version])

        with self.transaction(), _translate_link_errors():
            cursor = self._connection.execute(
                f"UPDATE executions SET {assignments} WHERE id = ? AND version = ? AND deleted_at IS NULL",
                values,
            )
            if cursor.rowcount == 0:
                self._raise_precondition_failure(execution.id, expected_version)

        return execution

    def soft_delete_execution(self, execution_id: str, *, expected_version: int, deleted_at: datetime) -> None:
        """Tombstone a live execution at `expected_version` (version + 1). Its work sessions are kept."""
        stamp = deleted_at.astimezone(timezone.utc).isoformat()
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE executions SET deleted_at = ?, updated_at = ?, version = version + 1 "
                "WHERE id = ? AND version = ? AND deleted_at IS NULL",
                (stamp, stamp, execution_id, expected_version),
            )
            if cursor.rowcount == 0:
                self._raise_precondition_failure(execution_id, expected_version)

    def _raise_precondition_failure(self, execution_id: str, expected_version: int) -> None:
        row = self._connection.execute(
            "SELECT version, deleted_at FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise ExecutionNotFoundError(execution_id)
        raise ExecutionVersionConflictError(
            execution_id, expected_version=expected_version, current_version=row["version"],
            deleted=row["deleted_at"] is not None,
        )

    def get_execution(self, execution_id: str, *, include_deleted: bool = False) -> TaskExecution:
        """Raises ExecutionNotFoundError if no such (live, unless include_deleted) execution exists."""
        live = "" if include_deleted else " AND deleted_at IS NULL"
        with self._read():
            row = self._connection.execute(
                f"SELECT * FROM executions WHERE id = ?{live}",
                (execution_id,),
            ).fetchone()

        if row is None:
            raise ExecutionNotFoundError(execution_id)

        return _row_to_execution(row)

    def find_by_scheduled_task_id(self, scheduled_task_id: str) -> TaskExecution | None:
        """The live execution recorded for a placement id, if any (lookup only, never creates)."""
        with self._read():
            row = self._connection.execute(
                "SELECT * FROM executions WHERE scheduled_task_id = ? AND deleted_at IS NULL",
                (scheduled_task_id,),
            ).fetchone()
        return _row_to_execution(row) if row is not None else None

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        """Return all live executions, optionally filtered by status, oldest first."""
        with self._read():
            if status is None:
                rows = self._connection.execute(
                    "SELECT * FROM executions WHERE deleted_at IS NULL ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM executions WHERE status = ? AND deleted_at IS NULL ORDER BY created_at",
                    (status.value,),
                ).fetchall()

        return [_row_to_execution(row) for row in rows]

    def delete_all_executions(self) -> int:
        """
        Permanently delete every execution (and, via the schema's
        ON DELETE CASCADE, every work session and wire-id mapping). Returns
        the number of executions deleted.

        This is a destructive, explicit local reset operation -- callers (see
        ExecutionService.reset_all_history) are expected to gate it behind
        a user confirmation; this method itself performs no confirmation.
        It is a local purge, not a synchronizable deletion (see
        docs/sync-contract.md); deleting one execution is a tombstone.
        """
        with self.transaction():
            cursor = self._connection.execute("DELETE FROM executions")
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Work sessions
    # ------------------------------------------------------------------

    def create_session(self, execution_id: str, started_at: str) -> WorkSession:
        with self.transaction():
            cursor = self._connection.execute(
                "INSERT INTO work_sessions (execution_id, started_at) VALUES (?, ?)",
                (execution_id, started_at),
            )
        return WorkSession(id=cursor.lastrowid, execution_id=execution_id, started_at=started_at)

    def close_session(self, session_id: int, ended_at: str) -> WorkSession:
        """
        Set ended_at on a currently-open session.

        Raises ExecutionError if the session does not exist or is already closed.
        """
        with self.transaction():
            cursor = self._connection.execute(
                "UPDATE work_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (ended_at, session_id),
            )
            if cursor.rowcount == 0:
                raise ExecutionError(
                    f"No open work session with id={session_id!r} to close."
                )

            row = self._connection.execute(
                "SELECT * FROM work_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _row_to_session(row)

    def get_open_session(self, execution_id: str) -> WorkSession | None:
        """Return the currently-open session for this execution, if any."""
        with self._read():
            row = self._connection.execute(
                "SELECT * FROM work_sessions WHERE execution_id = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        """Return all sessions for an execution, in chronological order."""
        with self._read():
            rows = self._connection.execute(
                "SELECT * FROM work_sessions WHERE execution_id = ? ORDER BY started_at",
                (execution_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]


    def store_synced(self, execution: TaskExecution, sessions: list[tuple[str, str | None]]) -> None:
        """
        Insert or overwrite one execution aggregate (row and sessions) exactly as
        given, for app/sync applying a server-accepted record when the device has
        no pending change of it. A non-UUID id keeps a wire-id mapping.
        """
        columns = ", ".join(_EXECUTION_COLUMNS)
        placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
        assignments = ", ".join(f"{column} = excluded.{column}" for column in _EXECUTION_COLUMNS if column != "id")
        with self.transaction():
            self._connection.execute(
                f"INSERT INTO executions ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {assignments}",
                _execution_to_row(execution),
            )
            self._connection.execute("DELETE FROM work_sessions WHERE execution_id = ?", (execution.id,))
            self._connection.executemany(
                "INSERT INTO work_sessions (execution_id, started_at, ended_at) VALUES (?, ?, ?)",
                [(execution.id, started, ended) for started, ended in sessions],
            )

    def set_wire_id(self, execution_id: str, wire_id: uuid.UUID) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO execution_wire_ids (execution_id, wire_id) VALUES (?, ?)",
                (execution_id, str(wire_id)),
            )

    def local_id_for_wire(self, wire_id: uuid.UUID) -> str | None:
        """The local id of the execution with this wire id, if the device has it."""
        with self._read():
            row = self._connection.execute(
                "SELECT execution_id FROM execution_wire_ids WHERE wire_id = ?", (str(wire_id),)
            ).fetchone()
            if row is not None:
                return row["execution_id"]
            row = self._connection.execute("SELECT id FROM executions WHERE id = ?", (str(wire_id),)).fetchone()
        return row["id"] if row is not None else None

    def list_executions_with_unresolved_links(self) -> list[TaskExecution]:
        """
        Live executions whose historical task_id/scheduled_task_id no longer
        (or never did) resolve to a live persisted task/placement -- pre-v3
        rows whose parents were never persisted, and rows whose
        task/placement was later deleted (tombstoned) or replaced. Their
        snapshots remain the record of what was planned; nothing is
        fabricated for them.
        """
        with self._read():
            rows = self._connection.execute(
                "SELECT e.* FROM executions AS e WHERE e.deleted_at IS NULL AND ("
                "(e.task_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM tasks AS t WHERE t.id = e.task_id AND t.deleted_at IS NULL)) "
                "OR (e.scheduled_task_id IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM scheduled_tasks AS s WHERE s.id = e.scheduled_task_id AND s.deleted_at IS NULL))) "
                "ORDER BY e.created_at, e.id"
            ).fetchall()
        return [_row_to_execution(row) for row in rows]


@contextmanager
def _translate_link_errors() -> Iterator[None]:
    try:
        yield
    except sqlite3.IntegrityError as error:
        if EXECUTION_LINK_VIOLATION in str(error):
            raise ExecutionLinkError(str(error)) from error
        raise


def _execution_to_row(execution: TaskExecution) -> tuple:
    return (
        execution.id,
        execution.task_name,
        execution.category,
        execution.tag,
        execution.planned_date,
        execution.planned_start,
        execution.planned_end,
        execution.planned_duration,
        execution.priority,
        execution.status.value,
        execution.created_at,
        execution.updated_at,
        execution.actual_active_duration_minutes,
        execution.duration_variance_minutes,
        execution.start_delay_minutes,
        execution.focus_rating,
        execution.energy_rating,
        execution.interruption_count,
        execution.note,
        _str_or_none(execution.task_id),
        _str_or_none(execution.scheduled_task_id),
        _str_or_none(execution.user_id),
        _iso_or_none(execution.canonical_planned_date),
        execution.canonical_timezone,
        _iso_or_none(execution.canonical_planned_start),
        _iso_or_none(execution.canonical_planned_end),
        _iso_or_none(execution.actual_first_start_at),
        _iso_or_none(execution.actual_final_end_at),
        execution.version,
        execution.deleted_at,
    )


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_execution(row: sqlite3.Row) -> TaskExecution:
    return TaskExecution.model_validate(dict(row))


def _row_to_session(row: sqlite3.Row) -> WorkSession:
    return WorkSession.model_validate(dict(row))
