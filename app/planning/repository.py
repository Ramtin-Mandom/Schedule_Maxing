"""
app/planning/repository.py

SQLite persistence for the canonical planning models (app/planning/models.py):
Project, Task (with its tags, preferred dates, preferred time window,
dependencies, deadline, and recurrence rule), FixedBlock, ScheduledTask
placements, persisted preference layers (PreferenceRecord) and schedule
provenance (GenerationRecord). Tables are created by schema migrations v3/v4
in app/execution/db.py -- the same database file and migration chain as
execution history, not a second database.

Like app/execution/repository.py, this is the only planning module that
writes SQL, every statement is parameterized (`?` placeholders; the only
interpolated fragments are internal constant table/column lists and WHERE
clauses), and it holds no business rules: which ids may be deleted, what a
replacement scope means, how versions advance, and which tasks are eligible
for a date range are decided by app/planning/application.py. The repository
maps models <-> rows faithfully and exactly (UUIDs, aware timestamps with
their original UTC offsets, versions, nullable fields, ordered collections).

Writes (Milestone 3) -- there is no silent last-write-wins primitive:
    - insert_*: a new row; an id that already exists (live *or* tombstoned)
      raises DuplicateEntityError.
    - update_*(model, expected_version=...): an atomic compare-and-update --
      one `UPDATE ... WHERE id = ? AND version = ? AND deleted_at IS NULL`.
      Returns False (and changes nothing) when the stored record is absent,
      tombstoned, or at another version; the service turns that into a
      structured conflict.
    - soft_delete_*: sets deleted_at (a tombstone), updated_at, and
      version + 1, optionally guarded by an expected version the same way.
      Tombstones keep their row (and, for tasks, their child rows), so
      history and deletions stay representable for synchronization.
Reads return live records only unless include_deleted=True is passed.

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
from app.planning.errors import DuplicateEntityError, InvalidEntityError
from app.planning.external_dependencies import ExecutionFact
from app.planning.models import FixedBlock, Project, ScheduledTask, Task
from app.planning.preferences import (
    PreferenceRecord,
    PreferenceScope,
    overrides_from_document,
    overrides_to_document,
)
from app.planning.provenance import GenerationRecord

# SQLite's historical default limit on bound variables is 999; stay well below.
_IN_CHUNK = 500

_LIVE = "deleted_at IS NULL"

_PROJECT_COLUMNS = ("id", "user_id", "name", "description", "created_at", "updated_at", "version", "deleted_at")

_TASK_COLUMNS = (
    "id", "user_id", "project_id", "name", "category",
    "estimated_duration_minutes", "priority", "required", "required_date",
    "preferred_window_start_minute", "preferred_window_end_minute",
    "deadline", "deadline_utc",
    "recurrence_frequency", "recurrence_interval", "recurrence_day_of_month",
    "recurrence_end_date", "recurrence_count",
    "created_at", "updated_at", "version", "deleted_at",
)

_FIXED_BLOCK_COLUMNS = (
    "id", "user_id", "label", "category", "planned_date", "timezone", "planned_start", "planned_end",
    "planned_start_utc", "planned_end_utc", "created_at", "updated_at", "version", "deleted_at",
)

_PLACEMENT_COLUMNS = (
    "id", "task_id", "user_id", "planned_date", "timezone", "planned_start", "planned_end",
    "planned_start_utc", "planned_end_utc", "score", "optimization_metadata",
    "created_at", "updated_at", "version", "deleted_at",
)

_PREFERENCE_COLUMNS = (
    "id", "user_id", "scope", "scope_date", "optimizer_mode", "overrides",
    "created_at", "updated_at", "version", "deleted_at",
)

_GENERATION_COLUMNS = (
    "id", "user_id", "planned_date", "timezone", "engine_mode", "range_start", "range_end", "range_scope",
    "allocation_id", "fingerprint", "fingerprint_version", "placements_digest", "placement_count",
    "unscheduled_count", "total_score", "generated_at", "created_at", "updated_at", "version", "deleted_at",
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


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _date_or_none(value: str | None) -> date_ | None:
    return date_.fromisoformat(value) if value is not None else None


def _datetime_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _live_clause(include_deleted: bool) -> str:
    return "1 = 1" if include_deleted else _LIVE


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
    # Generic write primitives
    # ------------------------------------------------------------------

    def _insert(self, table: str, columns: Sequence[str], row: tuple, kind: str, entity_id: object) -> None:
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({_placeholders(columns)})"
        try:
            self._connection.execute(sql, row)
        except sqlite3.IntegrityError as error:
            message = str(error)
            if f"{table}.id" in message:
                raise DuplicateEntityError(kind, entity_id) from error
            if EXECUTION_LINK_VIOLATION in message:
                raise InvalidEntityError(f"{kind} {entity_id}: {message}") from error
            raise

    def _compare_and_update(
        self, table: str, columns: Sequence[str], row: tuple, expected_version: int, kind: str, entity_id: object
    ) -> bool:
        assignments = ", ".join(f"{column} = ?" for column in columns if column != "id")
        values = [value for column, value in zip(columns, row) if column != "id"]
        try:
            cursor = self._connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ? AND version = ? AND {_LIVE}",
                (*values, str(entity_id), expected_version),
            )
        except sqlite3.IntegrityError as error:
            if EXECUTION_LINK_VIOLATION in str(error):
                raise InvalidEntityError(f"{kind} {entity_id}: {error}") from error
            raise
        return cursor.rowcount == 1

    def _soft_delete(
        self, table: str, entity_id: object, deleted_at: datetime, expected_version: int | None = None
    ) -> bool:
        stamp = deleted_at.astimezone(timezone.utc).isoformat()
        guard = "" if expected_version is None else " AND version = ?"
        params: tuple = (stamp, stamp, str(entity_id)) + (() if expected_version is None else (expected_version,))
        cursor = self._connection.execute(
            f"UPDATE {table} SET deleted_at = ?, updated_at = ?, version = version + 1 WHERE id = ? AND {_LIVE}{guard}",
            params,
        )
        return cursor.rowcount == 1

    def record_states(self, table: str, ids: Iterable[object]) -> dict[str, tuple[int, bool]]:
        """{id: (version, is_deleted)} for every stored row (live or tombstoned) among `ids`."""
        if table not in {"projects", "tasks", "fixed_blocks", "scheduled_tasks", "preference_overrides",
                         "schedule_generations"}:
            raise ValueError(f"unknown table {table!r}")
        found: dict[str, tuple[int, bool]] = {}
        with self._read():
            for chunk in _chunks(list(dict.fromkeys(str(value) for value in ids))):
                rows = self._connection.execute(
                    f"SELECT id, version, deleted_at FROM {table} WHERE id IN ({_placeholders(chunk)})", tuple(chunk)
                ).fetchall()
                found.update({row["id"]: (row["version"], row["deleted_at"] is not None) for row in rows})
        return found

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def insert_project(self, project: Project) -> None:
        with self.transaction():
            self._insert("projects", _PROJECT_COLUMNS, _project_to_row(project), "project", project.id)

    def update_project(self, project: Project, *, expected_version: int) -> bool:
        with self.transaction():
            return self._compare_and_update(
                "projects", _PROJECT_COLUMNS, _project_to_row(project), expected_version, "project", project.id
            )

    def soft_delete_project(self, project_id: uuid.UUID, *, deleted_at: datetime, expected_version: int | None) -> bool:
        with self.transaction():
            return self._soft_delete("projects", project_id, deleted_at, expected_version)

    def get_project(self, project_id: uuid.UUID, *, include_deleted: bool = False) -> Project | None:
        with self._read():
            row = self._connection.execute(
                f"SELECT * FROM projects WHERE id = ? AND {_live_clause(include_deleted)}", (str(project_id),)
            ).fetchone()
        return _row_to_project(row) if row is not None else None

    def list_projects(self, *, include_deleted: bool = False) -> list[Project]:
        with self._read():
            rows = self._connection.execute(
                f"SELECT * FROM projects WHERE {_live_clause(include_deleted)} ORDER BY created_at, id"
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def task_ids_for_project(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        """Live tasks that belong to the project."""
        with self._read():
            rows = self._connection.execute(
                f"SELECT id FROM tasks WHERE project_id = ? AND {_LIVE} ORDER BY id", (str(project_id),)
            ).fetchall()
        return [uuid.UUID(row["id"]) for row in rows]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def insert_task(self, task: Task) -> None:
        """Insert one new task and its child rows (atomic)."""
        with self.transaction():
            self._insert("tasks", _TASK_COLUMNS, _task_to_row(task), "task", task.id)
            self._write_task_children(task)

    def update_task(self, task: Task, *, expected_version: int) -> bool:
        """Compare-and-update one live task and rewrite its child rows (atomic). False if the precondition fails."""
        with self.transaction():
            if not self._compare_and_update("tasks", _TASK_COLUMNS, _task_to_row(task), expected_version, "task", task.id):
                return False
            self._write_task_children(task)
            return True

    def _write_task_children(self, task: Task) -> None:
        task_id = str(task.id)
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

    def soft_delete_task(self, task_id: uuid.UUID, *, deleted_at: datetime, expected_version: int | None) -> bool:
        """Tombstone one live task. Its child rows are kept with the tombstone (history)."""
        with self.transaction():
            return self._soft_delete("tasks", task_id, deleted_at, expected_version)

    def get_task(self, task_id: uuid.UUID, *, include_deleted: bool = False) -> Task | None:
        return self.get_tasks([task_id], include_deleted=include_deleted).get(task_id)

    def get_tasks(self, task_ids: Iterable[uuid.UUID], *, include_deleted: bool = False) -> dict[uuid.UUID, Task]:
        ids = _ids(task_ids)
        tasks: dict[uuid.UUID, Task] = {}
        with self._read():
            for chunk in _chunks(ids):
                for task in self._load_tasks(f"id IN ({_placeholders(chunk)})", tuple(chunk), include_deleted):
                    tasks[task.id] = task
        return tasks

    def list_tasks(self, *, include_deleted: bool = False) -> list[Task]:
        """Every task, ordered by (created_at, id) -- deterministic across reopen."""
        with self._read():
            return self._load_tasks("1 = 1", (), include_deleted)

    def list_tasks_eligible_for_range(self, start_date: date_, end_date: date_) -> list[Task]:
        """Live tasks whose hard date rules allow some date in [start_date, end_date]; ordered by (created_at, id)."""
        with self._read():
            return self._load_tasks(
                _ELIGIBLE_FOR_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat(), start_date.isoformat())
            )

    def list_tasks_planned_in_range(
        self, start_date: date_, end_date: date_, *, include_undated: bool = True, include_deleted: bool = False
    ) -> list[Task]:
        """
        Tasks whose planned date (required_date, else earliest preferred date)
        is in [start_date, end_date]; with include_undated, also undated tasks
        eligible for the range. Ordered by (created_at, id).
        """
        with self._read():
            if include_undated:
                return self._load_tasks(
                    _PLANNED_IN_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat(), start_date.isoformat()),
                    include_deleted,
                )
            return self._load_tasks(_DATED_IN_RANGE_WHERE, (start_date.isoformat(), end_date.isoformat()), include_deleted)

    def existing_task_ids(self, task_ids: Iterable[uuid.UUID], *, include_deleted: bool = False) -> set[uuid.UUID]:
        return self._existing_ids("tasks", task_ids, include_deleted)

    def existing_project_ids(self, project_ids: Iterable[uuid.UUID], *, include_deleted: bool = False) -> set[uuid.UUID]:
        return self._existing_ids("projects", project_ids, include_deleted)

    def dependents_of(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, set[uuid.UUID]]:
        """For each given task id that live tasks depend on: the set of live dependent task ids."""
        ids = _ids(task_ids)
        dependents: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        with self._read():
            for chunk in _chunks(ids):
                rows = self._connection.execute(
                    f"SELECT d.task_id, d.depends_on_task_id FROM task_dependencies AS d "
                    f"JOIN tasks AS t ON t.id = d.task_id "
                    f"WHERE t.deleted_at IS NULL AND d.depends_on_task_id IN ({_placeholders(chunk)})",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    dependents[uuid.UUID(row["depends_on_task_id"])].add(uuid.UUID(row["task_id"]))
        return dict(dependents)

    def _load_tasks(self, where_sql: str, params: tuple, include_deleted: bool = False) -> list[Task]:
        # Caller holds the lock. where_sql is always an internal constant.
        where = f"{_live_clause(include_deleted)} AND ({where_sql})"
        rows = self._connection.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY created_at, id", params).fetchall()
        if not rows:
            return []

        subquery = f"SELECT id FROM tasks WHERE {where}"
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

    def insert_fixed_block(self, block: FixedBlock) -> None:
        with self.transaction():
            self._insert("fixed_blocks", _FIXED_BLOCK_COLUMNS, _fixed_block_to_row(block), "fixed block", block.id)

    def update_fixed_block(self, block: FixedBlock, *, expected_version: int) -> bool:
        with self.transaction():
            return self._compare_and_update(
                "fixed_blocks", _FIXED_BLOCK_COLUMNS, _fixed_block_to_row(block), expected_version, "fixed block", block.id
            )

    def soft_delete_fixed_block(self, block_id: uuid.UUID, *, deleted_at: datetime, expected_version: int | None) -> bool:
        with self.transaction():
            return self._soft_delete("fixed_blocks", block_id, deleted_at, expected_version)

    def get_fixed_blocks(
        self, block_ids: Iterable[uuid.UUID], *, include_deleted: bool = False
    ) -> dict[uuid.UUID, FixedBlock]:
        blocks: dict[uuid.UUID, FixedBlock] = {}
        with self._read():
            for chunk in _chunks(_ids(block_ids)):
                rows = self._connection.execute(
                    f"SELECT * FROM fixed_blocks WHERE id IN ({_placeholders(chunk)}) AND {_live_clause(include_deleted)}",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    block = _row_to_fixed_block(row)
                    blocks[block.id] = block
        return blocks

    def list_fixed_blocks(self, start_date: date_, end_date: date_, *, include_deleted: bool = False) -> list[FixedBlock]:
        """Blocks with planned_date in [start_date, end_date], ordered by (planned_date, start, id)."""
        with self._read():
            rows = self._connection.execute(
                f"SELECT * FROM fixed_blocks WHERE planned_date BETWEEN ? AND ? AND {_live_clause(include_deleted)} "
                "ORDER BY planned_date, planned_start_utc, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [_row_to_fixed_block(row) for row in rows]

    # ------------------------------------------------------------------
    # Placements (ScheduledTask)
    # ------------------------------------------------------------------

    def insert_placement(self, placement: ScheduledTask) -> None:
        with self.transaction():
            self._insert("scheduled_tasks", _PLACEMENT_COLUMNS, _placement_to_row(placement), "placement", placement.id)

    def update_placement(self, placement: ScheduledTask, *, expected_version: int) -> bool:
        with self.transaction():
            return self._compare_and_update(
                "scheduled_tasks", _PLACEMENT_COLUMNS, _placement_to_row(placement), expected_version,
                "placement", placement.id,
            )

    def soft_delete_placement(
        self, placement_id: uuid.UUID, *, deleted_at: datetime, expected_version: int | None
    ) -> bool:
        with self.transaction():
            return self._soft_delete("scheduled_tasks", placement_id, deleted_at, expected_version)

    def soft_delete_placements(self, placement_ids: Iterable[uuid.UUID], *, deleted_at: datetime) -> int:
        """Tombstone live placements (derived output; no per-row precondition). Execution rows are untouched."""
        deleted = 0
        with self.transaction():
            for placement_id in _ids(placement_ids):
                deleted += int(self._soft_delete("scheduled_tasks", placement_id, deleted_at))
        return deleted

    def get_placements(
        self, placement_ids: Iterable[uuid.UUID], *, include_deleted: bool = False
    ) -> dict[uuid.UUID, ScheduledTask]:
        placements: dict[uuid.UUID, ScheduledTask] = {}
        with self._read():
            for chunk in _chunks(_ids(placement_ids)):
                rows = self._connection.execute(
                    f"SELECT * FROM scheduled_tasks WHERE id IN ({_placeholders(chunk)}) "
                    f"AND {_live_clause(include_deleted)}",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    placement = _row_to_placement(row)
                    placements[placement.id] = placement
        return placements

    def list_placements(
        self, start_date: date_, end_date: date_, *, include_deleted: bool = False
    ) -> list[ScheduledTask]:
        """Placements with planned_date in [start_date, end_date], ordered by (planned_date, start, id)."""
        with self._read():
            rows = self._connection.execute(
                f"SELECT * FROM scheduled_tasks WHERE planned_date BETWEEN ? AND ? AND {_live_clause(include_deleted)} "
                "ORDER BY planned_date, planned_start_utc, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [_row_to_placement(row) for row in rows]

    def active_placements_for_tasks(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[ScheduledTask]]:
        """Live placements of the given tasks, on any date, grouped by task; each list ordered by (date, start, id)."""
        grouped: dict[uuid.UUID, list[ScheduledTask]] = defaultdict(list)
        with self._read():
            for chunk in _chunks(_ids(task_ids)):
                rows = self._connection.execute(
                    f"SELECT * FROM scheduled_tasks WHERE task_id IN ({_placeholders(chunk)}) AND {_LIVE} "
                    "ORDER BY planned_date, planned_start_utc, id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    placement = _row_to_placement(row)
                    grouped[placement.task_id].append(placement)
        return dict(grouped)

    def placement_ids_with_history(self, placement_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """The subset of placement ids that some execution row references."""
        return set(self.placement_execution_statuses(placement_ids))

    def placement_execution_statuses(self, placement_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        """{placement id: status} for the placements that execution history references."""
        found: dict[uuid.UUID, str] = {}
        with self._read():
            for chunk in _chunks(_ids(placement_ids)):
                rows = self._connection.execute(
                    f"SELECT scheduled_task_id, status FROM executions "
                    f"WHERE scheduled_task_id IN ({_placeholders(chunk)})",
                    tuple(chunk),
                ).fetchall()
                found.update({uuid.UUID(row["scheduled_task_id"]): row["status"] for row in rows})
        return found

    def execution_facts_for_tasks(self, task_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, list[ExecutionFact]]:
        """Live executions of the given tasks (read-only; used to resolve external dependencies)."""
        grouped: dict[uuid.UUID, list[ExecutionFact]] = defaultdict(list)
        with self._read():
            for chunk in _chunks(_ids(task_ids)):
                rows = self._connection.execute(
                    f"SELECT task_id, scheduled_task_id, status, actual_final_end_at, updated_at FROM executions "
                    f"WHERE task_id IN ({_placeholders(chunk)}) AND {_LIVE} ORDER BY updated_at, id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    task_id = uuid.UUID(row["task_id"])
                    grouped[task_id].append(
                        ExecutionFact(
                            task_id=task_id,
                            scheduled_task_id=_uuid_or_none(row["scheduled_task_id"]),
                            status=row["status"],
                            finished_at=_datetime_or_none(row["actual_final_end_at"]),
                            updated_at=row["updated_at"],
                        )
                    )
        return dict(grouped)

    # ------------------------------------------------------------------
    # Preference layers
    # ------------------------------------------------------------------

    def insert_preference(self, record: PreferenceRecord) -> None:
        with self.transaction():
            self._insert("preference_overrides", _PREFERENCE_COLUMNS, _preference_to_row(record), "preference", record.id)

    def update_preference(self, record: PreferenceRecord, *, expected_version: int) -> bool:
        with self.transaction():
            return self._compare_and_update(
                "preference_overrides", _PREFERENCE_COLUMNS, _preference_to_row(record), expected_version,
                "preference", record.id,
            )

    def soft_delete_preference(self, record_id: uuid.UUID, *, deleted_at: datetime, expected_version: int | None) -> bool:
        with self.transaction():
            return self._soft_delete("preference_overrides", record_id, deleted_at, expected_version)

    def get_preference(self, scope: PreferenceScope, day: date_ | None = None) -> PreferenceRecord | None:
        """
        The live user-level record (day=None) or the live record for one date.
        Preferences are device-wide: a layer owned by a signed-in account
        (app/sync) is preferred over an ownerless one for the same scope.
        """
        with self._read():
            row = self._connection.execute(
                f"SELECT * FROM preference_overrides WHERE scope = ? AND scope_date IS ? AND {_LIVE} "
                "ORDER BY user_id IS NULL, created_at, id LIMIT 1",
                (scope.value, _iso(day)),
            ).fetchone()
        return _row_to_preference(row) if row is not None else None

    def list_date_preferences(self, start_date: date_, end_date: date_) -> list[PreferenceRecord]:
        """One live layer per date in the range (owned before ownerless, as in get_preference)."""
        with self._read():
            rows = self._connection.execute(
                f"SELECT * FROM preference_overrides WHERE scope = 'date' AND scope_date BETWEEN ? AND ? "
                f"AND {_LIVE} ORDER BY scope_date, user_id IS NULL, created_at, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        by_date: dict[str, PreferenceRecord] = {}
        for row in rows:
            by_date.setdefault(row["scope_date"], _row_to_preference(row))
        return list(by_date.values())

    def get_preference_by_id(self, record_id: uuid.UUID, *, include_deleted: bool = False) -> PreferenceRecord | None:
        with self._read():
            row = self._connection.execute(
                f"SELECT * FROM preference_overrides WHERE id = ? AND {_live_clause(include_deleted)}", (str(record_id),)
            ).fetchone()
        return _row_to_preference(row) if row is not None else None

    # ------------------------------------------------------------------
    # Schedule provenance
    # ------------------------------------------------------------------

    def insert_generation(self, record: GenerationRecord) -> None:
        with self.transaction():
            self._insert("schedule_generations", _GENERATION_COLUMNS, _generation_to_row(record), "generation", record.id)

    def update_generation(self, record: GenerationRecord, *, expected_version: int) -> bool:
        with self.transaction():
            return self._compare_and_update(
                "schedule_generations", _GENERATION_COLUMNS, _generation_to_row(record), expected_version,
                "generation", record.id,
            )

    def soft_delete_generations(self, start_date: date_, end_date: date_, *, deleted_at: datetime) -> int:
        with self.transaction():
            ids = [record.id for record in self.list_generations(start_date, end_date)]
            return sum(int(self._soft_delete("schedule_generations", record_id, deleted_at)) for record_id in ids)

    def get_generation_by_id(self, record_id: uuid.UUID, *, include_deleted: bool = False) -> GenerationRecord | None:
        with self._read():
            row = self._connection.execute(
                f"SELECT * FROM schedule_generations WHERE id = ? AND {_live_clause(include_deleted)}", (str(record_id),)
            ).fetchone()
        return _row_to_generation(row) if row is not None else None

    def list_generations(self, start_date: date_, end_date: date_) -> list[GenerationRecord]:
        """Live generation records dated in [start_date, end_date], by date."""
        with self._read():
            rows = self._connection.execute(
                f"SELECT * FROM schedule_generations WHERE planned_date BETWEEN ? AND ? AND {_LIVE} "
                "ORDER BY planned_date, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [_row_to_generation(row) for row in rows]

    # ------------------------------------------------------------------
    # Synchronization (app/sync): storing server-validated records
    # ------------------------------------------------------------------

    def store_synced(self, entity_type: str, model) -> None:
        """
        Insert or overwrite one record exactly as given (tombstones included).
        Only app/sync uses this, to apply a record the server already accepted,
        and only when the device has no pending change of that record -- it is
        not a way around the service's preconditions for local edits.
        """
        table, columns, to_row = {
            "project": ("projects", _PROJECT_COLUMNS, _project_to_row),
            "task": ("tasks", _TASK_COLUMNS, _task_to_row),
            "fixed_block": ("fixed_blocks", _FIXED_BLOCK_COLUMNS, _fixed_block_to_row),
            "placement": ("scheduled_tasks", _PLACEMENT_COLUMNS, _placement_to_row),
            "preference": ("preference_overrides", _PREFERENCE_COLUMNS, _preference_to_row),
            "schedule_generation": ("schedule_generations", _GENERATION_COLUMNS, _generation_to_row),
        }[entity_type]
        assignments = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "id")
        with self.transaction():
            self._connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({_placeholders(columns)}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}",
                to_row(model),
            )
            if entity_type == "task":
                self._write_task_children(model)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _existing_ids(self, table: str, ids: Iterable[uuid.UUID], include_deleted: bool) -> set[uuid.UUID]:
        found: set[uuid.UUID] = set()
        with self._read():
            for chunk in _chunks(_ids(ids)):
                rows = self._connection.execute(
                    f"SELECT id FROM {table} WHERE id IN ({_placeholders(chunk)}) AND {_live_clause(include_deleted)}",
                    tuple(chunk),
                ).fetchall()
                found.update(uuid.UUID(row["id"]) for row in rows)
        return found


# -----------------------------------------------------------------------------
# Row mapping
# -----------------------------------------------------------------------------


def _project_to_row(project: Project) -> tuple:
    return (
        str(project.id), _str_or_none(project.user_id), project.name, project.description,
        project.created_at.isoformat(), project.updated_at.isoformat(), project.version, _iso(project.deleted_at),
    )


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project.model_validate(dict(row))


def _task_to_row(task: Task) -> tuple:
    window = task.preferred_time_window
    recurrence = task.recurrence
    return (
        str(task.id),
        _str_or_none(task.user_id),
        _str_or_none(task.project_id),
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
        _iso(task.deleted_at),
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
            "deleted_at": _datetime_or_none(row["deleted_at"]),
        }
    )


def _fixed_block_to_row(block: FixedBlock) -> tuple:
    return (
        str(block.id), _str_or_none(block.user_id), block.label, block.category, block.planned_date.isoformat(),
        block.timezone, block.planned_start.isoformat(), block.planned_end.isoformat(),
        _utc_text(block.planned_start), _utc_text(block.planned_end),
        block.created_at.isoformat(), block.updated_at.isoformat(), block.version, _iso(block.deleted_at),
    )


def _row_to_fixed_block(row: sqlite3.Row) -> FixedBlock:
    return FixedBlock.model_validate(
        {
            "id": uuid.UUID(row["id"]),
            "user_id": _uuid_or_none(row["user_id"]),
            "label": row["label"],
            "category": row["category"],
            "planned_date": date_.fromisoformat(row["planned_date"]),
            "timezone": row["timezone"],
            "planned_start": datetime.fromisoformat(row["planned_start"]),
            "planned_end": datetime.fromisoformat(row["planned_end"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "version": row["version"],
            "deleted_at": _datetime_or_none(row["deleted_at"]),
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
        str(placement.id), str(placement.task_id), _str_or_none(placement.user_id), placement.planned_date.isoformat(),
        placement.timezone, placement.planned_start.isoformat(), placement.planned_end.isoformat(),
        _utc_text(placement.planned_start), _utc_text(placement.planned_end),
        placement.score, metadata,
        placement.created_at.isoformat(), placement.updated_at.isoformat(), placement.version,
        _iso(placement.deleted_at),
    )


def _row_to_placement(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask.model_validate(
        {
            "id": uuid.UUID(row["id"]),
            "task_id": uuid.UUID(row["task_id"]),
            "user_id": _uuid_or_none(row["user_id"]),
            "planned_date": date_.fromisoformat(row["planned_date"]),
            "timezone": row["timezone"],
            "planned_start": datetime.fromisoformat(row["planned_start"]),
            "planned_end": datetime.fromisoformat(row["planned_end"]),
            "score": row["score"],
            "optimization_metadata": json.loads(row["optimization_metadata"]),
            "created_at": datetime.fromisoformat(row["created_at"]),
            "updated_at": datetime.fromisoformat(row["updated_at"]),
            "version": row["version"],
            "deleted_at": _datetime_or_none(row["deleted_at"]),
        }
    )


def _preference_to_row(record: PreferenceRecord) -> tuple:
    mode = record.overrides.optimizer_mode
    return (
        str(record.id), _str_or_none(record.user_id), record.scope.value, _iso(record.date),
        mode.value if mode is not None else None, overrides_to_document(record.overrides),
        record.created_at.isoformat(), record.updated_at.isoformat(), record.version, _iso(record.deleted_at),
    )


def _row_to_preference(row: sqlite3.Row) -> PreferenceRecord:
    return PreferenceRecord(
        id=uuid.UUID(row["id"]),
        user_id=_uuid_or_none(row["user_id"]),
        scope=PreferenceScope(row["scope"]),
        date=_date_or_none(row["scope_date"]),
        overrides=overrides_from_document(row["overrides"], row["optimizer_mode"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        version=row["version"],
        deleted_at=_datetime_or_none(row["deleted_at"]),
    )


def _generation_to_row(record: GenerationRecord) -> tuple:
    return (
        str(record.id), _str_or_none(record.user_id), record.planned_date.isoformat(), record.timezone,
        record.engine_mode.value, record.range_start.isoformat(), record.range_end.isoformat(), record.range_scope,
        str(record.allocation_id), record.fingerprint, record.fingerprint_version, record.placements_digest,
        record.placement_count, record.unscheduled_count, record.total_score, record.generated_at.isoformat(),
        record.created_at.isoformat(), record.updated_at.isoformat(), record.version, _iso(record.deleted_at),
    )


def _row_to_generation(row: sqlite3.Row) -> GenerationRecord:
    return GenerationRecord.model_validate(dict(row))
