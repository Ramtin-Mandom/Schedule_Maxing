"""
app/planning/repository.py

SQLite persistence for the canonical planning models (app/planning/models.py):
Project, Task (with its tags, preferred dates, preferred time window,
dependencies, deadline, and recurrence rule), FixedBlock, and ScheduledTask
placements. Tables are created by schema migration v3 in app/execution/db.py
-- the same database file and migration chain as execution history, not a
second database.

Like app/execution/repository.py, this is the only planning module that
writes SQL, every statement is parameterized (`?` placeholders; the only
interpolated fragments are internal constant column lists/WHERE clauses),
and it holds no business rules: which ids may be deleted, what a
replacement scope means, how versions advance, and which tasks are eligible
for a date range are decided by app/planning/application.py. The repository
maps models <-> rows faithfully and exactly (UUIDs, aware timestamps with
their original UTC offsets, versions, nullable fields, ordered collections).

Every read returns freshly constructed model instances, so mutating a
returned model never changes stored state (or another caller's copy).

Transactions: each write method is atomic on its own; inside
PlanningRepository.transaction() (or any enclosing app.execution.db
transaction on the same connection, e.g. a service operation spanning
planning and execution writes) it joins that transaction as a savepoint.
The lock is the connection's shared lock, so this repository and
ExecutionRepository serialize together when they share a connection.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date as date_
from datetime import datetime, timezone
from typing import Any

from app.execution.db import EXECUTION_LINK_VIOLATION, TransactionState, locked, transaction, transaction_state_for
from app.planning.errors import InvalidEntityError
from app.planning.models import FixedBlock, Project, ScheduledTask, Task

# SQLite's historical default limit on bound variables is 999; stay well below.
_IN_CHUNK = 500

_PROJECT_COLUMNS = ("id", "user_id", "name", "description", "created_at", "updated_at", "version")

_TASK_COLUMNS = (
    "id", "user_id", "project_id", "name", "category",
    "estimated_duration_minutes", "priority", "required", "required_date",
    "preferred_window_start_minute", "preferred_window_end_minute",
    "deadline", "deadline_utc",
    "recurrence_frequency", "recurrence_interval", "recurrence_day_of_month",
    "recurrence_end_date", "recurrence_count",
    "created_at", "updated_at", "version",
)

_FIXED_BLOCK_COLUMNS = (
    "id", "label", "planned_date", "timezone", "planned_start", "planned_end",
    "planned_start_utc", "planned_end_utc",
)

_PLACEMENT_COLUMNS = (
    "id", "task_id", "planned_date", "timezone", "planned_start", "planned_end",
    "planned_start_utc", "planned_end_utc", "score", "optimization_metadata",
    "created_at", "updated_at", "version",
)

# Deterministic task eligibility for an inclusive date range, mirroring
# app.planning.allocation._feasible_dates_for_task's hard date rules (see
# app/planning/application.py for the documented semantics). Parameters:
# (start, end, start).
_ELIGIBLE_FOR_RANGE_WHERE = (
    "(required_date IS NOT NULL AND required_date BETWEEN ? AND ?) "
    "OR (required_date IS NULL AND (deadline_utc IS NULL OR substr(deadline_utc, 1, 10) >= ?))"
)


# A task's planned date: its required_date, else its earliest preferred date,
# else NULL (an undated, floating task). Mirrored in Python by
# app.planning.application.task_planned_date.
_PLANNED_DATE_SQL = (
    "COALESCE(required_date, "
    "(SELECT MIN(p.preferred_date) FROM task_preferred_dates AS p WHERE p.task_id = tasks.id))"
)

# Tasks planned inside an inclusive range, plus undated tasks eligible for it.
# Parameters: (start, end, start).
_PLANNED_IN_RANGE_WHERE = (
    f"({_PLANNED_DATE_SQL} BETWEEN ? AND ?) "
    f"OR ({_PLANNED_DATE_SQL} IS NULL AND (deadline_utc IS NULL OR substr(deadline_utc, 1, 10) >= ?))"
)

# Only dated tasks whose planned date is inside the range. Parameters: (start, end).
_DATED_IN_RANGE_WHERE = f"{_PLANNED_DATE_SQL} BETWEEN ? AND ?"


def _upsert_sql(table: str, columns: Sequence[str]) -> str:
    column_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
    # ON CONFLICT DO UPDATE (not INSERT OR REPLACE): REPLACE would delete the
    # old row first and fire ON DELETE CASCADE into child rows/placements.
    return f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {assignments}"


def _chunks(values: Sequence[str]) -> Iterator[Sequence[str]]:
    for offset in range(0, len(values), _IN_CHUNK):
        yield values[offset:offset + _IN_CHUNK]


def _placeholders(values: Sequence[Any]) -> str:
    return ", ".join("?" for _ in values)


def _ids(values: Iterable[uuid.UUID]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))


def _iso(value: date_ | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc_text(value: datetime) -> str:
    """Fixed-width UTC text, so these columns sort/compare correctly as strings."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value is not None else None


