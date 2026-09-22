"""
app/planning/application.py

The persistence-backed planning application service (Milestone 2): the one
boundary through which callers (app/ui/planning_controller.py today; the
desktop widgets and CSV import in later stages) read and write persisted
canonical planning data. It owns the business rules around storage --
reference checks, deletion policy, version/audit bookkeeping, scoped
placement replacement, and date-range eligibility -- while
app/planning/repository.py only maps rows. Scheduling itself stays in
app/planning/allocation.py, app/planning/service.py (selected-day
generation), and app/optimizer.py; nothing here schedules or scores.

Snapshots: every returned model is freshly loaded from the database.
Mutating a returned Task/FixedBlock/ScheduledTask (or one passed in) never
changes stored state; only an explicit save does.

Audit/version rules (the repository itself stores values verbatim):
    - Creating a task/project stores it exactly as given (its own id,
      created_at, updated_at, version -- e.g. an import keeps them).
    - Saving an existing task/project whose content changed keeps the
      stored created_at, sets updated_at to the service clock, and sets
      version to max(stored, given) + 1. Saving unchanged content is a
      no-op that returns the stored snapshot.
    - Placements follow the same rule by id inside replace_placements.
    - This is last-write-wins for a single local user; there is no
      optimistic-concurrency rejection or sync conflict resolution.

Deletion/replacement policy:
    - Task: refused (EntityInUseError) while another *surviving* task
      depends on it -- deleting a dependency must never silently unblock
      its dependents. Deleting both in one delete_tasks call is fine. The
      task's own tags/preferred dates/dependency edges/recurrence weekdays
      and all of its placements are removed with it.
    - Project: refused (EntityInUseError) while any task references it.
    - Placement: replace_placements(start, end, placements) makes the
      stored placements dated within [start, end] exactly `placements`
      (upsert by id, delete the rest *of that range only*). Placements
      dated outside the range are never touched; a placement whose id is
      already stored on a date outside the range is rejected (ScopeError)
      rather than moved.
    - Execution history is never deleted, modified, or used to block a
      planning edit: a deleted/replaced task or placement that executions
      reference simply leaves those executions with their historical
      task_id/scheduled_task_id and snapshot (see app/execution/db.py,
      "Execution <-> planning links"). replace_placements reports such
      placements in PlacementReplacement.removed_with_history_ids so a UI
      can say so.
    - Dependency cycles and overlapping fixed blocks are stored as given:
      detecting them is the PERT/constraint layers' job (allocation and
      the day scheduler already report them), not the store's.

Date-range task eligibility (tasks_for_range / load_range), derived from
app.planning.allocation._feasible_dates_for_task's hard date rules so a
range query never hides a task allocation could place there:
    - required_date set: eligible iff start <= required_date <= end;
    - else deadline set: eligible iff the deadline's UTC calendar date is
      >= start (some date in range is on/before the deadline);
    - else: always eligible (a floating task).
Recurrence is model-only (no expansion): a recurring template is treated
exactly like any other task. Placements and preferred_dates never affect
eligibility (preferred_dates only rank dates). A dependency outside the
eligible set is not pulled in: allocation then reports the dependent as
blocked/unresolved rather than silently ignoring the edge. Ordering is
(created_at, id), deterministic across reopen.

RangeScope.PLANNED (used by the desktop pages, whose tasks are entered
"on" a date): a task's *planned date* is its required_date, else its
earliest preferred date, else none (task_planned_date). A range's planned
tasks are those whose planned date lies in the range, plus undated tasks
that are eligible for it. Unlike ELIGIBLE, a task planned for another week
does not appear in (or get allocated into) this week just because it has
no hard date constraint. ELIGIBLE remains the default for load_range.

clear_range(start, end, include_planning_data=...) is the "reset" scope:
it always deletes the placements dated in the range; with
include_planning_data it also deletes the fixed blocks dated in the range
and the tasks whose planned date is in the range (undated tasks are never
touched). All in one transaction, subject to the deletion policy above
(e.g. refused if a task outside the range depends on one inside it).
Execution history is never deleted by it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as date_
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from app.execution.db import get_connection
from app.planning.errors import (
    DuplicateEntityError,
    EntityInUseError,
    EntityNotFoundError,
    InvalidEntityError,
    InvalidReferenceError,
    ScopeError,
)
from app.planning.models import (
    DayScheduleOutput,
    FixedBlock,
    Project,
    ScheduledTask,
    Task,
    TaskRegistry,
    compute_total_score,
)
from app.planning.repository import PlanningRepository

Clock = Callable[[], datetime]

_AUDIT_FIELDS = {"created_at", "updated_at", "version"}


class RangeScope(str, Enum):
    #: Allocation feasibility (see "Date-range task eligibility").
    ELIGIBLE = "eligible"
    #: Planned date in range, plus undated eligible tasks (see RangeScope.PLANNED notes).
    PLANNED = "planned"


def task_planned_date(task: Task) -> date_ | None:
    """required_date, else the earliest preferred date, else None (mirrors the repository SQL)."""
    if task.required_date is not None:
        return task.required_date
    return min(task.preferred_dates) if task.preferred_dates else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dates(start_date: date_, end_date: date_) -> list[date_]:
    return [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]


def _require_range(start_date: date_, end_date: date_) -> None:
    if end_date < start_date:
        raise ScopeError(f"end_date {end_date} is before start_date {start_date}.")


def _group_by_date(items, start_date: date_, end_date: date_) -> dict:
    grouped = {day: [] for day in _dates(start_date, end_date)}
    for item in items:
        grouped[item.planned_date].append(item)
    return grouped


def _same_content(stored, incoming) -> bool:
    return stored.model_dump(exclude=_AUDIT_FIELDS) == incoming.model_dump(exclude=_AUDIT_FIELDS)


@dataclass(frozen=True)
class PlanningRange:
    """Everything persisted that planning needs for one inclusive date range (a snapshot)."""

    start_date: date_
    end_date: date_
    #: Eligible tasks (see the module docstring), in (created_at, id) order.
    task_ids: list[uuid.UUID]
    tasks: TaskRegistry
    #: Every date in the range is a key, even when it has no entries.
    fixed_blocks_by_date: dict[date_, list[FixedBlock]]
    placements_by_date: dict[date_, list[ScheduledTask]]


@dataclass(frozen=True)
class PlacementReplacement:
    start_date: date_
    end_date: date_
    #: The stored placements of the range after replacement, as re-read.
    placements: list[ScheduledTask]
    removed_ids: list[uuid.UUID] = field(default_factory=list)
    #: The removed placements that execution history references (history kept).
    removed_with_history_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class ImportApplyResult:
    """What one committed import wrote (and, for a replace, cleared first)."""

    tasks: list[Task]
    fixed_blocks: list[FixedBlock]
    replaced_range: tuple[date_, date_] | None
    cleared: RangeClearResult | None


@dataclass(frozen=True)
class RangeClearResult:
    deleted_placements: int
    deleted_fixed_blocks: int
    deleted_tasks: int
    #: Deleted placements that execution history references (history kept).
    placements_with_history: int


class PlanningService:
    def __init__(self, repository: PlanningRepository, clock: Clock = _utcnow) -> None:
        self._repository = repository
        self._clock = clock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several service calls into one atomic unit of work."""
        with self._repository.transaction():
            yield

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def create_task(self, task: Task) -> Task:
        """Persist a new task exactly as given. DuplicateEntityError if its id exists."""
        with self._repository.transaction():
            if self._repository.existing_task_ids([task.id]):
                raise DuplicateEntityError("task", task.id)
            return self._save_tasks([task])[0]

    def update_task(self, task: Task) -> Task:
        """Overwrite an existing task (see the audit rules). EntityNotFoundError if absent."""
        with self._repository.transaction():
            if not self._repository.existing_task_ids([task.id]):
                raise EntityNotFoundError("task", task.id)
            return self._save_tasks([task])[0]

    def save_task(self, task: Task) -> Task:
        """Create or update one task."""
        return self.save_tasks([task])[0]

    def save_tasks(self, tasks: Iterable[Task]) -> list[Task]:
        """
        Create or update several tasks atomically: either every task (and
        every child row) is written, or none is. Dependencies/projects may
        reference other tasks in the same batch, in any order.
        """
        with self._repository.transaction():
            return self._save_tasks(list(tasks))

    def get_task(self, task_id: uuid.UUID) -> Task | None:
        return self._repository.get_task(task_id)

    def get_tasks(self, task_ids: Iterable[uuid.UUID]) -> TaskRegistry:
        """The persisted subset of `task_ids`, as a registry."""
        return TaskRegistry(tasks=self._repository.get_tasks(task_ids))

    def list_tasks(self) -> list[Task]:
        return self._repository.list_tasks()

    def tasks_for_range(self, start_date: date_, end_date: date_) -> list[Task]:
        """Tasks eligible for [start_date, end_date] (see the module docstring)."""
        _require_range(start_date, end_date)
        return self._repository.list_tasks_eligible_for_range(start_date, end_date)

    def tasks_planned_in_range(
        self, start_date: date_, end_date: date_, *, include_undated: bool = True
    ) -> list[Task]:
        """Tasks in RangeScope.PLANNED for the range (or only dated ones)."""
        _require_range(start_date, end_date)
        return self._repository.list_tasks_planned_in_range(start_date, end_date, include_undated=include_undated)

    def delete_task(self, task_id: uuid.UUID) -> bool:
        """Delete one task; False if it did not exist. See the deletion policy."""
        return self.delete_tasks([task_id]) > 0

    def delete_tasks(self, task_ids: Iterable[uuid.UUID]) -> int:
        ids = set(task_ids)
        with self._repository.transaction():
            blocking = {
                dependency_id: dependents - ids
                for dependency_id, dependents in self._repository.dependents_of(ids).items()
                if dependents - ids
            }
            if blocking:
                dependents = set().union(*blocking.values())
                raise EntityInUseError(
                    "Cannot delete task(s) "
                    + ", ".join(sorted(str(task_id) for task_id in blocking))
                    + " while other tasks depend on them: "
                    + ", ".join(sorted(str(task_id) for task_id in dependents))
                    + ". Remove those dependencies first, or delete the dependents too.",
                    dependents,
                )
            return self._repository.delete_tasks(ids)

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def save_project(self, project: Project) -> Project:
        with self._repository.transaction():
            stored = self._repository.get_project(project.id)
            if stored is None:
                to_store = project
            elif _same_content(stored, project):
                return stored
            else:
                to_store = project.model_copy(
                    update={
                        "created_at": stored.created_at,
                        "updated_at": self._clock(),
                        "version": max(stored.version, project.version) + 1,
                    }
                )
            self._repository.upsert_project(to_store)
            return self._repository.get_project(project.id)

    def get_project(self, project_id: uuid.UUID) -> Project | None:
        return self._repository.get_project(project_id)

    def list_projects(self) -> list[Project]:
        return self._repository.list_projects()

    def delete_project(self, project_id: uuid.UUID) -> bool:
        with self._repository.transaction():
            task_ids = self._repository.task_ids_for_project(project_id)
            if task_ids:
                raise EntityInUseError(
                    f"Cannot delete project {project_id}: {len(task_ids)} task(s) still belong to it.", task_ids
                )
            return self._repository.delete_project(project_id)

    # ------------------------------------------------------------------
    # Fixed blocks
    # ------------------------------------------------------------------

    def save_fixed_block(self, block: FixedBlock) -> FixedBlock:
        """Create or overwrite one fixed block (it may move to another date)."""
        with self._repository.transaction():
            self._repository.upsert_fixed_block(block)
            return self._repository.get_fixed_blocks([block.id])[block.id]

    def delete_fixed_block(self, block_id: uuid.UUID) -> bool:
        return self._repository.delete_fixed_blocks([block_id]) > 0

    def set_fixed_blocks_for_date(self, day: date_, blocks: Iterable[FixedBlock]) -> list[FixedBlock]:
        """
        Make `day`'s stored fixed blocks exactly `blocks`, atomically. Every
        block must be dated `day`; a block id already stored on another date
        is rejected (ScopeError) rather than moved. Other dates are untouched.
        """
        blocks = list(blocks)
        ids = [block.id for block in blocks]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError(f"duplicate fixed block ids for {day}.")
        wrong_date = [block for block in blocks if block.planned_date != day]
        if wrong_date:
            raise ScopeError(f"fixed block {wrong_date[0].id} is dated {wrong_date[0].planned_date}, not {day}.")

        with self._repository.transaction():
            elsewhere = [
                stored for stored in self._repository.get_fixed_blocks(ids).values() if stored.planned_date != day
            ]
            if elsewhere:
                raise ScopeError(
                    f"fixed block {elsewhere[0].id} is stored on {elsewhere[0].planned_date}; "
                    f"set_fixed_blocks_for_date({day}) will not move it."
                )
            current_ids = {block.id for block in self._repository.list_fixed_blocks(day, day)}
            self._repository.delete_fixed_blocks(current_ids - set(ids))
            for block in blocks:
                self._repository.upsert_fixed_block(block)
            return self._repository.list_fixed_blocks(day, day)

    def fixed_blocks_for_date(self, day: date_) -> list[FixedBlock]:
        return self._repository.list_fixed_blocks(day, day)

    def fixed_blocks_for_range(self, start_date: date_, end_date: date_) -> dict[date_, list[FixedBlock]]:
        _require_range(start_date, end_date)
        return _group_by_date(self._repository.list_fixed_blocks(start_date, end_date), start_date, end_date)

    # ------------------------------------------------------------------
    # Placements
    # ------------------------------------------------------------------

    def replace_placements(
        self, start_date: date_, end_date: date_, placements: Iterable[ScheduledTask]
    ) -> PlacementReplacement:
        """
        Atomically make the stored placements dated within [start_date,
        end_date] exactly `placements` (see the module docstring's
        replacement policy). Any failure leaves every stored placement,
        in and out of the range, unchanged.
        """
        _require_range(start_date, end_date)
        placements = list(placements)
        ids = [placement.id for placement in placements]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError("duplicate placement ids in replacement.")
        outside = [p for p in placements if not start_date <= p.planned_date <= end_date]
        if outside:
            raise ScopeError(
                f"placement {outside[0].id} is dated {outside[0].planned_date}, outside [{start_date}, {end_date}]."
            )

        with self._repository.transaction():
            stored_by_id = self._repository.get_placements(ids)
            moved = [stored for stored in stored_by_id.values() if not start_date <= stored.planned_date <= end_date]
            if moved:
                raise ScopeError(
                    f"placement {moved[0].id} is stored on {moved[0].planned_date}, outside "
                    f"[{start_date}, {end_date}]; a scoped replacement will not move it."
                )

            task_ids = {placement.task_id for placement in placements}
            missing = task_ids - self._repository.existing_task_ids(task_ids)
            if missing:
                raise InvalidReferenceError(
                    "placements reference tasks that are not persisted: " + ", ".join(sorted(map(str, missing))),
                    missing,
                )

            current_ids = {placement.id for placement in self._repository.list_placements(start_date, end_date)}
            removed = current_ids - set(ids)
            removed_with_history = self._repository.placement_ids_with_history(removed)
            self._repository.delete_placements(removed)

            now = self._clock()
            for placement in placements:
                stored = stored_by_id.get(placement.id)
                if stored is not None and _same_content(stored, placement):
                    continue
                if stored is not None:
                    placement = placement.model_copy(
                        update={
                            "created_at": stored.created_at,
                            "updated_at": now,
                            "version": max(stored.version, placement.version) + 1,
                        }
                    )
                self._repository.upsert_placement(placement)

            return PlacementReplacement(
                start_date=start_date,
                end_date=end_date,
                placements=self._repository.list_placements(start_date, end_date),
                removed_ids=sorted(removed, key=str),
                removed_with_history_ids=sorted(removed_with_history, key=str),
            )

    def placements_for_date(self, day: date_) -> list[ScheduledTask]:
        return self._repository.list_placements(day, day)

    def placements_for_range(self, start_date: date_, end_date: date_) -> dict[date_, list[ScheduledTask]]:
        _require_range(start_date, end_date)
        return _group_by_date(self._repository.list_placements(start_date, end_date), start_date, end_date)

    def stored_day_output(self, day: date_, timezone_name: str) -> DayScheduleOutput | None:
        """
        The stored placements for `day` as a DayScheduleOutput (None if
        there are none) -- suitable as generate_selected_day's
        previous_result, so an unchanged placement keeps its id (and any
        execution history linked to it) across regenerations and restarts.
        """
        placements = self.placements_for_date(day)
        if not placements:
            return None
        registry = self.get_tasks(placement.task_id for placement in placements)
        return DayScheduleOutput(
            date=day, timezone=timezone_name, tasks=registry, placements=placements,
            total_score=compute_total_score(placements),
        )

    # ------------------------------------------------------------------
    # Range loading
    # ------------------------------------------------------------------

    def load_range(
        self, start_date: date_, end_date: date_, *, scope: RangeScope = RangeScope.ELIGIBLE
    ) -> PlanningRange:
        """One consistent snapshot of the range's tasks (per `scope`), fixed blocks, and placements."""
        _require_range(start_date, end_date)
        with self._repository.transaction():
            if scope == RangeScope.PLANNED:
                tasks = self._repository.list_tasks_planned_in_range(start_date, end_date)
            else:
                tasks = self._repository.list_tasks_eligible_for_range(start_date, end_date)
            fixed_blocks = self._repository.list_fixed_blocks(start_date, end_date)
            placements = self._repository.list_placements(start_date, end_date)

        return PlanningRange(
            start_date=start_date,
            end_date=end_date,
            task_ids=[task.id for task in tasks],
            tasks=TaskRegistry(tasks={task.id: task for task in tasks}),
            fixed_blocks_by_date=_group_by_date(fixed_blocks, start_date, end_date),
            placements_by_date=_group_by_date(placements, start_date, end_date),
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def clear_range(self, start_date: date_, end_date: date_, *, include_planning_data: bool) -> RangeClearResult:
        """
        Delete the placements dated in the range and, with
        include_planning_data, the range's fixed blocks and dated tasks --
        atomically (see the module docstring). Never touches execution
        history or anything dated outside the range.
        """
        _require_range(start_date, end_date)
        with self._repository.transaction():
            placement_ids = [p.id for p in self._repository.list_placements(start_date, end_date)]
            history = self._repository.placement_ids_with_history(placement_ids)
            self._repository.delete_placements(placement_ids)

            block_ids: list[uuid.UUID] = []
            task_ids: list[uuid.UUID] = []
            if include_planning_data:
                block_ids = [b.id for b in self._repository.list_fixed_blocks(start_date, end_date)]
                self._repository.delete_fixed_blocks(block_ids)
                task_ids = [
                    t.id for t in self._repository.list_tasks_planned_in_range(start_date, end_date, include_undated=False)
                ]
                # Their placements dated *outside* the range go with them (cascade).
                self.delete_tasks(task_ids)

            return RangeClearResult(
                deleted_placements=len(placement_ids),
                deleted_fixed_blocks=len(block_ids),
                deleted_tasks=len(task_ids),
                placements_with_history=len(history),
            )

    # ------------------------------------------------------------------
    # Bulk import
    # ------------------------------------------------------------------

    def apply_import(
        self,
        tasks: Iterable[Task],
        fixed_blocks: Iterable[FixedBlock],
        *,
        replace_range: tuple[date_, date_] | None = None,
    ) -> ImportApplyResult:
        """
        Write a validated import in one transaction: optionally clear
        `replace_range` first (clear_range with planning data), then create
        every task and fixed block. New entities only -- an id that is
        already stored is a DuplicateEntityError, and a fixed block that
        overlaps a stored block on its date (after any clearing) is an
        InvalidEntityError. Any failure rolls back everything, including
        the clearing.
        """
        tasks = list(tasks)
        fixed_blocks = list(fixed_blocks)
        with self._repository.transaction():
            cleared = None
            if replace_range is not None:
                cleared = self.clear_range(*replace_range, include_planning_data=True)

            existing_tasks = self._repository.existing_task_ids(task.id for task in tasks)
            if existing_tasks:
                raise DuplicateEntityError("task", sorted(existing_tasks, key=str)[0])
            existing_blocks = self._repository.get_fixed_blocks(block.id for block in fixed_blocks)
            if existing_blocks:
                raise DuplicateEntityError("fixed block", sorted(existing_blocks, key=str)[0])

            for block in fixed_blocks:
                for stored in self._repository.list_fixed_blocks(block.planned_date, block.planned_date):
                    if block.planned_start < stored.planned_end and stored.planned_start < block.planned_end:
                        raise InvalidEntityError(
                            f"fixed block {block.label!r} overlaps the saved fixed block {stored.label!r} "
                            f"on {block.planned_date}."
                        )
                self._repository.upsert_fixed_block(block)

            saved_tasks = self._save_tasks(tasks) if tasks else []
            saved_blocks = self._repository.get_fixed_blocks(block.id for block in fixed_blocks)
            return ImportApplyResult(
                tasks=saved_tasks,
                fixed_blocks=[saved_blocks[block.id] for block in fixed_blocks],
                replaced_range=replace_range,
                cleared=cleared,
            )

    # ------------------------------------------------------------------
    # Flat reads (exports)
    # ------------------------------------------------------------------

    def list_fixed_blocks(self, start_date: date_ = date_.min, end_date: date_ = date_.max) -> list[FixedBlock]:
        """Fixed blocks dated in the range (default: all), ordered by (date, start, id)."""
        _require_range(start_date, end_date)
        return self._repository.list_fixed_blocks(start_date, end_date)

    def list_placements(self, start_date: date_ = date_.min, end_date: date_ = date_.max) -> list[ScheduledTask]:
        """Placements dated in the range (default: all), ordered by (date, start, id)."""
        _require_range(start_date, end_date)
        return self._repository.list_placements(start_date, end_date)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _save_tasks(self, tasks: list[Task]) -> list[Task]:
        # Caller holds a repository transaction.
        ids = [task.id for task in tasks]
        if len(set(ids)) != len(ids):
            raise InvalidEntityError("duplicate task ids in one save.")
        batch_ids = set(ids)

        dependency_ids = {dependency for task in tasks for dependency in task.dependency_ids} - batch_ids
        missing_dependencies = dependency_ids - self._repository.existing_task_ids(dependency_ids)
        if missing_dependencies:
            raise InvalidReferenceError(
                "dependency_ids reference tasks that are not persisted: "
                + ", ".join(sorted(map(str, missing_dependencies))),
                missing_dependencies,
            )

        project_ids = {task.project_id for task in tasks if task.project_id is not None}
        missing_projects = project_ids - self._repository.existing_project_ids(project_ids)
        if missing_projects:
            raise InvalidReferenceError(
                "project_id references projects that are not persisted: " + ", ".join(sorted(map(str, missing_projects))),
                missing_projects,
            )

        stored_by_id = self._repository.get_tasks(ids)
        now = self._clock()
        for task in tasks:
            stored = stored_by_id.get(task.id)
            if stored is not None and _same_content(stored, task):
                continue
            if stored is not None:
                task = task.model_copy(
                    update={
                        "created_at": stored.created_at,
                        "updated_at": now,
                        "version": max(stored.version, task.version) + 1,
                    }
                )
            self._repository.upsert_task(task)

        saved = self._repository.get_tasks(ids)
        return [saved[task_id] for task_id in ids]


@contextmanager
def open_planning_service(db_path: str | Path | None = None) -> Iterator[PlanningService]:
    """
    Open the application database and yield a PlanningService on it,
    closing the connection on exit. Pass an explicit db_path in tests;
    omit it to use the configured default location.
    """
    connection = get_connection(db_path)
    try:
        yield PlanningService(PlanningRepository(connection))
    finally:
        connection.close()
