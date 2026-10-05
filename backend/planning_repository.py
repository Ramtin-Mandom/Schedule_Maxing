"""
backend/planning_repository.py

The server's adapter for the shared planning application layer: it
implements the storage interface app.planning.application.PlanningService
uses (the one app/planning/repository.py implements for SQLite) over the
server's SQLAlchemy tables, for exactly one authenticated user. With it the
server runs the *same* PlanningService and app/planning/workflow.py as the
desktop -- allocation, generation, occurrence cleanup, provenance, reset,
the canonical CSV merge rules and the fixed-block invariants -- instead of
a second copy of any of them.

Rules of this adapter:

    - Owner: always OwnerScope.account(user_id). Every query filters by the
      user; a record of another user behaves exactly like a missing one,
      and writing a record whose user_id is not the user's is a ScopeError.
    - Transactions: the outermost transaction() is one backend.mutations
      mutation -- the user's change-log lock is taken first and held until
      commit, so a read-check-write inside it (e.g. the generation's
      fingerprint re-check) cannot interleave with another write of the user.
      Nested transactions are savepoints. A repository built with an open
      Mutator (a sync push applying a reschedule) joins that mutation:
      every transaction() is then a savepoint and the caller commits.
    - Every write goes through the Mutator's change log: one change_log
      entry per written record, in commit order, exactly like the REST and
      sync endpoints.
    - The server owns audit fields and versions: an insert stores version 1
      and the server clock's created_at/updated_at (whatever the model says);
      an update stores stored version + 1 and the server clock. Callers read
      the stored record back (PlanningService always does).
    - Content is validated with the same resource schemas the REST API uses
      (backend/resources.py); a violation is an InvalidEntityError.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import date as date_
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.planning.application import task_planned_date
from app.planning.errors import DuplicateEntityError, InvalidEntityError, ScopeError
from app.planning.external_dependencies import ExecutionFact
from app.planning.history import (
    CompletionHistory,
    ExecutionHistory,
    HistoryBounds,
    ScheduleHistory,
    collect_completion_history,
    collect_schedule_history,
)
from app.execution.models import CancelReason, ExecutionStatus
from app.planning.models import (
    PLACEMENT_SNAPSHOT_FIELDS,
    FixedBlock,
    OccurrenceState,
    PlacementRemovalReason,
    Project,
    ScheduledTask,
    Task,
    TaskType,
)
from app.planning.preferences import PreferenceRecord, PreferenceScope
from app.planning.provenance import GenerationRecord
from app.planning.scope import OwnerScope
from app.planning.time import local_day_start_utc
from backend import models
from backend.executions import ActionIn, to_task_execution, to_work_session
from backend.mutations import Mutator, mutation
from backend.record_mapping import placement_snapshot, preference_overrides, task_content
from backend.resources import (
    FIXED_BLOCKS,
    GENERATIONS,
    PLACEMENTS,
    PREFERENCES,
    PROJECTS,
    TASK_TYPES,
    TASKS,
    FixedBlockCreate,
    GenerationCreate,
    PlacementCreate,
    PreferenceCreate,
    ProjectCreate,
    ResourceSpec,
    TaskCreate,
    TaskTypeCreate,
)

_TABLES = {
    "projects": models.Project, "tasks": models.Task, "fixed_blocks": models.FixedBlock,
    "scheduled_tasks": models.Placement, "preference_overrides": models.Preference,
    "schedule_generations": models.ScheduleGeneration, "task_types": models.TaskType,
}


def _payload(schema, data: dict):
    try:
        return schema.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0] if error.errors() else {}
        raise InvalidEntityError(f"{first.get('msg', error)} ({'.'.join(map(str, first.get('loc', ())))})") from None


class ServerPlanningRepository:
    def __init__(
        self, session: Session, user_id: uuid.UUID, clock: Callable[[], datetime], *, mutator: Mutator | None = None
    ) -> None:
        if mutator is not None and mutator.user_id != user_id:
            raise ScopeError("a server repository joins only a mutation of its own user.")
        self._session = session
        self._user_id = user_id
        self._clock = clock
        self._owner = OwnerScope.account(user_id)
        self._mutator: Mutator | None = mutator

    # ------------------------------------------------------------------
    # Scope and transactions
    # ------------------------------------------------------------------

    @property
    def owner(self) -> OwnerScope:
        return self._owner

    def scoped(self, owner: OwnerScope) -> "ServerPlanningRepository":
        if owner != self._owner:
            raise ScopeError("a server repository is bound to the authenticated user and cannot change scope.")
        return self

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._mutator is not None:
            with self._session.begin_nested():
                yield
            return
        with mutation(self._session, self._user_id, self._clock) as mutator:
            self._mutator = mutator
            try:
                yield
            finally:
                self._mutator = None

    def _require_owner(self, kind: str, entity_id, user_id) -> None:
        if user_id != self._user_id:
            raise ScopeError(f"{kind} {entity_id} belongs to another owner; it cannot be written here.")

    def _rows(self, model, *conditions, include_deleted: bool = False) -> list:
        query = select(model).where(model.user_id == self._user_id, *conditions)
        if not include_deleted:
            query = query.where(model.deleted_at.is_(None))
        return list(self._session.scalars(query))

    def _by_ids(self, model, ids: Iterable, include_deleted: bool = False) -> list:
        ids = list(dict.fromkeys(uuid.UUID(str(value)) for value in ids))
        if not ids:
            return []
        return self._rows(model, model.id.in_(ids), include_deleted=include_deleted)

    # ------------------------------------------------------------------
    # Generic writes (all through the mutator's change log)
    # ------------------------------------------------------------------

    def _insert(self, spec: ResourceSpec, model, payload) -> None:
        self._require_owner(spec.label, model.id, model.user_id)
        with self.transaction():
            if self._session.get(spec.model, (self._user_id, model.id)) is not None:
                raise DuplicateEntityError(spec.label, model.id)
            now = self._mutator.now
            row = spec.model(user_id=self._user_id, id=model.id, created_at=now, updated_at=now, version=1)
            self._session.add(row)
            spec.assign(self._session, self._user_id, row, payload)
            deleted = getattr(model, "deleted_at", None) is not None
            if deleted:
                row.deleted_at = now
            self._mutator._flush(spec, row)
            self._mutator.log(spec.entity_type, spec.serialize(self._session, self._user_id, row),
                              "delete" if deleted else "upsert")

    def _update(self, spec: ResourceSpec, model, payload, expected_version: int) -> bool:
        self._require_owner(spec.label, model.id, model.user_id)
        with self.transaction():
            row = self._session.get(spec.model, (self._user_id, model.id), populate_existing=True)
            if row is None or row.deleted_at is not None or row.version != expected_version:
                return False
            if getattr(model, "deleted_at", None) is not None:
                if spec is PLACEMENTS:
                    row.removal_reason = model.removal_reason.value if model.removal_reason is not None else None
                    row.superseded_by_id = model.superseded_by_id
                self._mutator.tombstone(spec, row)
                return True
            spec.assign(self._session, self._user_id, row, payload)
            row.updated_at, row.version = self._mutator.now, row.version + 1
            self._mutator._flush(spec, row)
            self._mutator.log(spec.entity_type, spec.serialize(self._session, self._user_id, row), "upsert")
            return True

    def _soft_delete(
        self, spec: ResourceSpec, entity_id, expected_version: int | None,
        removal: tuple[PlacementRemovalReason | None, uuid.UUID | None] | None = None,
        occurrence_state: OccurrenceState | None = None,
    ) -> bool:
        with self.transaction():
            row = self._session.get(spec.model, (self._user_id, uuid.UUID(str(entity_id))), populate_existing=True)
            if row is None or row.deleted_at is not None:
                return False
            if expected_version is not None and row.version != expected_version:
                return False
            if removal is not None:  # a placement's removal provenance, part of the same revision
                reason, successor = removal
                row.removal_reason = reason.value if reason is not None else None
                row.superseded_by_id = successor
            if occurrence_state is not None:  # why an occurrence was removed, part of the same revision
                row.occurrence_state = occurrence_state.value
            self._mutator.tombstone(spec, row)
            return True

    def record_states(self, table: str, ids: Iterable[object]) -> dict[str, tuple[int, bool]]:
        rows = self._by_ids(_TABLES[table], ids, include_deleted=True)
        return {str(row.id): (row.version, row.deleted_at is not None) for row in rows}

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def insert_project(self, project: Project) -> None:
        self._insert(PROJECTS, project, _payload(ProjectCreate, {"name": project.name, "description": project.description}))

    def update_project(self, project: Project, *, expected_version: int) -> bool:
        payload = _payload(ProjectCreate, {"name": project.name, "description": project.description})
        return self._update(PROJECTS, project, payload, expected_version)

    def soft_delete_project(self, project_id, *, deleted_at, expected_version) -> bool:
        return self._soft_delete(PROJECTS, project_id, expected_version)

    def get_project(self, project_id, *, include_deleted: bool = False) -> Project | None:
        rows = self._by_ids(models.Project, [project_id], include_deleted)
        return _project(rows[0]) if rows else None

    def list_projects(self, *, include_deleted: bool = False) -> list[Project]:
        rows = self._rows(models.Project, include_deleted=include_deleted)
        return [_project(row) for row in sorted(rows, key=lambda row: (row.created_at, str(row.id)))]

    def task_ids_for_project(self, project_id) -> list[uuid.UUID]:
        return sorted((row.id for row in self._rows(models.Task, models.Task.project_id == project_id)), key=str)

    # ------------------------------------------------------------------
    # Task types
    # ------------------------------------------------------------------

    def insert_task_type(self, task_type: TaskType) -> None:
        self._insert(TASK_TYPES, task_type, _payload(TaskTypeCreate, {"label": task_type.label}))

    def update_task_type(self, task_type: TaskType, *, expected_version: int) -> bool:
        return self._update(TASK_TYPES, task_type, _payload(TaskTypeCreate, {"label": task_type.label}), expected_version)

    def get_task_types(self, type_ids: Iterable, *, include_deleted: bool = False) -> dict[uuid.UUID, TaskType]:
        return {row.id: _task_type(row) for row in self._by_ids(models.TaskType, type_ids, include_deleted)}

    def list_task_types(self, *, include_deleted: bool = False) -> list[TaskType]:
        rows = self._rows(models.TaskType, include_deleted=include_deleted)
        return [_task_type(row) for row in sorted(rows, key=lambda row: (row.created_at, str(row.id)))]

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def insert_task(self, task: Task) -> None:
        self._insert(TASKS, task, _task_payload(task))

    def update_task(self, task: Task, *, expected_version: int) -> bool:
        return self._update(TASKS, task, _task_payload(task), expected_version)

    def soft_delete_task(self, task_id, *, deleted_at, expected_version, occurrence_state=None) -> bool:
        return self._soft_delete(TASKS, task_id, expected_version, occurrence_state=occurrence_state)

    def occurrences_of_series(self, series_ids: Iterable, *, include_deleted: bool = True) -> dict[uuid.UUID, list[Task]]:
        ids = [uuid.UUID(str(value)) for value in series_ids]
        grouped: dict[uuid.UUID, list[Task]] = defaultdict(list)
        if ids:
            for task in self._tasks(self._rows(models.Task, models.Task.series_id.in_(ids),
                                               include_deleted=include_deleted)):
                grouped[task.series_id].append(task)
        return {key: sorted(tasks, key=lambda task: task.occurrence_slot) for key, tasks in grouped.items()}

    def occurrences_in_slot_range(self, series_ids: Iterable, first: date_, last: date_) -> dict[uuid.UUID, list[Task]]:
        ids = [uuid.UUID(str(value)) for value in series_ids]
        grouped: dict[uuid.UUID, list[Task]] = defaultdict(list)
        if ids:
            for task in self._tasks(self._rows(models.Task, models.Task.series_id.in_(ids),
                                               models.Task.occurrence_slot.between(first, last), include_deleted=True)):
                grouped[task.series_id].append(task)
        return dict(grouped)

    def list_series(self, *, include_deleted: bool = False) -> list[Task]:
        return self._tasks(self._rows(models.Task, models.Task.recurrence_frequency.is_not(None),
                                      include_deleted=include_deleted))

    def series_successors(self, series_ids: Iterable) -> dict[uuid.UUID, list[Task]]:
        ids = [uuid.UUID(str(value)) for value in series_ids]
        grouped: dict[uuid.UUID, list[Task]] = defaultdict(list)
        if ids:
            for task in self._tasks(self._rows(models.Task, models.Task.series_predecessor_id.in_(ids),
                                               include_deleted=True)):
                grouped[task.series_predecessor_id].append(task)
        return dict(grouped)

    def get_task(self, task_id, *, include_deleted: bool = False) -> Task | None:
        return self.get_tasks([task_id], include_deleted=include_deleted).get(task_id)

    def get_tasks(self, task_ids: Iterable, *, include_deleted: bool = False) -> dict[uuid.UUID, Task]:
        return {task.id: task for task in self._tasks(self._by_ids(models.Task, task_ids, include_deleted))}

    def list_tasks(self, *, include_deleted: bool = False) -> list[Task]:
        return self._tasks(self._rows(models.Task, include_deleted=include_deleted))

    def list_tasks_eligible_for_range(self, start_date: date_, end_date: date_, *, timezone_name: str = "UTC") -> list[Task]:
        boundary = local_day_start_utc(start_date, timezone_name)
        rows = self._rows(models.Task, or_(
            models.Task.required_date.between(start_date, end_date),
            models.Task.required_date.is_(None) & (models.Task.deadline_utc.is_(None) | (models.Task.deadline_utc >= boundary)),
        ))
        return self._tasks(rows)

    def list_tasks_planned_in_range(
        self, start_date: date_, end_date: date_, *, include_undated: bool = True, include_deleted: bool = False,
        timezone_name: str = "UTC",
    ) -> list[Task]:
        # The planned date (required_date, else the earliest preferred date) is judged exactly as the SQLite query does.
        boundary = local_day_start_utc(start_date, timezone_name)
        rows = self._rows(models.Task, or_(models.Task.required_date.is_(None),
                                           models.Task.required_date.between(start_date, end_date)),
                          include_deleted=include_deleted)
        result = []
        for task in self._tasks(rows):
            planned = task_planned_date(task)
            if planned is not None:
                if start_date <= planned <= end_date:
                    result.append(task)
            elif include_undated and (task.deadline is None or task.deadline >= boundary):
                result.append(task)
        return result

    def existing_task_ids(self, task_ids: Iterable, *, include_deleted: bool = False) -> set[uuid.UUID]:
        return {row.id for row in self._by_ids(models.Task, task_ids, include_deleted)}

    def existing_project_ids(self, project_ids: Iterable, *, include_deleted: bool = False) -> set[uuid.UUID]:
        return {row.id for row in self._by_ids(models.Project, project_ids, include_deleted)}

    def dependents_of(self, task_ids: Iterable) -> dict[uuid.UUID, set[uuid.UUID]]:
        ids = [uuid.UUID(str(value)) for value in task_ids]
        if not ids:
            return {}
        rows = self._session.execute(
            select(models.TaskDependency.task_id, models.TaskDependency.depends_on_id)
            .join(models.Task, (models.Task.user_id == models.TaskDependency.user_id)
                  & (models.Task.id == models.TaskDependency.task_id))
            .where(models.TaskDependency.user_id == self._user_id, models.TaskDependency.depends_on_id.in_(ids),
                   models.Task.deleted_at.is_(None))
        )
        dependents: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for task_id, depends_on in rows:
            dependents[depends_on].add(task_id)
        return dict(dependents)

    def _tasks(self, rows: list) -> list[Task]:
        # The child rows (tags, dates, dependencies, weekdays) were loaded with the rows, one query per kind.
        return sorted((_task(row) for row in rows), key=lambda task: (task.created_at, str(task.id)))

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def insert_fixed_block(self, block: FixedBlock) -> None:
        self._insert(FIXED_BLOCKS, block, _payload(FixedBlockCreate, _block_fields(block)))

    def update_fixed_block(self, block: FixedBlock, *, expected_version: int) -> bool:
        return self._update(FIXED_BLOCKS, block, _payload(FixedBlockCreate, _block_fields(block)), expected_version)

    def soft_delete_fixed_block(self, block_id, *, deleted_at, expected_version) -> bool:
        return self._soft_delete(FIXED_BLOCKS, block_id, expected_version)

    def get_fixed_blocks(self, block_ids: Iterable, *, include_deleted: bool = False) -> dict[uuid.UUID, FixedBlock]:
        return {row.id: _block(row) for row in self._by_ids(models.FixedBlock, block_ids, include_deleted)}

    def list_fixed_blocks(self, start_date: date_, end_date: date_, *, include_deleted: bool = False) -> list[FixedBlock]:
        rows = self._rows(models.FixedBlock, models.FixedBlock.planned_date.between(start_date, end_date),
                          include_deleted=include_deleted)
        return sorted((_block(row) for row in rows), key=lambda b: (b.planned_date, b.planned_start, str(b.id)))

    # ------------------------------------------------------------------
    # Placements
    # ------------------------------------------------------------------

    def insert_placement(self, placement: ScheduledTask) -> None:
        self._insert(PLACEMENTS, placement, _payload(PlacementCreate, _placement_fields(placement)))

    def update_placement(self, placement: ScheduledTask, *, expected_version: int) -> bool:
        return self._update(PLACEMENTS, placement, _payload(PlacementCreate, _placement_fields(placement)), expected_version)

    def soft_delete_placement(self, placement_id, *, deleted_at, expected_version, removal_reason=None,
                              superseded_by_id=None) -> bool:
        return self._soft_delete(PLACEMENTS, placement_id, expected_version, (removal_reason, superseded_by_id))

    def soft_delete_placements(self, placement_ids: Iterable, *, deleted_at, removal_reason=None,
                               superseded_by=None) -> int:
        superseded_by = superseded_by or {}
        with self.transaction():
            return sum(
                int(self._soft_delete(PLACEMENTS, placement_id, None,
                                      (removal_reason, superseded_by.get(uuid.UUID(str(placement_id))))))
                for placement_id in dict.fromkeys(placement_ids)
            )

    def placements_superseded_by(self, placement_ids: Iterable) -> dict[uuid.UUID, list[ScheduledTask]]:
        ids = [uuid.UUID(str(value)) for value in placement_ids]
        grouped: dict[uuid.UUID, list[ScheduledTask]] = defaultdict(list)
        if ids:
            rows = self._rows(models.Placement, models.Placement.superseded_by_id.in_(ids), include_deleted=True)
            for row in sorted(rows, key=lambda row: str(row.id)):
                grouped[row.superseded_by_id].append(_placement(row))
        return dict(grouped)

    def schedule_history(self, start_utc: datetime, end_utc: datetime) -> ScheduleHistory:
        """
        The analytics read model (app/planning/history.py) of the user's records.
        On PostgreSQL the user's change-log row is read FOR SHARE first: every
        writer of the user takes that row exclusively (backend/mutations.py), so
        until this unit of work ends nothing of the user can commit and the
        queries below see one consistent state. Writes nothing.
        """
        self._session.execute(
            select(models.User.id).where(models.User.id == self._user_id).with_for_update(read=True)
        )
        return collect_schedule_history(
            start_utc, end_utc, in_range=self._placements_starting_between,
            superseded_by=self.placements_superseded_by, executions_for=self._executions_for_placements,
            tasks_for=lambda ids: self.get_tasks(ids, include_deleted=True),
            types_for=lambda ids: self.get_task_types(ids, include_deleted=True),
        )

    def _completed(self):
        return (models.Execution.user_id == self._user_id, models.Execution.deleted_at.is_(None),
                models.Execution.status == ExecutionStatus.COMPLETED.value)

    def completion_history(self, start_utc: datetime, end_utc: datetime) -> CompletionHistory:
        """The completion activity of [start_utc, end_utc) (see the SQLite repository); one consistent read."""
        self._session.execute(
            select(models.User.id).where(models.User.id == self._user_id).with_for_update(read=True)
        )
        unknown = self._session.scalar(
            select(func.count()).select_from(models.Execution).where(
                *self._completed(), models.Execution.actual_final_end_at.is_(None)))

        def completed_between(start: datetime, end: datetime) -> list:
            rows = self._session.scalars(select(models.Execution).where(
                *self._completed(), models.Execution.actual_final_end_at >= start,
                models.Execution.actual_final_end_at < end))
            return [to_task_execution(row) for row in rows]

        return collect_completion_history(
            start_utc, end_utc, completed_between=completed_between,
            placements_for=lambda ids: self.get_placements(ids, include_deleted=True),
            tasks_for=lambda ids: self.get_tasks(ids, include_deleted=True),
            types_for=lambda ids: self.get_task_types(ids, include_deleted=True),
            unknown_completion_dates=unknown or 0,
        )

    def history_bounds(self) -> HistoryBounds:
        planned = self._session.execute(
            select(func.min(models.Placement.planned_start), func.max(models.Placement.planned_start))
            .where(models.Placement.user_id == self._user_id)).one()
        completed = self._session.execute(
            select(func.min(models.Execution.actual_final_end_at), func.max(models.Execution.actual_final_end_at))
            .where(*self._completed(), models.Execution.actual_final_end_at.is_not(None))).one()
        return HistoryBounds(planned[0], planned[1], completed[0], completed[1])

    def _placements_starting_between(self, start_utc: datetime, end_utc: datetime) -> list[ScheduledTask]:
        rows = self._rows(models.Placement, models.Placement.planned_start >= start_utc,
                          models.Placement.planned_start < end_utc, include_deleted=True)
        return _ordered_placements(_placement(row) for row in rows)

    def _executions_for_placements(self, placement_ids: Iterable) -> list[ExecutionHistory]:
        ids = [uuid.UUID(str(value)) for value in placement_ids]
        if not ids:
            return []
        rows = sorted(self._rows(models.Execution, models.Execution.scheduled_task_id.in_(ids)), key=lambda row: str(row.id))
        sessions: dict[uuid.UUID, list] = defaultdict(list)
        if rows:
            for work in self._session.scalars(
                select(models.WorkSession).where(
                    models.WorkSession.user_id == self._user_id,
                    models.WorkSession.execution_id.in_([row.id for row in rows]),
                ).order_by(models.WorkSession.execution_id, models.WorkSession.position)
            ):
                sessions[work.execution_id].append(to_work_session(work))
        return [ExecutionHistory(to_task_execution(row), tuple(sessions[row.id])) for row in rows]

    def cancel_unstarted_execution(
        self, placement_id, *, at: datetime, reason: CancelReason = CancelReason.RESCHEDULED
    ) -> str | None:
        """
        The execution disposition of a placement that stops being the plan
        before its work started (see the SQLite repository): the placement's
        live, never-started execution is cancelled with `reason` through the
        Mutator's lifecycle action -- the shared transition table, a server
        version and a change-log entry. Returns its id, or None.
        """
        with self.transaction():
            row = self._session.scalars(select(models.Execution).where(
                models.Execution.user_id == self._user_id,
                models.Execution.scheduled_task_id == uuid.UUID(str(placement_id)),
                models.Execution.deleted_at.is_(None),
                models.Execution.status == ExecutionStatus.SCHEDULED.value,
            )).first()
            if row is None:
                return None
            self._mutator.execution_action(row.id, "cancel", ActionIn(base_version=row.version, at=at,
                                                                      cancel_reason=CancelReason(reason)))
            return str(row.id)

    def get_placements(self, placement_ids: Iterable, *, include_deleted: bool = False) -> dict[uuid.UUID, ScheduledTask]:
        return {row.id: _placement(row) for row in self._by_ids(models.Placement, placement_ids, include_deleted)}

    def list_placements(self, start_date: date_, end_date: date_, *, include_deleted: bool = False) -> list[ScheduledTask]:
        rows = self._rows(models.Placement, models.Placement.planned_date.between(start_date, end_date),
                          include_deleted=include_deleted)
        return _ordered_placements(_placement(row) for row in rows)

    def active_placements_for_tasks(self, task_ids: Iterable) -> dict[uuid.UUID, list[ScheduledTask]]:
        ids = [uuid.UUID(str(value)) for value in task_ids]
        grouped: dict[uuid.UUID, list[ScheduledTask]] = defaultdict(list)
        if ids:
            for placement in _ordered_placements(_placement(row) for row in self._rows(
                    models.Placement, models.Placement.task_id.in_(ids))):
                grouped[placement.task_id].append(placement)
        return dict(grouped)

    def placement_ids_with_history(self, placement_ids: Iterable) -> set[uuid.UUID]:
        return set(self.placement_execution_statuses(placement_ids))

    def placement_execution_statuses(self, placement_ids: Iterable) -> dict[uuid.UUID, str]:
        ids = [uuid.UUID(str(value)) for value in placement_ids]
        if not ids:
            return {}
        rows = self._session.execute(select(models.Execution.scheduled_task_id, models.Execution.status).where(
            models.Execution.user_id == self._user_id, models.Execution.scheduled_task_id.in_(ids)))
        return {placement_id: status for placement_id, status in rows}

    def execution_facts_for_tasks(self, task_ids: Iterable) -> dict[uuid.UUID, list[ExecutionFact]]:
        ids = [uuid.UUID(str(value)) for value in task_ids]
        grouped: dict[uuid.UUID, list[ExecutionFact]] = defaultdict(list)
        if not ids:
            return {}
        rows = sorted(self._rows(models.Execution, models.Execution.task_id.in_(ids)),
                      key=lambda row: (row.updated_at, str(row.id)))
        for row in rows:
            grouped[row.task_id].append(ExecutionFact(
                task_id=row.task_id, scheduled_task_id=row.scheduled_task_id, status=row.status,
                finished_at=row.actual_final_end_at, updated_at=row.updated_at.isoformat(),
            ))
        return dict(grouped)

    # ------------------------------------------------------------------
    # Preference layers
    # ------------------------------------------------------------------

    def insert_preference(self, record: PreferenceRecord) -> None:
        self._insert(PREFERENCES, record, _payload(PreferenceCreate, _preference_fields(record)))

    def update_preference(self, record: PreferenceRecord, *, expected_version: int) -> bool:
        return self._update(PREFERENCES, record, _payload(PreferenceCreate, _preference_fields(record)), expected_version)

    def soft_delete_preference(self, record_id, *, deleted_at, expected_version) -> bool:
        return self._soft_delete(PREFERENCES, record_id, expected_version)

    def get_preference(self, scope: PreferenceScope, day: date_ | None = None) -> PreferenceRecord | None:
        key = "user" if scope == PreferenceScope.USER else day.isoformat()
        rows = self._rows(models.Preference, models.Preference.scope_key == key)
        return _preference(rows[0]) if rows else None

    def list_preference_records(self, *, include_deleted: bool = False) -> list[PreferenceRecord]:
        rows = self._rows(models.Preference, include_deleted=include_deleted)
        return sorted((_preference(row) for row in rows),
                      key=lambda record: (record.scope != PreferenceScope.USER, record.date or date_.min, str(record.id)))

    def list_date_preferences(self, start_date: date_, end_date: date_) -> list[PreferenceRecord]:
        return self.list_date_preference_records(start_date, end_date)  # one live layer per date and user

    def list_date_preference_records(self, start_date: date_, end_date: date_) -> list[PreferenceRecord]:
        rows = self._rows(models.Preference, models.Preference.scope == "date",
                          models.Preference.scope_date.between(start_date, end_date))
        return sorted((_preference(row) for row in rows), key=lambda record: (record.date, str(record.id)))

    def get_preference_by_id(self, record_id, *, include_deleted: bool = False) -> PreferenceRecord | None:
        rows = self._by_ids(models.Preference, [record_id], include_deleted)
        return _preference(rows[0]) if rows else None

    # ------------------------------------------------------------------
    # Schedule provenance
    # ------------------------------------------------------------------

    def insert_generation(self, record: GenerationRecord) -> None:
        self._insert(GENERATIONS, record, _payload(GenerationCreate, _generation_fields(record)))

    def update_generation(self, record: GenerationRecord, *, expected_version: int) -> bool:
        return self._update(GENERATIONS, record, _payload(GenerationCreate, _generation_fields(record)), expected_version)

    def soft_delete_generations(self, start_date: date_, end_date: date_, *, deleted_at) -> int:
        with self.transaction():
            return sum(int(self._soft_delete(GENERATIONS, record.id, None))
                       for record in self.list_generations(start_date, end_date))

    def get_generation_by_id(self, record_id, *, include_deleted: bool = False) -> GenerationRecord | None:
        rows = self._by_ids(models.ScheduleGeneration, [record_id], include_deleted)
        return _generation(rows[0]) if rows else None

    def list_generations(self, start_date: date_, end_date: date_) -> list[GenerationRecord]:
        rows = self._rows(models.ScheduleGeneration, models.ScheduleGeneration.planned_date.between(start_date, end_date))
        return sorted((_generation(row) for row in rows), key=lambda record: (record.planned_date, str(record.id)))


# -----------------------------------------------------------------------------
# Row <-> canonical model mapping
# -----------------------------------------------------------------------------


def _audit(row) -> dict:
    return {"id": row.id, "user_id": row.user_id, "created_at": row.created_at, "updated_at": row.updated_at,
            "version": row.version, "deleted_at": row.deleted_at}


def _project(row) -> Project:
    return Project(name=row.name, description=row.description, **_audit(row))


def _task_type(row) -> TaskType:
    return TaskType(label=row.label, **_audit(row))


def _task(row) -> Task:
    return Task.model_validate({**_audit(row), **task_content(row)})


def _task_payload(task: Task):
    return _payload(TaskCreate, task.model_dump(include=set(TaskCreate.model_fields) - {"id"}))


def _block_fields(block: FixedBlock) -> dict:
    return block.model_dump(include={"label", "category", "planned_date", "timezone", "planned_start", "planned_end"})


def _block(row) -> FixedBlock:
    return FixedBlock(label=row.label, category=row.category, planned_date=row.planned_date, timezone=row.timezone,
                      planned_start=row.planned_start, planned_end=row.planned_end, **_audit(row))


def _placement_fields(placement: ScheduledTask) -> dict:
    return placement.model_dump(include={"task_id", "planned_date", "timezone", "planned_start", "planned_end", "score",
                                         "optimization_metadata", *PLACEMENT_SNAPSHOT_FIELDS, "removal_reason",
                                         "superseded_by_id", "origin", "preserved"})


def _placement(row) -> ScheduledTask:
    return ScheduledTask(task_id=row.task_id, planned_date=row.planned_date, timezone=row.timezone,
                         planned_start=row.planned_start, planned_end=row.planned_end, score=row.score,
                         optimization_metadata=dict(row.optimization_metadata), **placement_snapshot(row),
                         removal_reason=row.removal_reason, superseded_by_id=row.superseded_by_id,
                         origin=row.origin, preserved=bool(row.preserved), **_audit(row))


def _ordered_placements(placements: Iterable[ScheduledTask]) -> list[ScheduledTask]:
    return sorted(placements, key=lambda p: (p.planned_date, p.planned_start, str(p.id)))


def _preference_fields(record: PreferenceRecord) -> dict:
    return {"scope": record.scope.value, "date": record.date, "overrides": record.overrides}


def _preference(row) -> PreferenceRecord:
    return PreferenceRecord(scope=PreferenceScope(row.scope), date=row.scope_date, overrides=preference_overrides(row),
                            **_audit(row))


_GENERATION_FIELDS = (
    "planned_date", "timezone", "engine_mode", "range_start", "range_end", "range_scope", "allocation_id",
    "fingerprint", "fingerprint_version", "placements_digest", "placement_count", "unscheduled_count",
    "total_score", "generated_at",
)


def _generation_fields(record: GenerationRecord) -> dict:
    return record.model_dump(include=set(_GENERATION_FIELDS))


def _generation(row) -> GenerationRecord:
    return GenerationRecord(**{name: getattr(row, name) for name in _GENERATION_FIELDS}, **_audit(row))