def _date_or_none(value: str | None) -> date_ | None:
    return date_.fromisoformat(value) if value is not None else None


def _datetime_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class PlanningRepository:
    """Row mapping and queries for persisted canonical planning entities."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
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
    # Projects
    # ------------------------------------------------------------------

    def upsert_project(self, project: Project) -> None:
        with self.transaction():
            self._connection.execute(_upsert_sql("projects", _PROJECT_COLUMNS), _project_to_row(project))

    def get_project(self, project_id: uuid.UUID) -> Project | None:
        with self._read():
            row = self._connection.execute("SELECT * FROM projects WHERE id = ?", (str(project_id),)).fetchone()
        return _row_to_project(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        with self._read():
            rows = self._connection.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        return [_row_to_project(row) for row in rows]

    def delete_project(self, project_id: uuid.UUID) -> bool:
        with self.transaction():
            cursor = self._connection.execute("DELETE FROM projects WHERE id = ?", (str(project_id),))
        return cursor.rowcount > 0

    def task_ids_for_project(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        with self._read():
            rows = self._connection.execute(
                "SELECT id FROM tasks WHERE project_id = ? ORDER BY id", (str(project_id),)
            ).fetchall()
        return [uuid.UUID(row["id"]) for row in rows]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def upsert_task(self, task: Task) -> None:
        """Insert or fully overwrite one task and its child rows (atomic)."""
        task_id = str(task.id)
        with self.transaction():
            self._connection.execute(_upsert_sql("tasks", _TASK_COLUMNS), _task_to_row(task))

            for table in ("task_tags", "task_preferred_dates", "task_dependencies", "task_recurrence_weekdays"):
                self._connection.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))

            self._connection.executemany(
                "INSERT INTO task_tags (task_id, position, tag) VALUES (?, ?, ?)",
                [(task_id, position, tag) for position, tag in enumerate(task.tags)],
            )
            self._connection.executemany(
                "INSERT INTO task_preferred_dates (task_id, position, preferred_date) VALUES (?, ?, ?)",
                [(task_id, position, day.isoformat()) for position, day in enumerate(task.preferred_dates)],
            )
            self._connection.executemany(
                "INSERT INTO task_dependencies (task_id, position, depends_on_task_id) VALUES (?, ?, ?)",
                [(task_id, position, str(dependency)) for position, dependency in enumerate(task.dependency_ids)],
            )
            weekdays = task.recurrence.weekdays if task.recurrence is not None else None
            self._connection.executemany(
                "INSERT INTO task_recurrence_weekdays (task_id, weekday) VALUES (?, ?)",
                [(task_id, weekday) for weekday in weekdays or []],
            )

    def get_task(self, task_id: uuid.UUID) -> Task | None:
        return self.get_tasks([task_id]).get(task_id)

    def get_tasks(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Task]:
        ids = _ids(task_ids)
        tasks: dict[uuid.UUID, Task] = {}
        with self._read():
            for chunk in _chunks(ids):
                for task in self._load_tasks(f"id IN ({_placeholders(chunk)})", tuple(chunk)):
                    tasks[task.id] = task
        return tasks

    def list_tasks(self) -> list[Task]:
        """Every task, ordered by (created_at, id) -- deterministic across reopen."""
        with self._read():
            return self._load_tasks("1 = 1", ())

    def list_tasks_eligible_for_range(self, start_date: date_, end_date: date_) -> list[Task]:
        """Tasks whose hard date rules allow some date in [start_date, end_date]; ordered by (created_at, id)."""
        with self._read():
            return self._load_tasks(
                _ELIGIBLE_FOR_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat(), start_date.isoformat())
            )

    def list_tasks_planned_in_range(
        self, start_date: date_, end_date: date_, *, include_undated: bool = True
    ) -> list[Task]:
        """
        Tasks whose planned date (required_date, else earliest preferred date)
        is in [start_date, end_date]; with include_undated, also undated tasks
        eligible for the range. Ordered by (created_at, id).
        """
        with self._read():
            if include_undated:
                return self._load_tasks(
                    _PLANNED_IN_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat(), start_date.isoformat())
                )
            return self._load_tasks(_DATED_IN_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat()))

    def existing_task_ids(self, task_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        return self._existing_ids("tasks", task_ids)

    def existing_project_ids(self, project_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        return self._existing_ids("projects", project_ids)

    def dependents_of(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
        """For each given task id that others depend on: the set of dependent task ids."""
        ids = _ids(task_ids)
        dependents: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        with self._read():
            for chunk in _chunks(ids):
                rows = self._connection.execute(
                    f"SELECT task_id, depends_on_task_id FROM task_dependencies "
                    f"WHERE depends_on_task_id IN ({_placeholders(chunk)})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    dependents[uuid.UUID(row["depends_on_task_id"])].add(uuid.UUID(row["task_id"]))
        return dict(dependents)

    def delete_tasks(self, task_ids: Iterable[uuid.UUID]) -> int:
        """
        Delete tasks. Their own tags/preferred dates/dependency edges/
        recurrence weekdays and their placements are removed by ON DELETE
        CASCADE; execution history is never touched (no foreign key from
        executions). Edges *to* a deleted task from a surviving task violate
        the deferred foreign key and fail the enclosing transaction at
        COMMIT -- callers check dependents_of first.
        """
        ids = _ids(task_ids)
        deleted = 0
        with self.transaction():
            for chunk in _chunks(ids):
                cursor = self._connection.execute(f"DELETE FROM tasks WHERE id IN ({_placeholders(chunk)})", tuple(chunk))
                deleted += cursor.rowcount
        return deleted

    def _load_tasks(self, where_sql: str, params: tuple) -> list[Task]:
        # Caller holds the lock. where_sql is always an internal constant.
        rows = self._connection.execute(f"SELECT * FROM tasks WHERE {where_sql} ORDER BY created_at, id", params).fetchall()
        if not rows:
            return []

        subquery = f"SELECT id FROM tasks WHERE {where_sql}"
        tags: dict[str, list[str]] = defaultdict(list)
        for child in self._connection.execute(
            f"SELECT task_id, tag FROM task_tags WHERE task_id IN ({subquery}) ORDER BY task_id, position", params
        ):
            tags[child["task_id"]].append(child["tag"])

        preferred_dates: dict[str, list[str]] = defaultdict(list)
        for child in self._connection.execute(
            f"SELECT task_id, preferred_date FROM task_preferred_dates WHERE task_id IN ({subquery}) "
            "ORDER BY task_id, position",
            params,
        ):
            preferred_dates[child["task_id"]].append(child["preferred_date"])

        dependencies: dict[str, list[str]] = defaultdict(list)
        for child in self._connection.execute(
            f"SELECT task_id, depends_on_task_id FROM task_dependencies WHERE task_id IN ({subquery}) "
            "ORDER BY task_id, position",
            params,
        ):
            dependencies[child["task_id"]].append(child["depends_on_task_id"])

        weekdays: dict[str, list[int]] = defaultdict(list)
        for child in self._connection.execute(
            f"SELECT task_id, weekday FROM task_recurrence_weekdays WHERE task_id IN ({subquery}) "
            "ORDER BY task_id, weekday",
            params,
        ):
            weekdays[child["task_id"]].append(child["weekday"])

        return [
            _row_to_task(row, tags[row["id"]], preferred_dates[row["id"]], dependencies[row["id"]], weekdays[row["id"]])
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def upsert_fixed_block(self, block: FixedBlock) -> None:
        with self.transaction():
            self._connection.execute(_upsert_sql("fixed_blocks", _FIXED_BLOCK_COLUMNS), _fixed_block_to_row(block))

    def get_fixed_blocks(self, block_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, FixedBlock]:
        blocks: dict[uuid.UUID, FixedBlock] = {}
        with self._read():
            for chunk in _chunks(_ids(block_ids)):
                rows = self._connection.execute(
                    f"SELECT * FROM fixed_blocks WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                ).fetchall()
                for row in rows:
                    block = _row_to_fixed_block(row)
                    blocks[block.id] = block
        return blocks

    def list_fixed_blocks(self, start_date: date_, end_date: date_) -> list[FixedBlock]:
        """Blocks with planned_date in [start_date, end_date], ordered by (planned_date, start, id)."""
        with self._read():
            rows = self._connection.execute(
                "SELECT * FROM fixed_blocks WHERE planned_date BETWEEN ? AND ? "
                "ORDER BY planned_date, planned_start_utc, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [_row_to_fixed_block(row) for row in rows]

    def delete_fixed_blocks(self, block_ids: Iterable[uuid.UUID]) -> int:
        deleted = 0
        with self.transaction():
            for chunk in _chunks(_ids(block_ids)):
                cursor = self._connection.execute(
                    f"DELETE FROM fixed_blocks WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                )
                deleted += cursor.rowcount
        return deleted

    # ------------------------------------------------------------------
    # Placements (ScheduledTask)
    # ------------------------------------------------------------------

    def upsert_placement(self, placement: ScheduledTask) -> None:
        with self.transaction():
            try:
                self._connection.execute(_upsert_sql("scheduled_tasks", _PLACEMENT_COLUMNS), _placement_to_row(placement))
            except sqlite3.IntegrityError as error:
                if EXECUTION_LINK_VIOLATION in str(error):
                    raise InvalidEntityError(f"placement {placement.id}: {error}") from error
                raise

    def get_placements(self, placement_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, ScheduledTask]:
        placements: dict[uuid.UUID, ScheduledTask] = {}
        with self._read():
            for chunk in _chunks(_ids(placement_ids)):
                rows = self._connection.execute(
                    f"SELECT * FROM scheduled_tasks WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                ).fetchall()
                for row in rows:
                    placement = _row_to_placement(row)
                    placements[placement.id] = placement
        return placements

    def list_placements(self, start_date: date_, end_date: date_) -> list[ScheduledTask]:
        """Placements with planned_date in [start_date, end_date], ordered by (planned_date, start, id)."""
        with self._read():
            rows = self._connection.execute(
                "SELECT * FROM scheduled_tasks WHERE planned_date BETWEEN ? AND ? "
                "ORDER BY planned_date, planned_start_utc, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [_row_to_placement(row) for row in rows]

    def delete_placements(self, placement_ids: Iterable[uuid.UUID]) -> int:
        """Delete placements. Execution rows referencing them are untouched (historical identity)."""
        deleted = 0
        with self.transaction():
            for chunk in _chunks(_ids(placement_ids)):
                cursor = self._connection.execute(
                    f"DELETE FROM scheduled_tasks WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                )
                deleted += cursor.rowcount
        return deleted

    def placement_ids_with_history(self, placement_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """The subset of placement ids that some execution row references."""
        found: set[uuid.UUID] = set()
        with self._read():
            for chunk in _chunks(_ids(placement_ids)):
                rows = self._connection.execute(
                    f"SELECT DISTINCT scheduled_task_id FROM executions "
                    f"WHERE scheduled_task_id IN ({_placeholders(chunk)})",
                    tuple(chunk),
                ).fetchall()
                found.update(uuid.UUID(row["scheduled_task_id"]) for row in rows)
        return found

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _existing_ids(self, table: str, ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        found: set[uuid.UUID] = set()
        with self._read():
            for chunk in _chunks(_ids(ids)):
                rows = self._connection.execute(
                    f"SELECT id FROM {table} WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                ).fetchall()
                found.update(uuid.UUID(row["id"]) for row in rows)
        return found


# -----------------------------------------------------------------------------
# Row mapping
# -----------------------------------------------------------------------------


def _project_to_row(project: Project) -> tuple:
    return (
        str(project.id), str(project.user_id) if project.user_id else None, project.name, project.description,
        project.created_at.isoformat(), project.updated_at.isoformat(), project.version,
    )


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project.model_validate(dict(row))


def _task_to_row(task: Task) -> tuple:
    window = task.preferred_time_window
    recurrence = task.recurrence
    return (
        str(task.id),
        str(task.user_id) if task.user_id else None,
        str(task.project_id) if task.project_id else None,
        task.name,
        task.category,
        task.estimated_duration_minutes,
        task.priority,
        int(task.required),
        _iso(task.required_date),
        window.start_minute if window else None,
        window.end_minute if window else None,
        _iso(task.deadline),
        _utc_text(task.deadline) if task.deadline is not None else None,
        recurrence.frequency.value if recurrence else None,
        recurrence.interval if recurrence else None,
        recurrence.day_of_month if recurrence else None,
        _iso(recurrence.end_date) if recurrence else None,
        recurrence.count if recurrence else None,
        task.created_at.isoformat(),
        task.updated_at.isoformat(),
        task.version,
    )


def _row_to_task(
    row: sqlite3.Row, tags: list[str], preferred_dates: list[str], dependency_ids: list[str], weekdays: list[int]
) -> Task:
    window = None
    if row["preferred_window_start_minute"] is not None:
        window = {"start_minute": row["preferred_window_start_minute"], "end_minute": row["preferred_window_end_minute"]}

    recurrence = None
    if row["recurrence_frequency"] is not None:
        recurrence = {
            "frequency": row["recurrence_frequency"],
            "interval": row["recurrence_interval"],
            "weekdays": list(weekdays) or None,
            "day_of_month": row["recurrence_day_of_month"],
            "end_date": _date_or_none(row["recurrence_end_date"]),
            "count": row["recurrence_count"],
        }

    return Task.model_validate(
        {
            "id": uuid.UUID(row["id"]),
            "user_id": _uuid_or_none(row["user_id"]),
            "project_id": _uuid_or_none(row["project_id"]),
            "name": row["name"],
            "category": row["category"],
            "tags": list(tags),
            "estimated_duration_minutes": row["estimated_duration_minutes"],
            "priority": row["priority"],
            "required": bool(row["required"]),
            "required_date": _date_or_none(row["required_date"]),
            "preferred_dates": [date_.fromisoformat(value) for value in preferred_dates],
            "preferred_time_window": window,
            "dependency_ids": [uuid.UUID(value) for value in dependency_ids],
            "deadline": _datetime_or_none(row["deadline"]),
            "recurrence": recurrence,
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "version": row["version"],
        }
    )


def _fixed_block_to_row(block: FixedBlock) -> tuple:
    return (
        str(block.id), block.label, block.planned_date.isoformat(), block.timezone,
        block.planned_start.isoformat(), block.planned_end.isoformat(),
        _utc_text(block.planned_start), _utc_text(block.planned_end),
    )


def _row_to_fixed_block(row: sqlite3.Row) -> FixedBlock:
    return FixedBlock.model_validate(
        {
            "id": uuid.UUID(row["id"]),
            "label": row["label"],
            "planned_date": date_.fromisoformat(row["planned_date"]),
            "timezone": row["timezone"],
            "planned_start": datetime.fromisoformat(row["planned_start"]),
            "planned_end": datetime.fromisoformat(row["planned_end"]),
        }
    )


def _placement_to_row(placement: ScheduledTask) -> tuple:
    try:
        metadata = json.dumps(placement.optimization_metadata, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidEntityError(
            f"placement {placement.id}: optimization_metadata must be JSON-serializable ({error})"
        ) from error

    return (
        str(placement.id), str(placement.task_id), placement.planned_date.isoformat(), placement.timezone,
        placement.planned_start.isoformat(), placement.planned_end.isoformat(),
        _utc_text(placement.planned_start), _utc_text(placement.planned_end),
        placement.score, metadata,
        placement.created_at.isoformat(), placement.updated_at.isoformat(), placement.version,
    )


def _row_to_placement(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask.model_validate(
        {
            "id": uuid.UUID(row["id"]),
            "task_id": uuid.UUID(row["task_id"]),
            "planned_date": date_.fromisoformat(row["planned_date"]),
            "timezone": row["timezone"],
            "planned_start": datetime.fromisoformat(row["planned_start"]),
            "planned_end": datetime.fromisoformat(row["planned_end"]),
            "score": row["score"],
            "optimization_metadata": json.loads(row["optimization_metadata"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "version": row["version"],
        }
    )
