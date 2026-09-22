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
"""

from __future__ import annotations

import sqlite3
import threading

from app.execution.errors import ExecutionError, ExecutionNotFoundError
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
)


class ExecutionRepository:
    """CRUD access to executions and their work sessions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        # The desktop UI shares one connection between the Tk main thread and
        # background worker threads (app.ui.background.run_in_background).
        # sqlite3 connections aren't safe under unsynchronized concurrent
        # access, so every method below takes this lock before touching
        # self._connection.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Executions
    # ------------------------------------------------------------------

    def create_execution(self, execution: TaskExecution) -> TaskExecution:
        """Insert a brand-new execution row. Raises sqlite3.IntegrityError on a duplicate id."""
        values = _execution_to_row(execution)
        placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
        columns = ", ".join(_EXECUTION_COLUMNS)

        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO executions ({columns}) VALUES ({placeholders})",
                values,
            )
        return execution

    def get_or_create_by_scheduled_task_id(self, execution: TaskExecution) -> TaskExecution:
        """
        Atomic identity-aware get-or-create for a canonical, placement-based
        execution: if a row already exists with this
        execution.scheduled_task_id, return it unchanged (its original
        planned snapshot is never overwritten); otherwise insert `execution`
        as a new row and return it.

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

        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM executions WHERE scheduled_task_id = ?",
                (scheduled_task_id,),
            ).fetchone()
            if existing is not None:
                return _row_to_execution(existing)

            values = _execution_to_row(execution)
            placeholders = ", ".join("?" for _ in _EXECUTION_COLUMNS)
            columns = ", ".join(_EXECUTION_COLUMNS)

            with self._connection:
                try:
                    self._connection.execute(
                        f"INSERT INTO executions ({columns}) VALUES ({placeholders})",
                        values,
                    )
                except sqlite3.IntegrityError:
                    row = self._connection.execute(
                        "SELECT * FROM executions WHERE scheduled_task_id = ?",
                        (scheduled_task_id,),
                    ).fetchone()
                    if row is not None:
                        return _row_to_execution(row)
                    raise

        return execution

    def update_execution(self, execution: TaskExecution) -> TaskExecution:
        """
        Overwrite an existing execution row with the given state.

        Raises ExecutionNotFoundError if no row with this id exists, rather
        than silently inserting one — callers must create before updating.
        """
        assignments = ", ".join(f"{column} = ?" for column in _EXECUTION_COLUMNS if column != "id")
        values = [
            value
            for column, value in zip(_EXECUTION_COLUMNS, _execution_to_row(execution))
            if column != "id"
        ]
        values.append(execution.id)

        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE executions SET {assignments} WHERE id = ?",
                values,
            )
            if cursor.rowcount == 0:
                raise ExecutionNotFoundError(execution.id)

        return execution

    def get_execution(self, execution_id: str) -> TaskExecution:
        """Raises ExecutionNotFoundError if no such execution exists."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM executions WHERE id = ?",
                (execution_id,),
            ).fetchone()

        if row is None:
            raise ExecutionNotFoundError(execution_id)

        return _row_to_execution(row)

    def list_executions(self, status: ExecutionStatus | None = None) -> list[TaskExecution]:
        """Return all executions, optionally filtered by status, oldest first."""
        with self._lock:
            if status is None:
                rows = self._connection.execute(
                    "SELECT * FROM executions ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM executions WHERE status = ? ORDER BY created_at",
                    (status.value,),
                ).fetchall()

        return [_row_to_execution(row) for row in rows]

    def delete_all_executions(self) -> int:
        """
        Permanently delete every execution (and, via the schema's
        ON DELETE CASCADE, every work session). Returns the number of
        executions deleted.

        This is a destructive, explicit reset operation -- callers (see
        ExecutionService.reset_all_history) are expected to gate it behind
        a user confirmation; this method itself performs no confirmation.
        """
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM executions")
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Work sessions
    # ------------------------------------------------------------------

    def create_session(self, execution_id: str, started_at: str) -> WorkSession:
        with self._lock, self._connection:
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
        with self._lock:
            with self._connection:
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
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM work_sessions WHERE execution_id = ? AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (execution_id,),
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def list_sessions(self, execution_id: str) -> list[WorkSession]:
        """Return all sessions for an execution, in chronological order."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM work_sessions WHERE execution_id = ? ORDER BY started_at",
                (execution_id,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]


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
    )


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_execution(row: sqlite3.Row) -> TaskExecution:
    return TaskExecution.model_validate(dict(row))


def _row_to_session(row: sqlite3.Row) -> WorkSession:
    return WorkSession.model_validate(dict(row))
